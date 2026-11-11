"""Bounded, SDK-independent evidence tools over the existing integrity ledger.

MCP transport adapters may expose these operations, but never Store.review.
Read operations reopen Store in read-only mode. Only prepare_review creates a
new derived packet, and creating it never approves a quotation.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .packets import create_packet
from .store import EvidenceError, Store

MAX_TEXT = 20_000
MAX_QUERY = 1_000
MAX_PAGE_BYTES = 10 * 1024 * 1024
_CV = re.compile(r"cv_[0-9a-f]{64}\Z")
_SRC = re.compile(r"src_[0-9a-f]{64}\Z")
_PKT = re.compile(r"pkt_[0-9a-f]{64}\Z")
_BLOCK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_LOCAL_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|file://|/(?:home|Users|mnt|tmp|var|private)/)")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _required(value, label, maximum=MAX_QUERY):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise EvidenceError(f"{label} must be nonempty text of at most {maximum} characters")
    return value


def _limit(value):
    if type(value) is not int or not 1 <= value <= 20:
        raise EvidenceError("limit must be an integer from 1 to 20")
    return value


def _issues(values):
    """Keep concise diagnostics without exporting internal files or native records."""
    result = []
    for value in values[:20]:
        if isinstance(value, dict):
            kept = {}
            for key in ("code", "message", "reason", "severity", "status", "page", "block_id"):
                item = value.get(key)
                if isinstance(item, str):
                    kept[key] = "[local path omitted]" if _LOCAL_PATH.search(item) else item[:500]
                elif (type(item) in (int, bool) or item is None) and key in value:
                    kept[key] = item
            result.append(kept or {"code": "diagnostic_details_omitted"})
        elif isinstance(value, str):
            result.append("[local path omitted]" if _LOCAL_PATH.search(value) else value[:500])
        else:
            result.append("diagnostic_details_omitted")
    if len(values) > 20:
        result.append({"code": "additional_diagnostics", "message": f"{len(values)-20} details omitted"})
    return result


def _locator(locator):
    result = {key: value for key, value in locator.items() if key in {"page", "pages", "bbox", "unlocated"}}
    if "regions" in locator:
        result["regions"] = [{"page": item["page"], "bbox": item["bbox"]} for item in locator["regions"]]
    return result


def _pages(locator):
    return sorted(set(locator.get("pages", [])) | ({locator["page"]} if locator.get("page") else set())
                  | {region["page"] for region in locator.get("regions", [])})


class EvidenceTools:
    """Evidence retrieval with fixed roots, explicit versions and strict review checks."""

    def __init__(self, store_root: Path, review_root: Path, selected_versions: dict[str, str] | None = None):
        self.store_root = Path(store_root).resolve()
        self.review_root = Path(review_root).resolve()
        if (self.store_root == self.review_root or self.store_root in self.review_root.parents
                or self.review_root in self.store_root.parents):
            raise EvidenceError("Review output and evidence store must be separate, non-nested directories")
        self.selected_versions = dict(selected_versions or {})
        for key, version in self.selected_versions.items():
            _required(key, "selected document/source ID", 300)
            if not isinstance(version, str) or not _CV.fullmatch(version):
                raise EvidenceError("A selected version must be an exact cv_<64 lowercase hex> ID")
        if self.selected_versions:
            self._inventory(self._store())
        else:
            self._store()

    def _store(self):
        if self.store_root.resolve() != self.store_root:
            raise EvidenceError("Configured evidence root changed through a symlink")
        return Store(self.store_root, read_only=True)

    def _inventory(self, store):
        # Inventory validates canonical record bytes and their indexed copies.
        # Full original/native artifact checks remain mandatory for each returned
        # evidence item in Store.get; metadata lists do not claim those checks.
        conversions = [{**store._load("conversions", cid), "conversion_id": cid}
                       for cid in store._ids("conversions")]
        sources = {sid: {**store._load("sources", sid), "source_id": sid}
                   for sid in store._ids("sources")}
        if any(conversion["source_id"] not in sources for conversion in conversions):
            raise EvidenceError("Conversion inventory refers to a missing source")
        by_id = {conversion["conversion_id"]: conversion for conversion in conversions}
        for key, value in self.selected_versions.items():
            selected = by_id.get(value)
            if selected is None or selected["status"] == "failed":
                raise EvidenceError("A configured selected conversion is missing or failed")
            source = sources[selected["source_id"]]
            if key not in {source["document_id"], source["source_id"]}:
                raise EvidenceError("Selected conversion does not belong to its configured document/source")
        for source in sources.values():
            document_selected = self.selected_versions.get(source["document_id"])
            source_selected = self.selected_versions.get(source["source_id"])
            if document_selected and source_selected and document_selected != source_selected:
                raise EvidenceError("Document and source selections conflict")
        return conversions, sources

    def _selection(self, source):
        return self.selected_versions.get(source["source_id"], self.selected_versions.get(source["document_id"]))

    def _snapshot(self, store, conversions):
        return hashlib.sha256(_json({"versions": sorted(c["conversion_id"] for c in conversions),
            "reviews": store._ids("reviews"), "ingestions": store._ids("ingestions"),
            "selection": self.selected_versions}).encode()).hexdigest()

    @staticmethod
    def _offset(cursor, context, total):
        signature = hashlib.sha256(_json(context).encode()).hexdigest()
        if cursor is None:
            return 0, signature
        if not isinstance(cursor, str) or len(cursor) > 2048:
            raise EvidenceError("Invalid pagination cursor")
        try:
            data = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
        except (ValueError, UnicodeError) as exc:
            raise EvidenceError("Invalid pagination cursor") from exc
        if not isinstance(data, dict) or set(data) != {"signature", "offset"}:
            raise EvidenceError("Invalid pagination cursor")
        if data["signature"] != signature:
            raise EvidenceError("Cursor query, selection or evidence snapshot changed; restart pagination")
        offset = data["offset"]
        if type(offset) is not int or offset < 0 or offset >= total:
            raise EvidenceError("Pagination cursor offset is outside the result set")
        return offset, signature

    @staticmethod
    def _cursor(signature, offset, total):
        if offset >= total:
            return None
        return base64.urlsafe_b64encode(_json({"signature": signature, "offset": offset}).encode()).decode().rstrip("=")

    @staticmethod
    def _publication(store, source_id):
        records, sources = [], {}
        for receipt_id in store._ids("ingestions"):
            receipt = store._load("ingestions", receipt_id)
            sid = receipt["source_id"]
            if sid not in sources:
                sources[sid] = store._load("sources", sid)
            if any(receipt[key] != sources[sid][key] for key in ("document_id", "sha256", "size")):
                raise EvidenceError("Acquisition receipt/source binding differs")
            if sid != source_id:
                continue
            # The calling get/search has already validated this original and
            # conversion's full artifacts. Metadata retrieval preserves receipt
            # hashes/index/source binding without rereading every other PDF.
            metadata = receipt.get("metadata") or {}
            literature = metadata.get("literature")
            if isinstance(literature, dict):
                records.append(literature)
        result = {}
        conflicts = []
        for key in ("title", "doi", "pmid", "pmcid", "year"):
            values = sorted({str(record[key]) for record in records if record.get(key) is not None})
            if len(values) == 1:
                result[key] = values[0][:1000]
            elif len(values) > 1:
                result[key] = None
                conflicts.append(key)
        if conflicts:
            result["metadata_conflicts"] = conflicts
        return result

    def _item(self, store, item, publication=None, *, offset=0, maximum=MAX_TEXT):
        source, block = item["source"], item["block"]
        publication = self._publication(store, source["source_id"]) if publication is None else publication
        locator = _locator(block["locator"])
        text = block["text"]
        selected = self._selection(source)
        warnings = []
        if selected and selected != item["conversion_id"]:
            warnings.append("requested_version_differs_from_project_selection")
        resource = f"evidence://blocks/{item['conversion_id']}/{block['id']}"
        return {"citation": item["citation"], "source_id": source["source_id"],
                "source_sha256": source["sha256"], "document_id": source["document_id"],
                "source": {key: source[key] for key in ("source_id", "sha256", "document_id", "role",
                    "media_type", "pages") if key in source} | publication,
                "conversion_id": item["conversion_id"], "conversion_status": item["conversion_status"],
                "parser": item["parser"], "parser_version": item["parser_version"],
                "block_id": block["id"], "kind": block["kind"], "locator": locator,
                "locator_sha256": hashlib.sha256(_json(block["locator"]).encode()).hexdigest(),
                "location_accuracy": block.get("location_accuracy", "unlocated"),
                "text": text[offset:offset+maximum], "text_offset": offset, "text_characters": len(text),
                "text_truncated": offset > 0 or len(text) > offset + maximum,
                "next_text_resource_uri": (resource if maximum != MAX_TEXT else resource + f"/chunks/{offset+maximum}")
                    if len(text) > offset+maximum else None,
                "block_resource_uri": resource,
                "page_resource_uris": [f"evidence://sources/{source['source_id']}/pages/{page}"
                                       for page in _pages(locator)],
                "block_issues": _issues(block.get("issues", [])),
                "conversion_issues": _issues(item["conversion_issues"]),
                "review_status": item["review_status"], "selected_conversion_id": selected,
                "warnings": warnings, "scientifically_verified": False,
                "meaning": "Retrieval only; publication identity, quote-specific review and scientific support remain separate checks"}

    def search(self, query, limit=10, cursor=None, document_id=None, conversion_id=None):
        _required(query, "query")
        _limit(limit)
        if document_id is not None:
            _required(document_id, "document_id", 300)
        if conversion_id is not None and (not isinstance(conversion_id, str) or not _CV.fullmatch(conversion_id)):
            raise EvidenceError("conversion_id must identify an exact conversion")
        store = self._store()
        conversions, sources = self._inventory(store)
        by_id = {c["conversion_id"]: c for c in conversions}
        if conversion_id is not None and conversion_id not in by_id:
            raise EvidenceError("Requested conversion does not exist")
        if conversion_id is not None and by_id[conversion_id]["status"] == "failed":
            raise EvidenceError("Failed conversions are not searchable evidence")
        if conversion_id and document_id and sources[by_id[conversion_id]["source_id"]]["document_id"] != document_id:
            raise EvidenceError("Requested conversion does not belong to the requested document")
        eligible, groups = [], {}
        for conversion in conversions:
            source = sources[conversion["source_id"]]
            if conversion["status"] == "failed" or (document_id and source["document_id"] != document_id):
                continue
            selected = self._selection(source)
            if conversion_id and conversion["conversion_id"] != conversion_id:
                continue
            if not conversion_id and selected and conversion["conversion_id"] != selected:
                continue
            eligible.append(conversion["conversion_id"])
            groups.setdefault(source["document_id"], []).append(conversion["conversion_id"])
        # Reuse the full Store search integrity preflight. Its public method has a
        # 1000-hit cap, so do the paged read-only FTS query only after that check.
        store.search(query, limit=1)
        literal = '"' + query.replace('"', '""') + '"'
        with store._db() as db:
            rows = db.execute("SELECT citation FROM blocks_fts WHERE blocks_fts MATCH ? ORDER BY rank,citation",
                              (literal,)).fetchall()
        allowed = set(eligible)
        citations = [row[0] for row in rows if row[0].split("#", 1)[0][len("evidence:"):] in allowed]
        context = {"operation": "search", "query": query, "limit": limit, "document_id": document_id,
                   "conversion_id": conversion_id, "snapshot": self._snapshot(store, conversions)}
        offset, signature = self._offset(cursor, context, len(citations))
        items, publications = [], {}
        for citation in citations[offset:offset+limit]:
            item = store.get(citation)
            sid = item["source"]["source_id"]
            if sid not in publications:
                publications[sid] = self._publication(store, sid)
            rendered = self._item(store, item, publications[sid], maximum=600)
            rendered["excerpt"] = rendered.pop("text")
            rendered["excerpt_truncated"] = rendered.pop("text_truncated")
            rendered["version_selection"] = ("explicit" if conversion_id else "project_selected"
                if self._selection(item["source"]) else "unselected_multiple_versions"
                if len(groups[item["source"]["document_id"]]) > 1 else "only_available_version")
            items.append(rendered)
        ambiguous = sorted(key for key, values in groups.items() if len(values) > 1)
        return {"items": items, "returned": len(items), "total_matches": len(citations),
                "next_cursor": self._cursor(signature, offset+len(items), len(citations)),
                "snapshot": context["snapshot"], "query": query,
                "ordering": "FTS rank, then citation ID; not scientific strength",
                "version_warnings": [{"document_id": key, "available_version_count": len(groups[key]),
                                      "reason": "Select a specific conversion; newest is not automatically most accurate"}
                                     for key in ambiguous[:20]],
                "version_warnings_truncated": len(ambiguous) > 20,
                "inventory_verification": "Canonical metadata and index only; returned hits verify original and native artifacts",
                "scope": "Local archived full text only; no hits do not imply no relevant literature exists",
                "scientifically_verified": False}

    def get(self, citation):
        _required(citation, "citation", 300)
        store = self._store()
        return self._item(store, store.get(citation))

    def check_quote(self, citation, quote):
        _required(citation, "citation", 300)
        _required(quote, "quote", MAX_TEXT)
        store = self._store()
        checked = store.check(citation, quote, require_review=True)
        item = store.get(citation)
        return {**checked, "source_id": item["source"]["source_id"],
                "source_sha256": item["source"]["sha256"], "conversion_id": item["conversion_id"],
                "locator": _locator(item["block"]["locator"]),
                "locator_sha256": hashlib.sha256(_json(item["block"]["locator"]).encode()).hexdigest(),
                "block_issues": _issues(item["block"].get("issues", [])),
                "conversion_issues": _issues(item["conversion_issues"])}

    def versions(self, document_id, limit=10, cursor=None):
        _required(document_id, "document_id", 300)
        _limit(limit)
        store = self._store()
        conversions, sources = self._inventory(store)
        if document_id not in {source["document_id"] for source in sources.values()}:
            raise EvidenceError("Requested document does not exist in the evidence store")
        rows = sorted((c for c in conversions if sources[c["source_id"]]["document_id"] == document_id),
                      key=lambda c: c["conversion_id"])
        context = {"operation": "versions", "document_id": document_id, "limit": limit,
                   "snapshot": self._snapshot(store, conversions)}
        offset, signature = self._offset(cursor, context, len(rows))
        items = [{"conversion_id": c["conversion_id"], "source_id": c["source_id"],
                  "source_sha256": sources[c["source_id"]]["sha256"], "status": c["status"],
                  "parser": c["parser"], "parser_version": c["parser_version"],
                  "parent_conversion_id": c["settings"].get("parent_conversion_id"),
                  "block_count": len(c["blocks"]), "issues": _issues(c["issues"]),
                  "selected": c["conversion_id"] == self._selection(sources[c["source_id"]]),
                  "review_inherited": False,
                  "verification_scope": "Canonical metadata and index only; use exact citation to verify artifacts"}
                 for c in rows[offset:offset+limit]]
        return {"document_id": document_id, "items": items, "total_versions": len(rows),
                "returned": len(items), "snapshot": context["snapshot"],
                "next_cursor": self._cursor(signature, offset+len(items), len(rows)),
                "ordering": "Stable conversion ID order; not a quality ranking", "scientifically_verified": False}

    def prepare_review(self, citation):
        _required(citation, "citation", 300)
        if self.review_root.resolve() != self.review_root:
            raise EvidenceError("Configured review root changed through a symlink")
        reader = self._store()
        reader.get(citation)
        output = self.review_root / ("review-" + uuid.uuid4().hex)
        packet = create_packet(Store(self.store_root), citation, output)
        return {"packet_id": packet["packet_id"], "citation": citation,
                "packet_resource_uri": f"evidence://packets/{packet['packet_id']}",
                "page_resource_uris": [f"evidence://sources/{packet['metadata']['source_id']}/pages/{page}"
                                       for page in _pages(packet["metadata"]["locator"])],
                "approval_created": False, "scientifically_verified": False,
                "next_action": "A person must inspect the original and separately record the exact quote review"}

    def read_resource(self, uri):
        _required(uri, "resource URI", 1000)
        parsed = urlsplit(uri)
        if (parsed.scheme != "evidence" or parsed.query or parsed.fragment
                or parsed.netloc not in {"blocks", "sources", "packets"}):
            raise EvidenceError("Unknown evidence resource URI")
        parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
        store = self._store()
        if parsed.netloc == "blocks" and len(parts) in (2, 4):
            conversion, block = parts[:2]
            if not _CV.fullmatch(conversion) or not _BLOCK.fullmatch(block):
                raise EvidenceError("Invalid block resource identifier")
            offset = 0
            if len(parts) == 4:
                if parts[2] != "chunks" or not re.fullmatch(r"0|[1-9][0-9]{0,8}", parts[3]):
                    raise EvidenceError("Invalid block text offset")
                offset = int(parts[3])
            item = store.get(f"evidence:{conversion}#{block}")
            if offset and (offset >= len(item["block"]["text"]) or offset % MAX_TEXT):
                raise EvidenceError("Block text offset is outside a valid chunk boundary")
            result = self._item(store, item, offset=offset)
            return {"uri": uri, "mime_type": "application/json", "text": _json(result)}
        if parsed.netloc == "sources" and len(parts) == 3 and parts[1] == "pages":
            if not _SRC.fullmatch(parts[0]) or not re.fullmatch(r"[1-9][0-9]{0,5}", parts[2]):
                raise EvidenceError("Invalid source page resource identifier")
            source = store.source(parts[0])
            page_number = int(parts[2])
            if source["media_type"] != "application/pdf" or page_number > source.get("pages", 0):
                raise EvidenceError("Requested original PDF page does not exist")
            import pymupdf
            with pymupdf.open(source["path"]) as document:
                page = document[page_number-1]
                if not math.isfinite(page.rect.width * page.rect.height) or page.rect.width * page.rect.height * 4 > 20_000_000:
                    raise EvidenceError("Original page exceeds bounded rendering size")
                data = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False, annots=True).tobytes("png")
            if len(data) > MAX_PAGE_BYTES:
                raise EvidenceError("Rendered original page exceeds resource size limit")
            return {"uri": uri, "mime_type": "image/png", "data": data,
                    "sha256": hashlib.sha256(data).hexdigest(), "source_sha256": source["sha256"],
                    "physical_page": page_number, "renderer": "pymupdf", "scale": 2}
        if parsed.netloc == "packets" and len(parts) == 1 and _PKT.fullmatch(parts[0]):
            record = store._load("packets", parts[0])
            item = store.get(record["citation"])
            packet = store._packet(parts[0], item)
            result = {"packet_id": packet["packet_id"], "citation": packet["citation"],
                      "source_id": packet["metadata"]["source_id"],
                      "source_sha256": packet["metadata"]["source_sha256"],
                      "locator": _locator(packet["metadata"]["locator"]),
                      "renderer": packet["metadata"]["renderer"],
                      "artifacts": [{"name": name, "sha256": info["sha256"], "bytes": info["size"]}
                                    for name, info in packet["artifacts"].items()],
                      "page_resource_uris": [f"evidence://sources/{item['source']['source_id']}/pages/{page}"
                                             for page in _pages(item["block"]["locator"])],
                      "approval_created": False, "scientifically_verified": False}
            return {"uri": uri, "mime_type": "application/json", "text": _json(result)}
        raise EvidenceError("Unknown evidence resource URI")
