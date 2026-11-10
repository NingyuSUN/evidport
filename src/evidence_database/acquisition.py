"""Append-only literature acquisition queue, separate from searchable full text.

Metadata discovery does not establish scientific relevance or full-text access.
PDF identity checks are conservative frontmatter checks, not human review.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import re
import sqlite3
import unicodedata
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from .store import EvidenceError


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _compact(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def _cover_evidence(text):
    """Require repository-specific front-sheet wording before looking behind a cover."""
    lowered = " ".join(text.casefold().split())
    repository = re.search(r"(?:institutional|public|university|research) repository", lowered)
    deposit = re.search(r"(?:deposit cover|cover (?:page|sheet)|document version|downloaded from)", lowered)
    research_database = "research database" in lowered and bool(re.search(
        r"\b(?:university|institutional|research institute)\b", lowered))
    article_section = re.search(r"(?im)^\s*(?:abstract|introduction|references|bibliography)\b", text)
    if article_section:
        return []
    if repository and deposit:
        return [repository.group(), deposit.group()]
    if research_database and deposit:
        return ["institutional research database", deposit.group()]
    return []


def _write_once(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(data)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != data:
            raise EvidenceError(f"Immutable acquisition record differs: {path}") from None


class AcquisitionQueue:
    """Immutable batches/events and bytes, plus a checked SQLite event index."""

    def __init__(self, root: Path, store, *, read_only=False):
        self.root, self.store = Path(root).resolve(), store
        self.read_only = read_only
        self.db_path = self.root / "catalog.sqlite3"
        if read_only:
            if not self.db_path.is_file() or self.db_path.is_symlink() or any(
                    not (self.root / folder).is_dir() for folder in ("batches", "events", "objects")):
                raise EvidenceError("Read-only acquisition queue requires an existing complete directory structure")
            with self.db_path.open("rb") as handle:
                header = handle.read(100)
            if not header.startswith(b"SQLite format 3\x00") or header[18:20] != b"\x01\x01":
                raise EvidenceError("Read-only acquisition queue requires SQLite DELETE journal mode")
            self._records("batches")
            self._all_states()
            return
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in ("batches", "events", "objects"):
            (self.root / folder).mkdir(exist_ok=True)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, "
                       "sequence INTEGER NOT NULL, body TEXT NOT NULL, UNIQUE(paper_id,sequence))")

    def _require_writable(self):
        if self.read_only:
            raise EvidenceError("Acquisition queue is read-only")

    def _blob(self, data):
        self._require_writable()
        digest = _sha(data)
        path = self.root / "objects" / digest[:2] / digest
        _write_once(path, data)
        return {"sha256": digest, "bytes": len(data), "path": str(path)}

    def _verify_blob(self, info):
        digest = info.get("sha256", "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise EvidenceError("Invalid acquisition object SHA")
        path = self.root / "objects" / digest[:2] / digest
        if path.is_symlink() or not path.is_file():
            raise EvidenceError("Missing or unsafe acquisition object")
        data = path.read_bytes()
        if _sha(data) != digest or len(data) != info["bytes"]:
            raise EvidenceError("Acquisition object integrity failure")
        return path

    def _records(self, kind):
        prefix = "batch_" if kind == "batches" else "event_"
        with closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True)) as db, db:
            db.execute("PRAGMA query_only=ON")
            indexed = dict(db.execute(f"SELECT id,body FROM {kind}"))
            bindings = ({row[0]: row[1:] for row in db.execute("SELECT id,paper_id,sequence FROM events")}
                        if kind == "events" else {})
        paths = list((self.root / kind).glob("*.json"))
        if {p.stem for p in paths} != set(indexed):
            raise EvidenceError(f"Acquisition {kind} index membership differs")
        result = []
        for path in paths:
            data = path.read_bytes()
            if path.is_symlink() or path.stem != prefix + _sha(data):
                raise EvidenceError(f"Acquisition {kind} record integrity failure")
            if data.decode("utf-8") != indexed[path.stem]:
                raise EvidenceError(f"Acquisition {kind} index content differs")
            item = json.loads(data)
            if kind == "events" and bindings[path.stem] != (item["paper_id"], item["sequence"]):
                raise EvidenceError("Acquisition event index binding differs")
            result.append({**item, "record_id": path.stem})
        return result

    def _all_states(self):
        events = self._records("events")
        states, previous, sequences = {}, {}, {}
        for event in sorted(events, key=lambda e: (e["paper_id"], e["sequence"])):
            paper_id = event["paper_id"]
            if event["previous_event_id"] != previous.get(paper_id) or event["sequence"] != sequences.get(paper_id, 0) + 1:
                raise EvidenceError("Acquisition event chain differs")
            previous[paper_id], sequences[paper_id] = event["record_id"], event["sequence"]
            states[paper_id] = {**event["state"], "latest_event_id": event["record_id"]}
        return states

    def current(self, paper_id):
        if not isinstance(paper_id, str) or not re.fullmatch(r"paper_[0-9a-f]{64}", paper_id):
            raise EvidenceError("Invalid acquisition paper ID")
        states = self._all_states()
        if paper_id not in states:
            raise EvidenceError("Unknown acquisition paper ID")
        state = states[paper_id]
        if state.get("source_id"):
            self.store.source(state["source_id"])
        if state.get("conversion_id"):
            conversion = self.store.conversion(state["conversion_id"])
            if conversion["source_id"] != state["source_id"]:
                raise EvidenceError("Acquisition conversion source differs")
        return state

    def list(self):
        return [self.current(key) for key in sorted(self._all_states())]

    def _append(self, state, event, details):
        self._require_writable()
        state = copy.deepcopy(state)
        expected_previous = state.pop("latest_event_id", None)
        stamp = datetime.now(UTC).isoformat()
        state["updated_at"] = stamp
        with closing(sqlite3.connect(self.db_path, timeout=30)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT id,sequence FROM events WHERE paper_id=? ORDER BY sequence DESC LIMIT 1",
                                  (state["paper_id"],)).fetchone()
            if (previous[0] if previous else None) != expected_previous:
                raise EvidenceError("Acquisition changed concurrently; reload before retrying")
            record = {"schema": 1, "paper_id": state["paper_id"], "timestamp": stamp,
                      "sequence": previous[1] + 1 if previous else 1,
                      "previous_event_id": expected_previous, "event": event, "details": details, "state": state}
            body = _json(record)
            event_id = "event_" + _sha(body.encode())
            _write_once(self.root / "events" / (event_id + ".json"), body.encode())
            db.execute("INSERT INTO events VALUES (?,?,?,?)", (event_id, state["paper_id"], record["sequence"], body))
        return {**state, "latest_event_id": event_id}

    @staticmethod
    def _normalize(record):
        from .literature import normalize_doi
        if not isinstance(record, dict):
            raise EvidenceError("Literature records must be JSON objects")
        record = json.loads(_json(record))
        if record.get("doi"):
            record["doi"] = normalize_doi(record["doi"])
        for key in ("pmid", "pmcid", "record_id", "provider"):
            if record.get(key) is not None:
                record[key] = str(record[key]).strip()
        if record.get("pmid") and not record["pmid"].isdigit():
            raise EvidenceError("PMID must contain digits only")
        role = record.get("role", "paper")
        if role == "primary":
            role = "paper"
        if role not in ("paper", "supplement"):
            raise EvidenceError("Acquisition role must be paper or supplement")
        record["role"] = role
        if role == "supplement" and (not isinstance(record.get("attachment_id"), str)
                                     or not record["attachment_id"].strip()):
            raise EvidenceError("Supplement candidates require an explicit attachment_id")
        if not (record.get("doi") or record.get("pmid") or (record.get("provider") and record.get("record_id"))):
            raise EvidenceError("Candidate needs DOI, PMID, or provider + record_id; never invent a DOI")
        if record.get("title") is not None and not isinstance(record["title"], str):
            raise EvidenceError("Candidate title must be text")
        if not isinstance(record.get("oa_locations", []), list):
            raise EvidenceError("oa_locations must be a list")
        return record

    @staticmethod
    def _aliases(record):
        aliases = set()
        if record.get("doi"):
            aliases.add("doi:" + record["doi"])
        if record.get("pmid"):
            aliases.add("pmid:" + record["pmid"])
        if record.get("provider") and record.get("record_id"):
            aliases.add("provider:" + record["provider"] + ":" + record["record_id"])
        return aliases

    @staticmethod
    def _same_role(left, right):
        return (left["role"], left.get("attachment_id")) == (right["role"], right.get("attachment_id"))

    @staticmethod
    def _merge(old, new):
        for key in ("doi", "pmid"):
            if old.get(key) and new.get(key) and old[key] != new[key]:
                raise EvidenceError(f"Conflicting {key} metadata; candidate merge refused")
        merged = copy.deepcopy(old)
        for key, value in new.items():
            if not merged.get(key):
                merged[key] = value
        merged["oa_locations"] = list({_json(x): x for x in
                                       old.get("oa_locations", []) + new.get("oa_locations", [])}.values())
        return merged

    def register(self, records, *, provenance):
        self._require_writable()
        if not isinstance(records, list) or not isinstance(provenance, dict):
            raise EvidenceError("Registration requires records list and provenance object")
        records = [self._normalize(record) for record in records]
        batch = {"schema": 1, "timestamp": datetime.now(UTC).isoformat(), "records": records,
                 "provenance": provenance, "scientific_inclusion_decided": False}
        body = _json(batch)
        batch_id = "batch_" + _sha(body.encode())
        _write_once(self.root / "batches" / (batch_id + ".json"), body.encode())
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT OR IGNORE INTO batches VALUES (?,?)", (batch_id, body))
        selected = {}
        for record in records:
            states = self._all_states()
            aliases = self._aliases(record)
            matches = [state for state in states.values() if self._same_role(state["metadata"], record)
                       and aliases.intersection(state["aliases"])]
            if len(matches) > 1:
                raise EvidenceError("Identifier aliases connect multiple candidates; explicit reconciliation required")
            if matches:
                state = matches[0]
                merged = self._merge(state["metadata"], record)
                if state.get("source_id") and merged.get("doi") != state["metadata"].get("doi"):
                    source = self.store.source(state["source_id"])
                    checked = self._inspect(Path(source["path"]).read_bytes(), merged)
                    if checked["status"] != "verified":
                        raise EvidenceError("New DOI metadata conflicts with verified PDF; reconciliation required")
                state["metadata"] = merged
                state["aliases"] = sorted(set(state["aliases"]) | aliases)
            else:
                key = ("doi:" + record["doi"] if record.get("doi") else
                       "pmid:" + record["pmid"] if record.get("pmid") else min(aliases))
                paper_id = "paper_" + _sha((key + "|" + record["role"] + "|" + record.get("attachment_id", "")).encode())
                state = {"paper_id": paper_id, "metadata": record, "aliases": sorted(aliases),
                         "availability": "metadata_only", "identity_status": "not_checked",
                         "parsing_status": "not_started", "source_id": None, "conversion_id": None,
                         "source_ids": [], "conversion_ids": [], "attachments": [],
                         "last_failure": None, "failures": [], "fulltext_searchable": False,
                         "scientific_inclusion_decided": False, "human_approved": False}
            state = self._append(state, "registered", {"batch_id": batch_id, "metadata_record": record})
            selected[state["paper_id"]] = state
        return {"batch_id": batch_id, "input_count": len(records), "count": len(selected),
                "papers": list(selected.values())}

    def _failure(self, state, event, reason, details=None):
        if not state.get("source_id"):
            pending = any(a["identity"]["status"] == "pending" for a in state["attachments"])
            state["availability"] = "identity_review_required" if pending else "waiting_user"
            state["identity_status"] = "pending" if pending else "not_checked"
        state["last_failure"] = reason
        state.setdefault("failures", []).append({"event": event, "reason": reason,
                                                  "timestamp": datetime.now(UTC).isoformat()})
        return self._append(state, event, {"reason": reason, **(details or {})})

    def _inspect(self, data, metadata):
        identity = {"status": "rejected", "reason": "invalid_pdf_header", "visual_review_performed": False}
        if not data.startswith(b"%PDF-"):
            return identity
        try:
            import pymupdf
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                if doc.needs_pass or not len(doc):
                    return {**identity, "reason": "encrypted_or_empty_pdf"}
                texts = [page.get_text() for page in doc]
                first = texts[0]
                identity.update(pages=len(doc), text_chars_by_page=[len(text) for text in texts],
                                first_page_text_chars=len(first), pdf_repaired=doc.is_repaired)
                png = doc[0].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False).tobytes("png")
                identity["first_page_image"] = self._blob(png)
        except Exception as exc:  # noqa: BLE001 - retain PDF backend errors without inventing acquisition success.
            return {**identity, "reason": f"pdf_parse_failed:{type(exc).__name__}:{exc}"}
        if max(len(_compact(text)) for text in texts[:3]) < 30:
            return {**identity, "status": "pending", "reason": "scanned_or_low_text_identity_requires_review"}
        fronts = []
        for text in texts[:3]:
            parts = re.split(
                r"(?im)^[ \t]*(?:(?:\d+(?:\.\d+)*|[ivxlcdm]+)[.)]?[ \t]+)?"
                r"(?:references(?:[ \t]+(?:and|&)[ \t]+notes)?|bibliography|"
                r"literature[ \t]+cited|works[ \t]+cited)\b[^\n]*", text, maxsplit=1)
            fronts.append(parts[0][:6000])
            if len(parts) > 1:
                break
        title = metadata.get("title") or ""
        strong_title = len(_compact(title)) >= 40 and len(title.split()) >= 6
        doi = metadata.get("doi")
        checks = []
        for number, front in enumerate(fronts, 1):
            found = sorted({s.rstrip(".,;").casefold() for s in
                            re.findall(r"10\.\d{4,9}/[^\s<>\"]+", front, flags=re.IGNORECASE)})
            # A trailing sentence parenthesis is permitted only if the DOI itself has no opening parenthesis.
            found = [s.rstrip(")") if "(" not in s else s for s in found]
            checks.append({"page": number, "title_match": bool(strong_title and _compact(title) in _compact(front)),
                           "doi_match": bool(doi and doi in found), "dois": found,
                           "cover_evidence": _cover_evidence(front)})
        supplement = any(re.search(
            r"(?im)^\s*(?:supporting (?:information|online material|material|data)|"
            r"supplement(?:ary|al)? (?:material|information|data|appendix))", front[:1800]) for front in fronts)
        matched = next((check for check in checks if check["title_match"] and
                        (check["page"] == 1 or (check["doi_match"] and
                         all(previous["cover_evidence"] for previous in checks[:check["page"] - 1])))), checks[0])
        found, doi_match, title_match = matched["dois"], matched["doi_match"], matched["title_match"]
        identity.update(title_match=title_match, doi_match=doi_match, front_page_dois=checks[0]["dois"],
                        inspected_frontmatter_pages=checks, identity_page=matched["page"],
                        supplementary_frontmatter=supplement, expected_doi=doi,
                        method="first_page_or_explicit_repository_cover_then_same_page_title_and_doi",
                        identity_rule_version=2)
        if metadata["role"] == "paper" and supplement:
            return {**identity, "reason": "supplement_cannot_replace_primary_paper"}
        if metadata["role"] == "supplement":
            return {**identity, "status": "pending", "reason": "supplement_identity_requires_attachment_review"}
        if doi and any(value != doi for value in checks[0]["dois"] + found):
            return {**identity, "reason": "front_page_doi_mismatch"}
        if not title_match or (matched["page"] != 1 and not doi_match):
            return {**identity, "status": "pending", "reason": "title_identity_requires_review"}
        return {**identity, "status": "verified", "reason": "same_page_title_and_doi_match" if doi_match
                else "exact_long_title_match_no_conflicting_front_page_doi",
                "limitation": "Automated file identity check only; neither human review nor full transcription validation"}

    def _receive(self, state, data, *, origin):
        if not isinstance(data, bytes) or len(data) > 50 * 1024 * 1024:
            return self._failure(state, "file_rejected", "file_exceeds_50_mib_or_invalid_bytes", origin)
        obj = self._blob(data)
        identity = self._inspect(data, state["metadata"])
        attachment = {"object": obj, "identity": identity, "origin": origin,
                      "timestamp": datetime.now(UTC).isoformat()}
        state["attachments"].append(attachment)
        if identity["status"] != "verified":
            return self._failure(state, "file_identity_not_verified", identity["reason"], attachment)
        document_id = state["metadata"].get("document_id") or state["paper_id"]
        ingested = self.store.ingest(Path(obj["path"]), document_id, role=state["metadata"]["role"],
                                     metadata={"acquisition_paper_id": state["paper_id"],
                                               "literature": state["metadata"], "identity_check": identity,
                                               "acquisition": origin})
        changed = state.get("source_id") != ingested["source_id"]
        state.update(availability="available", identity_status="verified", source_id=ingested["source_id"],
                     last_failure=None)
        if ingested["source_id"] not in state["source_ids"]:
            state["source_ids"].append(ingested["source_id"])
        if changed:
            state.update(parsing_status="not_started", conversion_id=None, fulltext_searchable=False)
        return self._append(state, "file_attached", {**attachment, "source_id": ingested["source_id"],
                                                    "ingestion_id": ingested["ingestion_id"]})

    def attach(self, paper_id, path: Path):
        self._require_writable()
        state = self.current(paper_id)
        path = Path(path)
        try:
            if path.stat().st_size > 50 * 1024 * 1024:
                return self._failure(state, "file_rejected", "file_exceeds_50_mib", {"input_path": str(path)})
            data = path.read_bytes()
        except OSError as exc:
            return self._failure(state, "file_unreadable", f"{type(exc).__name__}:{exc}", {"input_path": str(path)})
        return self._receive(state, data, origin={"method": "manual_file", "input_path": str(path.resolve()),
                                                  "filename": path.name})

    def fetch(self, paper_id):
        self._require_writable()
        from . import literature
        state = self.current(paper_id)
        if state["availability"] == "available":
            return state
        if not state["metadata"].get("oa_locations") and state["metadata"].get("doi"):
            try:
                resolved = self._normalize(literature.resolve_doi(state["metadata"]["doi"]))
                state["metadata"] = self._merge(state["metadata"], resolved)
                state["aliases"] = sorted(set(state["aliases"]) | self._aliases(resolved))
                state = self._append(state, "metadata_resolved", {"metadata_record": resolved})
            except Exception as exc:  # noqa: BLE001 - retain provider outages separately from lack of access.
                state = self._failure(state, "metadata_resolution_failed", f"{type(exc).__name__}:{exc}")
        candidates = [x for x in state["metadata"].get("oa_locations", [])
                      if isinstance(x, dict) and x.get("url") and
                      x.get("eligibility") in ("open_access", "free_access")]
        if not candidates:
            return self._failure(state, "awaiting_user_file", "no_verified_free_pdf_location_found",
                                 {"access_conclusion": "Not proof of a paywall; source coverage or metadata may be incomplete"})
        for candidate in candidates:
            try:
                downloaded = literature.download_pdf(candidate["url"])
            except Exception as exc:  # noqa: BLE001 - retain download adapter failures per attempted location.
                state = self._failure(state, "download_failed", f"{type(exc).__name__}:{exc}",
                                      {"location": candidate})
                continue
            details = {k: v for k, v in downloaded.items() if k != "data"}
            state = self._receive(state, downloaded["data"], origin={"method": "free_download", "location": candidate,
                                                                      "receipt": details})
            if state["availability"] == "available":
                return state
        return state

    def convert(self, paper_id, backend="baseline", *, endpoint=None):
        self._require_writable()
        from .adapters import run_baseline, run_grobid
        state = self.current(paper_id)
        if state["availability"] != "available" or state["identity_status"] != "verified":
            raise EvidenceError("Full text is unavailable or identity unverified; acquire the correct PDF first")
        if backend not in ("baseline", "grobid"):
            raise EvidenceError("Acquisition conversion supports baseline or grobid")
        if backend == "grobid" and not endpoint:
            raise EvidenceError("GROBID requires an explicit endpoint")
        result = (run_baseline(self.store, state["source_id"]) if backend == "baseline"
                  else run_grobid(self.store, state["source_id"], endpoint=endpoint))
        state["last_conversion_attempt"] = {"conversion_id": result["conversion_id"], "status": result["status"]}
        if result["status"] != "failed" or not state["fulltext_searchable"]:
            state["parsing_status"] = result["status"]
            state["conversion_id"] = result["conversion_id"]
            state["fulltext_searchable"] = result["status"] != "failed"
        state["conversion_ids"].append(result["conversion_id"])
        state["last_failure"] = "conversion_failed" if result["status"] == "failed" else None
        return self._append(state, "converted", {"conversion_id": result["conversion_id"],
                                                  "status": result["status"], "issues": result["issues"]})

    def bind_conversion(self, paper_id, conversion_id):
        """Select an explicit existing version without reparsing or inheriting review."""
        self._require_writable()
        state = self.current(paper_id)
        if state["availability"] != "available" or state["identity_status"] != "verified":
            raise EvidenceError("Acquire and verify the original before binding a conversion")
        conversion = self.store.conversion(conversion_id)
        if conversion["source_id"] != state["source_id"] or conversion["status"] == "failed":
            raise EvidenceError("Selected conversion is failed or belongs to another original")
        if state.get("conversion_id") == conversion_id and not state.get("last_failure"):
            return state
        state.update(conversion_id=conversion_id, parsing_status=conversion["status"],
                     fulltext_searchable=bool(conversion["blocks"]), last_failure=None)
        if conversion_id not in state["conversion_ids"]:
            state["conversion_ids"].append(conversion_id)
        return self._append(state, "existing_conversion_selected", {"conversion_id": conversion_id,
                            "selection": "explicit_project_configuration", "human_approval_created": False})

    def export_pending(self, outdir: Path, *, paper_ids=None):
        self._require_writable()
        outdir = Path(outdir)
        if outdir.exists():
            raise EvidenceError("Pending export requires a new directory; old exports are preserved")
        states = [state for state in self.list() if state["availability"] != "available"
                  and (paper_ids is None or state["paper_id"] in paper_ids)]
        outdir.mkdir(parents=True, exist_ok=False)
        fields = ["paper_id", "action", "title", "doi", "doi_url", "pmid", "provider", "record_id", "role",
                  "reason", "suggested_filename", "retained_file"]
        rows = []
        for state in states:
            m = state["metadata"]
            pending = [a for a in state["attachments"] if a["identity"]["status"] == "pending"]
            rows.append({"paper_id": state["paper_id"], "action": "verify_downloaded_file" if pending else "need_pdf",
                         "title": m.get("title", ""), "doi": m.get("doi", ""),
                         "doi_url": "https://doi.org/" + quote(m["doi"], safe="/") if m.get("doi") else "",
                         "pmid": m.get("pmid", ""), "provider": m.get("provider", ""),
                         "record_id": m.get("record_id", ""), "role": m["role"],
                         "reason": state["last_failure"] or "full_text_not_acquired",
                         "suggested_filename": state["paper_id"] + ".pdf",
                         "retained_file": pending[-1]["object"]["path"] if pending else ""})
        for name, delimiter in (("pending.csv", ","), ("pending.tsv", "\t")):
            with (outdir / name).open("x", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, delimiter=delimiter)
                writer.writeheader()
                # Spreadsheet formula characters are neutralized in display exports only.
                writer.writerows({k: "'" + str(v) if str(v).startswith(("=", "+", "-", "@")) else v
                                  for k, v in row.items()} for row in rows)
        (outdir / "pending.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (outdir / "README.md").write_text(
            "# 待补全文\n\nneed_pdf：按DOI/PMID取得对应原件后，以paper_id明确关联导入。\n"
            "verify_downloaded_file：文件已保留，身份待核；不要只凭文件名或DOI出现在参考文献中认定。\n"
            "元数据没有全文引文，缺少全文不等于缺少科学证据。补入后仍需转换与原页复核。\n",
            encoding="utf-8")
        return {"directory": str(outdir.resolve()), "count": len(rows),
                "need_pdf": sum(row["action"] == "need_pdf" for row in rows),
                "verify_downloaded_file": sum(row["action"] == "verify_downloaded_file" for row in rows),
                "files": [str((outdir / name).resolve()) for name in ("pending.csv", "pending.tsv", "pending.json", "README.md")]}

    def audit(self):
        errors = []
        counts = {"batches": 0, "papers": 0, "events": 0, "objects": 0}
        try:
            counts["batches"] = len(self._records("batches"))
            events = self._records("events")
            counts["events"] = len(events)
            states = self._all_states()
            counts["papers"] = len(states)
            for state in states.values():
                for attachment in state["attachments"]:
                    self._verify_blob(attachment["object"])
                    if attachment["identity"].get("first_page_image"):
                        self._verify_blob(attachment["identity"]["first_page_image"])
                for source_id in state["source_ids"]:
                    self.store.source(source_id)
                for conversion_id in state["conversion_ids"]:
                    self.store.conversion(conversion_id)
            for path in (self.root / "objects").glob("*/*"):
                if path.is_file():
                    if path.is_symlink() or path.name != _sha(path.read_bytes()):
                        raise EvidenceError("Acquisition object hash differs")
                    counts["objects"] += 1
        except (EvidenceError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
        return {"ok": not errors, "errors": errors, "counts": counts,
                "metadata_is_fulltext": False, "scientifically_verified": False}
