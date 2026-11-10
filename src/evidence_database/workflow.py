"""A bounded project workflow over acquisition and immutable evidence versions."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .acquisition import AcquisitionQueue
from .store import EvidenceError, Store


def load_project(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    config = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(config, dict) or config.get("schema") != 1:
        raise EvidenceError("Project requires schema=1")
    if set(config) - {"schema", "project_id", "store", "queue", "inbox", "runs", "papers"}:
        raise EvidenceError("Unknown project configuration field")
    if not isinstance(config.get("project_id"), str) or not config["project_id"].strip():
        raise EvidenceError("Project requires project_id")
    paths = {}
    for name in ("store", "queue", "inbox", "runs"):
        value = config.get(name)
        if not isinstance(value, str) or not value.strip():
            raise EvidenceError(f"Project requires {name} path")
        paths[name] = (path.parent / value).resolve()
    values = list(paths.values())
    if any(a == b or a in b.parents or b in a.parents for i, a in enumerate(values) for b in values[i+1:]):
        raise EvidenceError("Store, queue, inbox and runs must be separate, non-nested directories")
    if any(path == target or target in path.parents for target in values):
        raise EvidenceError("Configuration must be outside managed store/queue/inbox/runs directories")
    papers = config.get("papers")
    if not isinstance(papers, list) or not papers:
        raise EvidenceError("Project requires a nonempty papers list")
    aliases = set()
    for paper in papers:
        if not isinstance(paper, dict) or set(paper) - {"metadata", "file", "sha256", "conversion_id"}:
            raise EvidenceError("Each paper requires metadata and optional file/sha256/conversion_id")
        metadata = AcquisitionQueue._normalize(paper.get("metadata"))
        keys = {(metadata["role"], metadata.get("attachment_id"), alias)
                for alias in AcquisitionQueue._aliases(metadata)}
        if keys & aliases:
            raise EvidenceError("Duplicate publication in project; reconcile metadata into one entry")
        aliases.update(keys)
        paper["metadata"] = metadata
        if "file" in paper:
            if not isinstance(paper["file"], str) or not paper["file"].strip():
                raise EvidenceError("file must be a nonempty path")
            paper["file"] = (path.parent / paper["file"]).resolve()
        if paper.get("sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}", str(paper["sha256"])):
            raise EvidenceError("Expected file SHA256 must be 64 lowercase hexadecimal characters")
        if paper.get("sha256") and "file" not in paper:
            raise EvidenceError("Expected SHA256 requires an explicit file")
        if paper.get("conversion_id") is not None and not re.fullmatch(r"cv_[0-9a-f]{64}", str(paper["conversion_id"])):
            raise EvidenceError("Invalid explicit conversion ID")
    return {**config, **paths, "config_path": path, "config_sha256": hashlib.sha256(raw).hexdigest()}


def _selected(queue, config):
    states = queue.list()
    selected = []
    for paper in config["papers"]:
        metadata = paper["metadata"]
        aliases = AcquisitionQueue._aliases(metadata)
        matches = [s for s in states if AcquisitionQueue._same_role(s["metadata"], metadata)
                   and aliases.intersection(s["aliases"])]
        if len(matches) > 1:
            raise EvidenceError("Project identifiers match multiple candidates")
        selected.append(matches[0] if matches else None)
    return selected


def _summary(config, selected):
    rows = []
    for paper, state in zip(config["papers"], selected, strict=True):
        rows.append({"doi": paper["metadata"].get("doi"), "pmid": paper["metadata"].get("pmid"),
                     "title": paper["metadata"].get("title"),
                     **({key: state.get(key) for key in ("paper_id", "availability", "identity_status",
                        "source_id", "conversion_id", "parsing_status", "fulltext_searchable", "last_failure")}
                        if state else {"availability": "not_registered", "fulltext_searchable": False})})
    return {"project_id": config["project_id"], "config_sha256": config["config_sha256"],
            "store": str(config["store"]), "queue": str(config["queue"]), "papers": rows,
            "counts": {"declared": len(rows), "available": sum(s["availability"] == "available" for s in rows),
                       "searchable": sum(bool(s["fulltext_searchable"]) for s in rows),
                       "identity_pending": sum(s["availability"] == "identity_review_required" for s in rows)},
            "scientifically_verified": False, "human_approvals_created": 0,
            "coverage": "Declared project papers only; no exhaustive literature coverage claim"}


def project_status(path):
    config = load_project(path)
    if not (config["queue"] / "catalog.sqlite3").is_file():
        return _summary(config, [None] * len(config["papers"]))
    queue = AcquisitionQueue(config["queue"], Store(config["store"], read_only=True), read_only=True)
    return _summary(config, _selected(queue, config))


def sync_project(path, *, fetch=False, parse=True):
    """Register, optionally fetch, intake explicit files/inbox, then reuse/convert.

    No implicit network, version preference, human review, deletion, or overwrite.
    A repeated sync retains selected successful/partial conversions.
    """
    config = load_project(path)
    store = Store(config["store"])
    queue = AcquisitionQueue(config["queue"], store)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    out = config["runs"] / run_id
    out.mkdir(parents=True, exist_ok=False)
    config["inbox"].mkdir(parents=True, exist_ok=True)
    errors, operations = [], []
    queue.register([p["metadata"] for p in config["papers"]], provenance={
        "kind": "project_workflow", "project_id": config["project_id"], "config_sha256": config["config_sha256"],
        "run_id": run_id, "network_requested": fetch})
    selected = _selected(queue, config)
    identifiers = {s["paper_id"] for s in selected}
    for paper, initial in zip(config["papers"], selected, strict=True):
        identifier = initial["paper_id"]
        state = initial
        try:
            if fetch and state["availability"] != "available":
                state = queue.fetch(identifier)
                operations.append({"paper_id": identifier, "action": "fetch", "failure": state["last_failure"]})
            candidates = []
            if paper.get("file"):
                candidates.append((paper["file"], paper.get("sha256")))
            inbox_file = config["inbox"] / (identifier + ".pdf")
            if inbox_file.is_file() and all(inbox_file != candidate[0] for candidate in candidates):
                candidates.append((inbox_file, None))
            current_file_failed = False
            for source, expected_sha in candidates:
                if source.stat().st_size > 50 * 1024 * 1024:
                    raise EvidenceError("Project input exceeds 50 MiB")
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                if expected_sha and digest != expected_sha:
                    raise EvidenceError("Project input differs from pinned SHA256")
                if (state.get("source_id") and state.get("last_failure") in (None, "conversion_failed")
                        and store.source(state["source_id"])["sha256"] == digest):
                    operations.append({"paper_id": identifier, "action": "reuse_original", "sha256": digest})
                    continue
                state = queue.attach(identifier, source)
                current_file_failed = bool(state["last_failure"])
                operations.append({"paper_id": identifier, "action": "intake", "failure": state["last_failure"]})
            if current_file_failed or state["availability"] != "available":
                continue
            if paper.get("conversion_id"):
                state = queue.bind_conversion(identifier, paper["conversion_id"])
                operations.append({"paper_id": identifier, "action": "reuse_pinned_conversion",
                                   "conversion_id": state["conversion_id"]})
            elif parse and not state["fulltext_searchable"]:
                state = queue.convert(identifier)
                operations.append({"paper_id": identifier, "action": "convert", "status": state["parsing_status"]})
        except (OSError, ValueError) as exc:
            errors.append({"paper_id": identifier, "error": str(exc)})
    unknown = [p.name for p in sorted(config["inbox"].iterdir())
               if p.is_file() and p.suffix.lower() == ".pdf" and p.stem not in identifiers]
    result = {**_summary(config, _selected(queue, config)), "run_id": run_id, "run_directory": str(out),
              "network_requested": fetch, "errors": errors, "operations": operations,
              "unknown_inbox_files_untouched": unknown,
              "pending": queue.export_pending(out / "pending", paper_ids=identifiers)}
    result["needs_attention"] = bool(errors or unknown or any(
        s["availability"] != "available" or not s["fulltext_searchable"] or
        s.get("parsing_status") != "completed" or s.get("last_failure") for s in result["papers"]))
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def add_project_parser(commands):
    command = commands.add_parser("project", help="Resume a configured project acquisition and evidence workflow")
    command.add_argument("--config", type=Path, required=True)
    steps = command.add_subparsers(dest="project_action", required=True)
    steps.add_parser("status", help="Report declared papers, acquired originals and pinned conversions")
    sync = steps.add_parser("sync", help="Register papers, intake files and continue parsing with an immutable run receipt")
    sync.add_argument("--fetch", action="store_true", help="Explicitly permit free-PDF network acquisition")
    sync.add_argument("--no-parse", action="store_true", help="Acquire files without launching new conversions")
