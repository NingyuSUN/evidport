"""Discovery/acquisition commands, kept separate from full-text evidence search."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def add_papers_parser(commands):
    papers = commands.add_parser("papers", help="Discover papers, acquire PDFs, and track missing full text")
    papers.add_argument("--queue", type=Path, default=Path("data/acquisition"))
    actions = papers.add_subparsers(dest="paper_action", required=True)
    discover = actions.add_parser("discover", help="Save bounded metadata searches; does not imply full-text access")
    discover.add_argument("--query", required=True)
    discover.add_argument("--provider", choices=["europepmc", "crossref", "both"], default="europepmc")
    discover.add_argument("--max-results", type=int, default=100, help="Maximum per provider, reported as a limit")
    resolve = actions.add_parser("resolve", help="Look up exact DOIs and retain metadata-only candidates")
    resolve.add_argument("--doi", action="append", required=True)
    register = actions.add_parser("register", help="Register a JSON metadata manifest from an existing search")
    register.add_argument("--manifest", type=Path, required=True)
    actions.add_parser("list", help="List acquisition states, including papers with no local full text")
    fetch = actions.add_parser("fetch", help="Attempt explicitly free PDF locations; keep failures visible")
    selection = fetch.add_mutually_exclusive_group(required=True)
    selection.add_argument("--paper")
    selection.add_argument("--all", action="store_true")
    fetch.add_argument("--parse", action="store_true", help="Run the baseline converter after verified ingestion")
    attach = actions.add_parser("import-pdf", help="Verify a user-supplied PDF against one candidate")
    attach.add_argument("--paper", required=True)
    attach.add_argument("--pdf", type=Path, required=True)
    attach.add_argument("--parse", action="store_true")
    inbox = actions.add_parser("import-inbox", help="Import exact paper_id.pdf filenames; leave unknown files alone")
    inbox.add_argument("--inbox", type=Path, required=True)
    inbox.add_argument("--parse", action="store_true")
    convert = actions.add_parser("convert", help="Convert an already acquired, identity-checked original")
    convert.add_argument("--paper", required=True)
    convert.add_argument("--backend", choices=["baseline", "grobid"], default="baseline")
    convert.add_argument("--endpoint")
    pending = actions.add_parser("pending", help="Export DOI/download and identity-review tasks to a new folder")
    pending.add_argument("--out", required=True, type=Path)
    actions.add_parser("audit", help="Check the acquisition ledger, files and evidence-store bindings")


def run_papers(args, store):
    from . import literature
    from .acquisition import AcquisitionQueue

    queue = AcquisitionQueue(args.queue, store)
    action = args.paper_action
    if action == "discover":
        if not 1 <= args.max_results <= 1000:
            raise ValueError("--max-results must be in 1..1000 per provider")
        providers = ["europepmc", "crossref"] if args.provider == "both" else [args.provider]
        records, searches, errors = [], [], []
        for provider in providers:
            try:
                method = literature.search_europe_pmc if provider == "europepmc" else literature.search_crossref
                result = method(args.query, max_results=args.max_results)
                records.extend(result["records"])
                searches.append({"provider": provider, **result["provenance"]})
                errors.extend({"provider": provider, **error} for error in result["provenance"].get("errors", []))
            except (ValueError, OSError) as exc:
                errors.append({"provider": provider, "error": str(exc)})
        result = queue.register(records, provenance={"kind": "metadata_search", "query": args.query,
                                "searches": searches, "errors": errors, "scope_complete": False})
        return {**result, "searches": searches, "errors": errors, "fulltext_search_performed": False}, 2 if errors else 0
    if action == "resolve":
        records, errors = [], []
        for value in args.doi:
            doi = literature.normalize_doi(value)
            try:
                record = literature.resolve_doi(doi)
                records.append(record)
                if record.get("errors") or record.get("metadata_status") == "unresolved":
                    errors.append({"doi": doi, "metadata_status": record.get("metadata_status"),
                                   "provider_errors": record.get("errors", [])})
            except (ValueError, OSError) as exc:
                records.append({"doi": doi, "title": None, "oa_locations": []})
                errors.append({"doi": doi, "error": str(exc)})
        result = queue.register(records, provenance={"kind": "doi_lookup", "dois": args.doi, "errors": errors})
        return {**result, "errors": errors}, 2 if errors else 0
    if action == "register":
        data = args.manifest.read_bytes()
        payload = json.loads(data.decode("utf-8-sig"))
        records = payload if isinstance(payload, list) else payload["records"]
        provenance = payload.get("provenance", {}) if isinstance(payload, dict) else {}
        return queue.register(records, provenance={"kind": "metadata_manifest", "input": str(args.manifest.resolve()),
                              "sha256": hashlib.sha256(data).hexdigest(), "provided": provenance}), 0
    if action == "list":
        return {"papers": queue.list()}, 0
    if action == "pending":
        return queue.export_pending(args.out), 0
    if action == "audit":
        result = queue.audit()
        return result, 0 if result.get("ok") else 2
    if action == "convert":
        result = queue.convert(args.paper, backend=args.backend, endpoint=args.endpoint)
        return result, 0 if result.get("parsing_status") == "completed" and not result.get("last_failure") else 2

    def with_parse(state):
        if args.parse and state.get("availability") == "available" and not state.get("last_failure"):
            return queue.convert(state["paper_id"])
        return state

    if action == "import-pdf":
        result = with_parse(queue.attach(args.paper, args.pdf))
        success = result.get("availability") == "available" and not result.get("last_failure") and (
            not args.parse or result.get("parsing_status") == "completed")
        return result, 0 if success else 2
    if action == "fetch":
        identifiers = [args.paper] if args.paper else [p["paper_id"] for p in queue.list()]
        papers, errors = [], []
        for identifier in identifiers:
            try:
                papers.append(with_parse(queue.fetch(identifier)))
            except (ValueError, OSError) as exc:
                errors.append({"paper_id": identifier, "error": str(exc)})
        pending = any(p.get("availability") != "available" or p.get("last_failure") or
                      (args.parse and p.get("parsing_status") != "completed") for p in papers)
        return {"papers": papers, "errors": errors}, 2 if pending or errors else 0
    if not args.inbox.is_dir():
        raise ValueError("--inbox must be an existing directory")
    known = {p["paper_id"]: p for p in queue.list()}
    results, unknown, errors = [], [], []
    for path in sorted(args.inbox.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        identifier = path.stem
        if identifier not in known:
            unknown.append(path.name)
            continue
        try:
            results.append(with_parse(queue.attach(identifier, path)))
        except (ValueError, OSError) as exc:
            errors.append({"file": path.name, "error": str(exc)})
    pending = any(p.get("availability") != "available" or p.get("last_failure") or
                  (args.parse and p.get("parsing_status") != "completed") for p in results)
    return {"papers": results, "unknown_files_untouched": unknown, "errors": errors}, 2 if pending or errors or unknown else 0
