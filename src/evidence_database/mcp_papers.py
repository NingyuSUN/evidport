"""Bounded, SDK-independent paper operations for a local EvidPort MCP server.

The configured project fixes all filesystem roots. Metadata discovery and file
identity checks never approve quotations or claim exhaustive scientific coverage.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import quote

from . import literature
from .acquisition import AcquisitionQueue
from .store import EvidenceError, Store
from .workflow import load_project

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_RESULTS = 20


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_RESULTS:
        raise EvidenceError("limit must be an integer from 1 to 20")
    return value


def _text(value, maximum=600):
    text = str(value or "")
    return text[:maximum]


def _paper_id(metadata):
    aliases = AcquisitionQueue._aliases(metadata)
    key = ("doi:" + metadata["doi"] if metadata.get("doi") else
           "pmid:" + metadata["pmid"] if metadata.get("pmid") else min(aliases))
    material = key + "|" + metadata["role"] + "|" + metadata.get("attachment_id", "")
    return "paper_" + hashlib.sha256(material.encode()).hexdigest()


def _virtual(metadata):
    return {"paper_id": _paper_id(metadata), "metadata": metadata,
            "aliases": sorted(AcquisitionQueue._aliases(metadata)), "availability": "not_registered",
            "identity_status": "not_checked", "parsing_status": "not_started", "source_id": None,
            "conversion_id": None, "fulltext_searchable": False, "last_failure": None,
            "latest_event_id": None}


def _failure_code(value):
    # Core errors retain diagnostic paths and provider responses privately; tools
    # expose only their stable code/class rather than dumping arbitrary messages.
    if value is None:
        return None
    code = str(value).split(":", 1)[0]
    return code if re.fullmatch(r"[A-Za-z0-9_]{1,100}", code) else "acquisition_operation_failed"


def _row(state):
    metadata = state["metadata"]
    available = state.get("availability") == "available" and state.get("identity_status") == "verified"
    searchable = available and bool(state.get("fulltext_searchable"))
    status = ("evidence_available" if searchable else "conversion_required" if available else
              "identity_review_required" if state.get("identity_status") == "pending" else "acquisition_required")
    doi = metadata.get("doi")
    return {"paper_id": state["paper_id"], "title": _text(metadata.get("title")),
            "doi": doi, "doi_url": "https://doi.org/" + quote(doi, safe="/") if doi else None,
            "pmid": metadata.get("pmid"), "pmcid": metadata.get("pmcid"),
            "document_id": metadata.get("document_id") or state["paper_id"], "role": metadata["role"],
            "status": status, "availability": state["availability"],
            "identity_status": state["identity_status"], "parsing_status": state["parsing_status"],
            "source_id": state.get("source_id") if available else None,
            "conversion_id": state.get("conversion_id") if searchable else None,
            "fulltext_searchable": searchable, "last_failure": _failure_code(state.get("last_failure")),
            "needs_attention": bool(state.get("last_failure")) or not searchable
                               or state.get("parsing_status") == "partial",
            "free_pdf_candidates": sum(isinstance(item, dict) and item.get("eligibility") in
                                       {"open_access", "free_access"}
                                       for item in metadata.get("oa_locations", [])),
            "expected_inbox_filename": state["paper_id"] + ".pdf",
            "next_action": ("inspect_acquisition_failure" if state.get("last_failure") else
                            "inspect_partial_conversion" if searchable and state.get("parsing_status") == "partial" else
                            "search_evidence" if searchable else "convert" if available else
                            "review_file_identity" if state.get("identity_status") == "pending" else
                            "obtain_pdf_and_import_from_inbox"),
            "human_approved": False, "scientifically_verified": False}


class PaperTools:
    """Use existing queue operations behind fixed project roots and compact results."""

    def __init__(self, project_config: Path, *, allow_network=False):
        if not isinstance(allow_network, bool):
            raise EvidenceError("allow_network must be a boolean startup setting")
        self.config = load_project(project_config)
        self.allow_network = allow_network

    def _roots(self):
        for key in ("store", "queue", "inbox", "runs"):
            if self.config[key].resolve() != self.config[key]:
                raise EvidenceError("Configured project root changed; restart with a verified configuration")
            if self.config[key].exists() and not self.config[key].is_dir():
                raise EvidenceError("Configured project roots must be directories")

    def _queue(self, *, writable=False):
        self._roots()
        for key, filename in (("store", "index.sqlite3"), ("queue", "catalog.sqlite3")):
            root, index = self.config[key], self.config[key] / filename
            if index.is_symlink() or (index.exists() and (not index.is_file()
                    or getattr(index.lstat(), "st_file_attributes", 0) & 0x400)):
                raise EvidenceError("Managed database must be an ordinary file, not a link or reparse point")
            if not index.exists() and root.exists() and any(root.iterdir()):
                raise EvidenceError("Existing managed directory is incomplete; restore its index before continuing")
        if not writable and not (self.config["queue"] / "catalog.sqlite3").is_file():
            return None
        return AcquisitionQueue(self.config["queue"], Store(self.config["store"], read_only=not writable),
                                read_only=not writable)

    def _catalog(self):
        queue = self._queue()
        states = queue.list() if queue else []
        for paper in self.config["papers"]:
            metadata = paper["metadata"]
            matches = self._matches(states, metadata)
            if not matches:
                states.append(_virtual(metadata))
        return sorted(states, key=lambda state: state["paper_id"])

    @staticmethod
    def _matches(states, metadata):
        aliases = AcquisitionQueue._aliases(metadata)
        matches = [state for state in states if AcquisitionQueue._same_role(state["metadata"], metadata)
                   and aliases.intersection(state["aliases"])]
        if len(matches) > 1:
            raise EvidenceError("Publication aliases match multiple candidates; reconcile the queue first")
        return matches

    def _register(self, records, provenance):
        records = [AcquisitionQueue._normalize(record) for record in records]
        if not records:
            return []
        queue = self._queue(writable=True)
        current = queue.list()
        changes = []
        for record in records:
            matches = self._matches(current, record)
            if not matches or AcquisitionQueue._merge(matches[0]["metadata"], record) != matches[0]["metadata"]:
                changes.append(record)
        if changes:
            queue.register(changes, provenance={"kind": "local_mcp_discovery",
                "project_id": self.config["project_id"], "config_sha256": self.config["config_sha256"],
                **provenance})
        current = queue.list()
        return list({self._matches(current, record)[0]["paper_id"]: self._matches(current, record)[0]
                     for record in records}.values())

    def _candidate(self, paper_id, *, writable=False):
        if not isinstance(paper_id, str) or not re.fullmatch(r"paper_[0-9a-f]{64}", paper_id):
            raise EvidenceError("Invalid acquisition paper ID")
        matches = [state for state in self._catalog() if state["paper_id"] == paper_id]
        if not matches:
            raise EvidenceError("Unknown paper ID; discover or register the DOI first")
        state = matches[0]
        if writable and state["availability"] == "not_registered":
            state = self._register([state["metadata"]], {"scope": "declared project metadata"})[0]
        return state

    def status(self):
        states = self._catalog()
        rows = [_row(state) for state in states]
        return {"project_id": self.config["project_id"], "network_enabled": self.allow_network,
                "counts": {"candidates": len(rows),
                           "available": sum(row["availability"] == "available" for row in rows),
                           "searchable": sum(row["fulltext_searchable"] for row in rows),
                           "pending": sum(row["needs_attention"] for row in rows)},
                "papers": rows[:MAX_RESULTS], "truncated": len(rows) > MAX_RESULTS,
                "coverage": "Local project and acquisition catalog only; not an exhaustive literature search",
                "human_approvals_created": 0, "scientifically_verified": False}

    def discover(self, query=None, dois=None, provider="europe_pmc", limit=10):
        limit = _limit(limit)
        if provider not in {"europe_pmc", "crossref"}:
            raise EvidenceError("provider must be europe_pmc or crossref")
        if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 1000):
            raise EvidenceError("query must contain 1 to 1000 characters")
        if query is not None and dois is not None:
            raise EvidenceError("Supply a query or DOI list, not both")
        if dois is not None:
            if (not isinstance(dois, list) or not dois or len(dois) > limit
                    or any(not isinstance(doi, str) or len(doi) > 500 for doi in dois)):
                raise EvidenceError("dois must be a nonempty list no longer than limit")
            requested = list(dict.fromkeys(literature.normalize_doi(doi) for doi in dois))
            states, records, remote_count = self._catalog(), [], 0
            for doi in requested:
                matches = [state for state in states if state["metadata"].get("doi") == doi
                           and state["metadata"]["role"] == "paper"]
                if len(matches) > 1:
                    raise EvidenceError("DOI matches multiple candidates; reconcile the queue first")
                if matches:
                    records.append(matches[0]["metadata"])
                elif self.allow_network:
                    records.append(literature.resolve_doi(doi))
                    remote_count += 1
                else:
                    records.append({"doi": doi, "title": "", "provider": "user_doi", "record_id": doi,
                                    "metadata_status": "unresolved", "oa_locations": []})
            rows = [_row(state) for state in self._register(records, {"scope": "explicit DOI list",
                         "requested_dois": requested, "network_requests_started": remote_count})]
            return {"papers": rows, "total": len(rows), "truncated": False,
                    "scope": "Explicit DOI list; cached metadata reused; unresolved DOI is only a pending record",
                    "network_used": remote_count > 0, "remote_doi_resolutions": remote_count,
                    "scientifically_verified": False}
        if query is not None and self.allow_network:
            search = literature.search_europe_pmc if provider == "europe_pmc" else literature.search_crossref
            result = search(query, max_results=limit)
            rows = [_row(state) for state in self._register(result["records"], result["provenance"])]
            provenance = result["provenance"]
            return {"papers": rows, "provider": provider, "query": query,
                    "total_hits": provenance.get("total_hits"), "truncated": bool(provenance.get("truncated")),
                    "provider_errors": [_text(error.get("type"), 100) for error in provenance.get("errors", [])],
                    "network_used": True, "scope": "Provider search candidates only; relevance and coverage unverified",
                    "scientifically_verified": False}
        states = self._catalog()
        if query is not None:
            words = query.casefold().split()
            states = [state for state in states if all(word in _json({key: state["metadata"].get(key)
                      for key in ("title", "doi", "pmid", "pmcid")}).casefold() for word in words)]
        return {"papers": [_row(state) for state in states[:limit]], "total": len(states),
                "truncated": len(states) > limit, "network_used": False, "provider": "local_catalog",
                "scope": "Local metadata only; no online query performed; no matches do not mean no literature",
                "scientifically_verified": False}

    def pending(self, limit=10, cursor=None):
        limit = _limit(limit)
        states = [state for state in self._catalog() if _row(state)["needs_attention"]]
        snapshot = hashlib.sha256(_json([self.config["config_sha256"],
                     [[state["paper_id"], state.get("latest_event_id")] for state in states]]).encode()).hexdigest()
        offset = 0
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError
                value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if set(value) != {"snapshot", "offset"} or value["snapshot"] != snapshot:
                    raise ValueError
                offset = value["offset"]
                if isinstance(offset, bool) or not isinstance(offset, int) or not 0 < offset < len(states):
                    raise ValueError
            except (ValueError, TypeError, KeyError, UnicodeError) as exc:
                raise EvidenceError("Invalid or stale pending cursor; restart without cursor") from exc
        end = offset + limit
        next_cursor = (base64.urlsafe_b64encode(_json({"snapshot": snapshot, "offset": end}).encode()).decode()
                       if end < len(states) else None)
        rows = [_row(state) for state in states[offset:end]]
        return {"papers": rows, "total": len(states), "next_cursor": next_cursor,
                "scope": "Missing full text, unresolved identity, conversion failures and partial conversions",
                "access_note": "Missing free full text does not establish that a paper is paywalled",
                "scientifically_verified": False}

    def _read_inbox_pdf(self, filename):
        if (not isinstance(filename, str) or not filename or len(filename) > 180
                or any(char in filename for char in '/\\:\x00') or filename[-1] in ' .'
                or any(ord(char) < 32 or char in '<>?"*|' for char in filename)
                or Path(filename).suffix.casefold() != ".pdf"
                or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", filename, re.IGNORECASE)):
            raise EvidenceError("Supply a direct PDF filename inside the configured inbox, not a path")
        self._roots()
        inbox, path = self.config["inbox"], self.config["inbox"] / filename
        try:
            before = path.lstat()
            if (path.is_symlink() or getattr(before, "st_file_attributes", 0) & 0x400
                    or not stat.S_ISREG(before.st_mode) or path.resolve().parent != inbox):
                raise EvidenceError("Inbox PDF must be an ordinary file, not a link or reparse point")
            if before.st_size > MAX_FILE_BYTES:
                raise EvidenceError("Inbox PDF exceeds the 50 MiB limit")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(path, flags), "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise EvidenceError("Inbox file changed while opening; retry after the download completes")
                data = handle.read(MAX_FILE_BYTES + 1)
                after = os.fstat(handle.fileno())
            if len(data) > MAX_FILE_BYTES:
                raise EvidenceError("Inbox PDF exceeds the 50 MiB limit")
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise EvidenceError("Inbox file changed while reading; retry after the download completes")
            return data
        except OSError as exc:
            raise EvidenceError("Inbox PDF is unavailable; finish copying it into the configured inbox") from exc

    def import_file(self, paper_id, filename):
        data = self._read_inbox_pdf(filename)
        self._candidate(paper_id, writable=True)
        queue = self._queue(writable=True)
        state = queue.current(paper_id)
        digest = hashlib.sha256(data).hexdigest()
        if (state.get("source_id") and not state.get("last_failure")
                and queue.store.source(state["source_id"])["sha256"] == digest):
            return {**_row(state), "operation": "reuse_original", "operation_succeeded": True}
        # Use the queue's existing byte intake/identity logic after opening a safe
        # descriptor. Calling attach(path) would reopen a potentially replaced link.
        state = queue._receive(state, data, origin={"method": "manual_file", "channel": "local_mcp_inbox",
                               "filename": filename, "input_path": str(self.config["inbox"] / filename)})
        return {**_row(state), "operation": "manual_import", "operation_succeeded": not bool(state["last_failure"])}

    def acquire(self, paper_id):
        state = self._candidate(paper_id)
        if state["availability"] == "available":
            return {**_row(state), "operation": "reuse_original", "network_used": False}
        if not self.allow_network:
            raise EvidenceError("Network is disabled; obtain the PDF manually and import its inbox filename")
        self._candidate(paper_id, writable=True)
        state = self._queue(writable=True).fetch(paper_id)
        return {**_row(state), "operation": "free_acquisition", "network_used": True,
                "operation_succeeded": not bool(state["last_failure"])}

    def convert(self, paper_id):
        state = self._candidate(paper_id)
        if state["availability"] != "available" or state["identity_status"] != "verified":
            raise EvidenceError("Acquire the correct full text and resolve file identity before conversion")
        queue = self._queue(writable=True)
        pinned = [paper["conversion_id"] for paper in self.config["papers"] if paper.get("conversion_id")
                  and self._matches([state], paper["metadata"])]
        if pinned:
            state = queue.bind_conversion(paper_id, pinned[0])
            operation = "reuse_pinned_conversion"
        elif state.get("fulltext_searchable") and state.get("conversion_id"):
            operation = "reuse_conversion"
        else:
            state = queue.convert(paper_id, backend="baseline")
            operation = "baseline_conversion"
        return {**_row(state), "operation": operation, "operation_succeeded": bool(state["fulltext_searchable"])}

    def selected_versions(self):
        """Read selection metadata; evidence retrieval separately verifies source bytes.

        Immutable record hashes, their index bindings and acquisition event chains
        are checked on every call. Selection is not an audit of every stored PDF
        or parser artifact; Store.get/search perform those evidence checks.
        """
        queue = self._queue()
        if queue is None:
            return {}
        source_ids = set(queue.store._ids("sources"))
        conversion_ids = set(queue.store._ids("conversions"))
        result = {}
        for state in queue._all_states().values():
            if state["availability"] != "available" or state["identity_status"] != "verified":
                continue
            source_id = state.get("source_id")
            if source_id not in source_ids:
                raise EvidenceError("Available paper is missing its original source record")
            source = queue.store._load("sources", source_id)
            current_id = state.get("conversion_id")
            current = None
            if current_id:
                if current_id not in conversion_ids:
                    raise EvidenceError("Paper conversion is missing from immutable records")
                current = queue.store._load("conversions", current_id)
                if current["source_id"] != source_id:
                    raise EvidenceError("Paper conversion belongs to another original")
            pinned = [paper["conversion_id"] for paper in self.config["papers"] if paper.get("conversion_id")
                      and self._matches([state], paper["metadata"])]
            conversion_id = pinned[0] if pinned else current_id
            if not pinned and not state.get("fulltext_searchable"):
                continue
            if not conversion_id:
                continue
            if conversion_id not in conversion_ids:
                raise EvidenceError("Selected conversion is missing from immutable records")
            conversion = current if conversion_id == current_id else queue.store._load("conversions", conversion_id)
            if conversion["source_id"] != source_id or conversion["status"] not in {"completed", "partial"}:
                raise EvidenceError("Selected conversion is failed or belongs to another original")
            if not conversion["blocks"]:
                continue
            document_id = source["document_id"]
            if document_id in result and result[document_id] != conversion_id:
                raise EvidenceError("Multiple selected versions for one document; reconcile configuration")
            result[document_id] = conversion_id
        return result
