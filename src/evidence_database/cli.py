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
    check = commands.add_parser("check", help="Admit only an exact, human-reviewed quote")
    check.add_argument("--citation", required=True)
    check.add_argument("--quote", required=True)
    check.add_argument("--inspect", action="store_true", help="Check text presence only; never admits evidence")
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
    listing = commands.add_parser("list", help="List source and conversion revisions")
    listing.add_argument("--id", dest="document_id")
    from .paper_cli import add_papers_parser
    from .workflow import add_project_parser
    add_papers_parser(commands)
    add_project_parser(commands)
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
        elif args.command == "history":
            result = store.reviews(args.citation)
        elif args.command == "check":
            result = store.check(args.citation, args.quote, require_review=not args.inspect)
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
