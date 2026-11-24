"""JSON command line interface. Inspection and evidence admission are distinct."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def parser():
    cli = argparse.ArgumentParser(description="Versioned local evidence with original-page review")
    cli.add_argument("--db", type=Path, default=Path("data/evidence"), help="Local evidence store directory")
    commands = cli.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize a local store")
    ingest = commands.add_parser("ingest", help="Copy/hash a local original; retain older revisions")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--id", required=True, dest="document_id")
    ingest.add_argument("--role", default="primary")
    ingest.add_argument("--metadata", type=Path, help="Optional provenance JSON object")
    convert = commands.add_parser("convert", help="Run a parser on the stored original")
    convert.add_argument("--source", required=True)
    convert.add_argument("--backend", choices=["baseline", "grobid"], required=True)
    convert.add_argument("--endpoint", help="Explicit existing GROBID endpoint")
    pilot = commands.add_parser("import-pilot", help="Snapshot hash-checked pre-parsed variants")
    pilot.add_argument("--manifest", required=True, type=Path)
    pilot.add_argument("--parser", action="append", required=True, help="name=output-root")
    legacy = commands.add_parser("import-legacy", help="Import original paper_text.py outputs with limitations")
    legacy.add_argument("--pdf", required=True, type=Path)
    legacy.add_argument("--receipt", required=True, type=Path)
    legacy.add_argument("--id", required=True, dest="document_id")
    legacy.add_argument("--provenance", type=Path)
    reconcile = commands.add_parser("reconcile", help="Append an AI-diagnosed revision; never approves evidence")
    reconcile.add_argument("--parent", required=True, help="Immutable parent conversion ID")
    reconcile.add_argument("--manifest", required=True, type=Path, help="Source-bound observation and edit JSON")
    find = commands.add_parser("search", help="Discover verified snapshots; results may be unreviewed")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=20)
    for name in ("show", "history"):
        command = commands.add_parser(name)
        command.add_argument("--citation", required=True)
        if name == "show":
            command.add_argument("--sentences", action="store_true",
                                 help="Also number the block's sentences, to quote by position with check --sentences")
    check = commands.add_parser("check", help="Admit only an exact, human-reviewed quote")
    check.add_argument("--citation", required=True)
    quoted = check.add_mutually_exclusive_group(required=True)
    quoted.add_argument("--quote")
    quoted.add_argument("--sentences", metavar="FIRST[-LAST]",
                        help="Take the quotation from the stored text by sentence position, e.g. 2 or 2-3")
    check.add_argument("--inspect", action="store_true", help="Check text presence only; never admits evidence")
    check.add_argument("--accept-independent-reading", action="store_true",
                       help="Also admit a quote the text layer and the recorded OCR reading agree on")
    review = commands.add_parser("review", help="Append a review decision; human identity is self-declared")
    review.add_argument("--citation", required=True)
    review.add_argument("--quote", required=True)
    review.add_argument("--decision", choices=["approve", "reject", "revoke"], required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--reviewer-kind", choices=["human", "ai"], required=True)
    review.add_argument("--note", required=True)
    review.add_argument("--viewed-original", action="store_true")
    review.add_argument("--packet-id")
    packet = commands.add_parser("packet", help="Render hash-bound original pages for review")
    packet.add_argument("--citation", required=True)
    packet.add_argument("--out", required=True, type=Path, help="New folder; existing folders are refused")
    commands.add_parser("audit", help="Validate stored artifacts and count revisions/reviews")
    scan = commands.add_parser("scan", help="Scan conversions read-only for OCR disagreements and text defects")
    scan.add_argument("--id", dest="document_id", help="Optional document_id filter")
    scan.add_argument("--conversion", dest="conversion_id", help="Optional exact conversion_id filter")
    scan.add_argument("--include-typography", action="store_true",
                      help="Also list sentences carrying superscript or symbol-font spans")
    scan.add_argument("--limit", type=int, default=200, help="Maximum flagged sentences returned (1..5000)")
    listing = commands.add_parser("list", help="List source and conversion revisions")
    listing.add_argument("--id", dest="document_id")
    from .memory import MEMORY_CODES
    from .paper_cli import add_papers_parser
    from .triage import VALID_IMPORT_VERDICTS
    from .workflow import add_project_parser
    add_papers_parser(commands)
    add_project_parser(commands)
    memory = commands.add_parser("memory", help="Manage advisory pitfall notes without altering Store")
    memory.add_argument("--memory-db", type=Path, required=True, help="Isolated directory for memory ledger")
    mem_steps = memory.add_subparsers(dest="memory_action", required=True)
    mem_add = mem_steps.add_parser("add", help="Append an advisory pitfall note for a block or document")
    mem_add.add_argument("--code", choices=MEMORY_CODES, required=True)
    mem_add.add_argument("--note", required=True)
    mem_add.add_argument("--actor", required=True)
    mem_add.add_argument("--actor-kind", choices=["human", "ai"], required=True)
    mem_add.add_argument("--citation")
    mem_add.add_argument("--document-id")
    mem_add.add_argument("--role")
    mem_anchor = mem_add.add_mutually_exclusive_group()
    mem_anchor.add_argument("--quote", help="Exact quotation in --citation whose sentences are hashed")
    mem_anchor.add_argument("--sentences", metavar="FIRST[-LAST]",
                            help="1-based sentence span in --citation whose sentences are hashed")
    mem_add.add_argument("--related-citation", action="append", default=[],
                         help="Related block citation to cross-check alongside this note (repeatable)")
    mem_add.add_argument("--review-id", help="Optional rv_<64 hex> review event in Store")
    mem_add.add_argument("--no-store-check", action="store_true",
                         help="Allow document-level note registration when --db does not exist yet")
    mem_resolve = mem_steps.add_parser("resolve", help="Append a resolution event for an active memory note")
    mem_resolve.add_argument("--id", required=True, dest="target_memory_id")
    mem_resolve.add_argument("--reason", required=True)
    mem_resolve.add_argument("--actor", required=True)
    mem_resolve.add_argument("--actor-kind", choices=["human", "ai"], required=True)
    mem_list = mem_steps.add_parser("list", help="List active pitfall notes (and optional full event chain)")
    mem_list.add_argument("--document-id")
    mem_list.add_argument("--citation")
    mem_list.add_argument("--code", choices=MEMORY_CODES)
    mem_list.add_argument("--history", action="store_true")
    mem_steps.add_parser("audit", help="Verify canonical JSON hashes, sequence continuity and hash chain")
    mem_imp = mem_steps.add_parser("import-validator",
                                   help="Import semantic review pitfalls from Validator report or receipt JSON")
    mem_imp.add_argument("report_path", type=Path, help="Validator report JSON/JSONL or semantic receipt JSON")
    mem_imp.add_argument("--request", type=Path, dest="request_path",
                         help="Semantic request JSON (required only when importing a raw semantic receipt)")
    mem_imp.add_argument("--verdict", action="append", choices=VALID_IMPORT_VERDICTS, dest="verdicts",
                         help="Verdict(s) to import (default: contradicted, not_established, uncertain)")
    mem_imp.add_argument("--actor", default="validator_semantic_import")
    mem_imp.add_argument("--actor-kind", choices=["ai", "human"], default="ai")
    mem_imp.add_argument("--dry-run", action="store_true", help="Preview candidate notes without writing")
    return cli


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path else None


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "project":
            from .workflow import project_status, sync_project
            result = (project_status(args.config) if args.project_action == "status" else
                      sync_project(args.config, fetch=args.fetch, parse=not args.no_parse))
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 2 if result.get("needs_attention") else 0
        if args.command == "scan":
            from .store import Store
            from .triage import scan_store
            ro_store = Store(args.db, read_only=True)
            result = scan_store(ro_store, document_id=args.document_id, conversion_id=args.conversion_id,
                                include_typography=args.include_typography, limit=args.limit)
            print(json.dumps(result, ensure_ascii=True, indent=2, default=str, allow_nan=False))
            return 2 if result["summary"]["high_severity_sentences"] else 0
        if args.command == "memory":
            from .memory import MemoryLedger, validate_memory_root
            from .store import Store
            action = args.memory_action
            if action == "list":
                ledger = MemoryLedger(args.memory_db, read_only=True)
                result = ledger.list_notes(document_id=args.document_id, citation=args.citation,
                                           code=args.code, include_history=args.history)
                exit_code = 0
            elif action == "audit":
                ledger = MemoryLedger(args.memory_db, read_only=True)
                result = ledger.audit()
                exit_code = 0 if result["ok"] else 2
            elif action == "resolve":
                ledger = MemoryLedger(args.memory_db)
                result = ledger.resolve(args.target_memory_id, reason=args.reason,
                                        actor=args.actor, actor_kind=args.actor_kind)
                exit_code = 0
            elif action == "import-validator":
                from .triage import VALID_IMPORT_VERDICTS, import_validator_report, load_report_payloads
                ro_store = Store(args.db, read_only=True)
                validate_memory_root(args.memory_db, other_roots=(ro_store.root,))
                ledger = None if args.dry_run else MemoryLedger(args.memory_db, other_roots=(ro_store.root,))
                payloads = load_report_payloads(args.report_path)
                req = read_json(args.request_path) if args.request_path else None
                chosen = tuple(dict.fromkeys(args.verdicts)) if args.verdicts else VALID_IMPORT_VERDICTS
                result = import_validator_report(payloads, store=ro_store, ledger=ledger, actor=args.actor,
                                                 actor_kind=args.actor_kind, request_data=req,
                                                 verdicts=chosen, dry_run=args.dry_run)
                exit_code = 0
            else:
                span = None
                if args.sentences:
                    first, _, last = args.sentences.partition("-")
                    if not first.isdigit() or (last and not last.isdigit()):
                        raise ValueError("--sentences takes FIRST or FIRST-LAST, counted from 1")
                    span = (int(first), int(last) if last else None)
                needs_store = bool(args.citation or args.related_citation or args.review_id)
                if args.no_store_check and needs_store:
                    raise ValueError("--no-store-check cannot be combined with citation or review anchors")
                use_store = needs_store or (not args.no_store_check and (args.db / "index.sqlite3").is_file())
                ro_store = Store(args.db, read_only=True) if use_store else None
                other_roots = (ro_store.root,) if ro_store is not None else ()
                ledger = MemoryLedger(args.memory_db, other_roots=other_roots)
                result = ledger.add(code=args.code, note=args.note, actor=args.actor,
                                    actor_kind=args.actor_kind, document_id=args.document_id, role=args.role,
                                    citation=args.citation, quote=args.quote, sentence_range=span,
                                    related_citations=args.related_citation, review_id=args.review_id,
                                    store=ro_store)
                exit_code = 0
            print(json.dumps(result, ensure_ascii=True, indent=2, default=str, allow_nan=False))
            return exit_code
        from .store import Store
        store = Store(args.db)
        exit_code = 0
        if args.command == "papers":
            from .paper_cli import run_papers
            result, exit_code = run_papers(args, store)
        elif args.command == "init":
            result = {"root": str(args.db.resolve()), "status": "initialized"}
        elif args.command == "ingest":
            result = store.ingest(args.path, args.document_id, role=args.role, metadata=read_json(args.metadata))
        elif args.command == "convert":
            from .adapters import run_baseline, run_grobid
            if args.backend == "baseline":
                result = run_baseline(store, args.source)
            else:
                if not args.endpoint:
                    raise ValueError("GROBID requires an explicit --endpoint")
                result = run_grobid(store, args.source, endpoint=args.endpoint)
            if result.get("status") not in {"completed", "ok"}:
                exit_code = 2
        elif args.command == "import-pilot":
            from .adapters import import_pilot
            roots = {}
            for entry in args.parser:
                if "=" not in entry:
                    raise ValueError("--parser must be name=output-root")
                name, value = entry.split("=", 1)
                if not name or not value or name in roots:
                    raise ValueError("Parser names and paths must be nonempty and unique")
                roots[name] = Path(value)
            result = import_pilot(store, args.manifest, roots)
            if result.get("failed") or result.get("partial"):
                exit_code = 2
        elif args.command == "import-legacy":
            from .adapters import import_legacy
            result = import_legacy(store, args.pdf, args.receipt, document_id=args.document_id,
                                   run_provenance=read_json(args.provenance))
            if result.get("status") not in {"completed", "ok"}:
                exit_code = 2
        elif args.command == "reconcile":
            from .reconciliation import apply_reconciliation
            result = apply_reconciliation(store, args.parent, args.manifest)
            exit_code = 2  # A stored AI revision remains partial and pending human review.
        elif args.command == "search":
            if not 1 <= args.limit <= 1000:
                raise ValueError("--limit must be between 1 and 1000")
            result = store.search(args.query, limit=args.limit)
        elif args.command == "show":
            result = store.get(args.citation)
            if args.sentences:
                result = {**result, "sentences": store.sentences(args.citation)}
        elif args.command == "history":
            result = store.reviews(args.citation)
        elif args.command == "check":
            if args.sentences:
                first, _, last = args.sentences.partition("-")
                if not first.isdigit() or (last and not last.isdigit()):
                    raise ValueError("--sentences takes FIRST or FIRST-LAST, counted from 1")
                args.quote = store.sentence_quote(args.citation, int(first), int(last) if last else None)
            result = store.check(args.citation, args.quote, require_review=not args.inspect,
                                 accept_independent_reading=args.accept_independent_reading)
            exit_code = 0 if result.get("quote_matches" if args.inspect else "admitted") else 2
        elif args.command == "review":
            result = store.review(args.citation, args.quote, decision=args.decision,
                                  reviewer=args.reviewer, reviewer_kind=args.reviewer_kind,
                                  note=args.note, viewed_original=args.viewed_original,
                                  packet_id=args.packet_id)
        elif args.command == "packet":
            from .packets import create_packet
            result = create_packet(store, args.citation, args.out)
        elif args.command == "audit":
            result = store.audit()
            if result.get("ok") is False or result.get("errors"):
                exit_code = 2
        else:
            result = {"sources": store.sources(), "conversions": store.conversions(args.document_id)}
        print(json.dumps(result, ensure_ascii=True, indent=2, default=str, allow_nan=False))
        return exit_code
    except (ValueError, OSError, KeyError, ImportError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
