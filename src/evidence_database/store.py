"""Content-addressed evidence and a deliberately narrow quote review gate.

This is a local integrity ledger, not an identity provider. A sufficiently
privileged person can rewrite every file and database together; hashes detect
accidental or partial tampering, not an adversary controlling the whole store.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any


class EvidenceError(ValueError):
    """Invalid input, unavailable evidence, or failed integrity verification."""


_KINDS = {"sources": ("src", "source_id"), "ingestions": ("ing", "ingestion_id"),
          "conversions": ("cv", "conversion_id"),
          "packets": ("pkt", "packet_id"), "reviews": ("rv", "review_id")}
_BLOCK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise EvidenceError(f"Not finite JSON data: {exc}") from exc


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _nonempty(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{label} must be a nonempty string")
    return value


def _quote(value: str) -> str:
    return " ".join(_nonempty(value, "quote").split())


def _matches(text: str, quote: str) -> bool:
    """Whitespace tolerance only; never Unicode compatibility normalization.

    Word and numeric token boundaries stop '5' matching '15', '-5', or '5.2'.
    This confirms extraction, not context, entailment, or scientific truth.
    """
    text, quote = " ".join(text.split()), _quote(quote)
    pos = 0
    while (start := text.find(quote, pos)) != -1:
        end = start + len(quote)
        before, after = text[start - 1:start] if start else "", text[end:end + 1]
        left_bad = bool(before and quote[0].isalnum() and (before.isalnum() or before == "_"))
        right_bad = bool(after and quote[-1].isalnum() and (after.isalnum() or after == "_"))
        # Spaced signs and percentage units remain part of a numeric expression.
        # Keep decimal/list punctuation distinct so a sentence-final period is allowed.
        numeric_marks = "+-−﹣－⁻⁺₋₊±∓^×·"
        left_context, right_context = text[:start].rstrip(), text[end:].lstrip()
        near_left, near_right = left_context[-1:], right_context[:1]
        unsigned_quote = quote.lstrip("+-−﹣－⁻⁺₋₊±∓").lstrip()
        starts_numeric = unsigned_quote[:1].isnumeric() or (
            unsigned_quote[:1] in (".", ",") and unsigned_quote[1:2].isnumeric())
        if starts_numeric:
            if before and (before.isalnum() or before in ("_", ".")):
                left_bad = True
            if near_left and near_left in numeric_marks:
                left_bad = True
            if near_left in (".", ",") and left_context[:-1].rstrip()[-1:].isnumeric():
                left_bad = True
        if quote[-1].isnumeric():
            if near_right and near_right in numeric_marks + "%‰‱":
                right_bad = True
            if near_right in (".", ",") and right_context[1:].lstrip()[:1].isnumeric():
                right_bad = True
        # The end can be inside a number even when its last character is
        # punctuation: '5.' in '5.25', '5e-' in '5e-3', or '10^' in '10^-5'.
        tail = quote.rstrip(numeric_marks + ".,")
        numeric_tail = tail[-1:].isnumeric() or (
            tail[-1:] in ("e", "E") and tail[-2:-1].isnumeric())
        has_numeric_suffix = tail != quote or quote[-1:] in ("e", "E")
        if has_numeric_suffix and numeric_tail and right_context.lstrip(numeric_marks + " ")[:1].isnumeric():
            right_bad = True
        if not left_bad and not right_bad:
            return True
        pos = start + 1
    return False


class Store:
    """Immutable snapshots plus a checked, derived SQLite/FTS5 index.

    A read-only instance opens an existing rollback-journal store without creating
    files or repairing its schema. WAL stores are rejected: SQLite can create or
    update their shared-memory sidecars even with a mode=ro connection.
    """

    def __init__(self, root: Path, *, read_only: bool = False):
        if type(read_only) is not bool:
            raise EvidenceError("read_only must be a boolean")
        self.root = Path(root).resolve()
        self.read_only = read_only
        self.db_path = self.root / "index.sqlite3"
        if read_only:
            self._validate_read_only_structure()
            return
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "objects").mkdir(exist_ok=True)
        for kind in _KINDS:
            (self.root / "records" / kind).mkdir(parents=True, exist_ok=True)
        try:
            with self._db() as db:
                for kind, (_, key) in _KINDS.items():
                    extra = ", sequence INTEGER UNIQUE NOT NULL" if kind == "reviews" else ""
                    db.execute(f"CREATE TABLE IF NOT EXISTS {kind} ({key} TEXT PRIMARY KEY, "
                               f"record_json TEXT NOT NULL{extra})")
                db.execute("CREATE TABLE IF NOT EXISTS blocks (citation TEXT PRIMARY KEY, "
                           "conversion_id TEXT NOT NULL, block_id TEXT NOT NULL, text TEXT NOT NULL, "
                           "kind TEXT NOT NULL, locator_json TEXT NOT NULL, block_json TEXT NOT NULL)")
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS blocks_fts USING fts5(citation UNINDEXED, text)")
        except sqlite3.Error as exc:
            raise EvidenceError(f"Cannot open evidence index: {exc}") from exc

    def _require_writable(self):
        if self.read_only:
            raise EvidenceError("Evidence store is read-only; mutations are not permitted")

    def _read_only_index_preflight(self):
        if self.db_path.is_symlink() or not self.db_path.is_file():
            raise EvidenceError("Read-only evidence store requires an existing regular index.sqlite3")
        try:
            with self.db_path.open("rb") as handle:
                header = handle.read(100)
        except OSError as exc:
            raise EvidenceError(f"Cannot read existing evidence index: {exc}") from exc
        if len(header) < 100 or header[:16] != b"SQLite format 3\x00":
            raise EvidenceError("Read-only evidence store index is not a SQLite database")
        if header[18] == 2 or header[19] == 2:
            raise EvidenceError("Read-only evidence store does not support WAL mode without file side effects; "
                                "provide a consistent rollback-journal copy")

    def _validate_read_only_structure(self):
        required = [self.root, self.root / "objects", *[self.root / "records" / kind for kind in _KINDS]]
        for path in required:
            if not path.is_dir():
                raise EvidenceError(f"Read-only evidence store is missing required directory: {path}")
        expected = {kind: {key, "record_json"} | ({"sequence"} if kind == "reviews" else set())
                    for kind, (_, key) in _KINDS.items()}
        expected["blocks"] = {"citation", "conversion_id", "block_id", "text", "kind", "locator_json", "block_json"}
        expected["blocks_fts"] = {"citation", "text"}
        with self._db() as db:
            tables = {row["name"]: row["sql"] for row in
                      db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")}
            for name, columns in expected.items():
                if name not in tables:
                    raise EvidenceError(f"Read-only evidence index is missing required table: {name}")
                present = {row["name"] for row in db.execute(f"PRAGMA table_info({name})")}
                if not columns <= present:
                    raise EvidenceError(f"Read-only evidence index has missing columns in {name}: "
                                        f"{', '.join(sorted(columns - present))}")
            if not re.search(r"\bUSING\s+fts5\s*\(", tables["blocks_fts"] or "", flags=re.IGNORECASE):
                raise EvidenceError("Read-only evidence index requires the blocks_fts FTS5 virtual table")

    @staticmethod
    def _read_only_authorizer(action, first, second, _database, _trigger):
        # query_only alone permits ATTACH to create an empty database. Do not
        # expose that exception through the connection used by read operations.
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and first.lower() == "query_only" and second is not None:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @contextmanager
    def _db(self):
        db = None
        try:
            if self.read_only:
                self._read_only_index_preflight()
                db = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=30)
                db.execute("PRAGMA query_only=ON")
                db.set_authorizer(self._read_only_authorizer)
            else:
                db = sqlite3.connect(self.db_path, timeout=30)
            db.row_factory = sqlite3.Row
            yield db
            if not self.read_only:
                db.commit()
        except sqlite3.Error as exc:
            if db is not None:
                db.rollback()
            raise EvidenceError(f"Cannot read evidence index: {exc}") from exc
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def _blob_path(self, sha: str) -> Path:
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise EvidenceError("Invalid artifact hash")
        return self.root / "objects" / sha[:2] / sha

    def _write_once(self, path: Path, data: bytes):
        self._require_writable()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(data)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != data:
                raise EvidenceError(f"Existing immutable object differs: {path}") from None

    def _snapshot(self, path: Path) -> dict:
        self._require_writable()
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            raise EvidenceError(f"Cannot read artifact {path}: {exc}") from exc
        sha = _hash(data)
        self._write_once(self._blob_path(sha), data)
        return {"sha256": sha, "size": len(data)}

    def _blob(self, info: dict) -> Path:
        path = self._blob_path(info["sha256"])
        try:
            if path.is_symlink():
                raise EvidenceError("Object cannot be a symbolic link")
            data = path.read_bytes()
        except OSError as exc:
            raise EvidenceError(f"Missing artifact: {path}") from exc
        if _hash(data) != info["sha256"] or len(data) != info["size"]:
            raise EvidenceError(f"Artifact integrity failure: {path}")
        return path

    def _record_path(self, kind: str, record_id: str) -> Path:
        prefix, _ = _KINDS[kind]
        if not isinstance(record_id, str) or not re.fullmatch(prefix + r"_[0-9a-f]{64}", record_id):
            raise EvidenceError(f"Invalid {kind} identifier")
        return self.root / "records" / kind / (record_id + ".json")

    def _record_data(self, kind: str, record: dict) -> tuple[str, str]:
        self._require_writable()
        data = _json(record)
        record_id = _KINDS[kind][0] + "_" + _hash(data.encode("utf-8"))
        self._write_once(self._record_path(kind, record_id), data.encode("utf-8"))
        return record_id, data

    def _save(self, kind: str, record: dict) -> str:
        self._require_writable()
        record_id, data = self._record_data(kind, record)
        key = _KINDS[kind][1]
        with self._db() as db:
            db.execute(f"INSERT OR IGNORE INTO {kind} ({key},record_json) VALUES (?,?)", (record_id, data))
        self._load(kind, record_id)
        return record_id

    def _load(self, kind: str, record_id: str) -> dict:
        path = self._record_path(kind, record_id)
        try:
            if path.is_symlink():
                raise EvidenceError("Record cannot be a symbolic link")
            data = path.read_bytes()
            record = json.loads(data)
            if not isinstance(record, dict) or _hash(data) != record_id.split("_", 1)[1]:
                raise EvidenceError(f"Record integrity failure: {record_id}")
            canonical = _json(record)
            if data != canonical.encode("utf-8"):
                raise EvidenceError(f"Noncanonical record: {record_id}")
            key = _KINDS[kind][1]
            with self._db() as db:
                row = db.execute(f"SELECT record_json FROM {kind} WHERE {key}=?", (record_id,)).fetchone()
            if row is None or row[0] != canonical:
                raise EvidenceError(f"Index record differs: {record_id}")
            return record
        except (OSError, json.JSONDecodeError, sqlite3.Error) as exc:
            raise EvidenceError(f"Cannot verify {record_id}: {exc}") from exc

    def _ids(self, kind: str) -> list[str]:
        disk = {p.stem for p in (self.root / "records" / kind).glob("*.json")}
        with self._db() as db:
            indexed = {row[0] for row in db.execute(f"SELECT {_KINDS[kind][1]} FROM {kind}")}
        if disk != indexed:
            raise EvidenceError(f"{kind} index membership differs from immutable records")
        return sorted(disk)

    def ingest(self, path: Path, document_id: str, *, role="primary", metadata=None) -> dict:
        self._require_writable()
        _nonempty(document_id, "document_id")
        _nonempty(role, "role")
        if any(c in document_id for c in ("/", "\\", "\x00")) or document_id in (".", ".."):
            raise EvidenceError("Unsafe document ID")
        if metadata is not None and not isinstance(metadata, dict):
            raise EvidenceError("metadata must be a JSON object")
        metadata = json.loads(_json(metadata or {}))
        artifact = self._snapshot(path)
        stored = self._blob(artifact)
        record = {"schema": 1, "document_id": document_id, "role": role,
                  **artifact, "media_type": "application/octet-stream"}
        if stored.read_bytes().startswith(b"%PDF-"):
            try:
                import pymupdf as fitz
                with fitz.open(stored) as pdf:
                    if pdf.needs_pass or not len(pdf):
                        raise EvidenceError("PDF is encrypted or has no pages")
                    record.update(media_type="application/pdf", pages=len(pdf),
                                  page_sizes=[[float(p.cropbox.width), float(p.cropbox.height)] for p in pdf],
                                  page_rotations=[p.rotation for p in pdf])
            except ImportError as exc:
                raise EvidenceError("PDF ingest requires the optional PyMuPDF dependency") from exc
            except Exception as exc:
                if isinstance(exc, EvidenceError):
                    raise
                raise EvidenceError(f"Cannot inspect PDF: {exc}") from exc
        record_id = self._save("sources", record)
        receipt = {"schema": 1, "source_id": record_id, "document_id": document_id,
                   "sha256": artifact["sha256"], "size": artifact["size"], "acquisition": "local_copy",
                   "original_path": str(Path(path).resolve()), "original_filename": Path(path).name,
                   "timestamp": datetime.now(UTC).isoformat(), "metadata": metadata}
        ingestion_id = self._save("ingestions", receipt)
        return {**self.source(record_id), "ingestion_id": ingestion_id, "metadata": metadata}

    def source(self, source_id: str) -> dict:
        record = self._load("sources", source_id)
        return {**record, "source_id": source_id, "path": str(self._blob(record))}

    def sources(self) -> list[dict]:
        return [self.source(x) for x in self._ids("sources")]

    def ingestion_receipts(self, source_id: str | None = None) -> list[dict]:
        """Immutable local-copy acquisition events; the original path is provenance, not authority."""
        if source_id is not None:
            self.source(source_id)
        result = []
        for ingestion_id in self._ids("ingestions"):
            record = self._load("ingestions", ingestion_id)
            source = self.source(record["source_id"])
            if any(record[key] != source[key] for key in ("document_id", "sha256", "size")):
                raise EvidenceError("Acquisition receipt/source binding differs")
            if source_id is None or record["source_id"] == source_id:
                result.append({**record, "ingestion_id": ingestion_id})
        return sorted(result, key=lambda r: (r["timestamp"], r["ingestion_id"]))

    def _locator(self, locator: Any, source: dict) -> dict:
        if locator is None or locator == {}:
            return {"unlocated": True}
        if not isinstance(locator, dict):
            raise EvidenceError("locator must be a JSON object")
        loc = json.loads(_json(locator))

        def page(value):
            if type(value) is not int or value < 1 or value > source.get("pages", value):
                raise EvidenceError("Page is not a physical 1-based source page")
            return value

        pages = []
        if "page" in loc:
            pages.append(page(loc["page"]))
        if "pages" in loc:
            if not isinstance(loc["pages"], list) or not loc["pages"]:
                raise EvidenceError("pages must be a nonempty page list")
            if len(set(loc["pages"])) != len(loc["pages"]):
                raise EvidenceError("Duplicate physical pages")
            pages.extend(page(p) for p in loc["pages"])

        def bbox(value, page_number):
            if not isinstance(value, (list, tuple)) or len(value) != 4:
                raise EvidenceError("bbox requires four PDF-point coordinates")
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in value):
                raise EvidenceError("bbox coordinates must be finite numbers")
            x0, y0, x1, y1 = value
            if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0:
                raise EvidenceError("bbox has negative or reversed/empty bounds")
            if "page_sizes" in source:
                width, height = source["page_sizes"][page_number - 1]
                if x1 > width + 0.01 or y1 > height + 0.01:
                    raise EvidenceError("bbox exceeds physical page")

        if "bbox" in loc:
            if "page" not in loc:
                raise EvidenceError("bbox needs one explicit physical page")
            bbox(loc["bbox"], loc["page"])
        if "regions" in loc:
            declared_pages = set(pages)
            if not isinstance(loc["regions"], list) or not loc["regions"]:
                raise EvidenceError("regions must be a nonempty list")
            for region in loc["regions"]:
                if not isinstance(region, dict) or "page" not in region or "bbox" not in region:
                    raise EvidenceError("Each region needs page and bbox")
                p = page(region["page"])
                if declared_pages and p not in declared_pages:
                    raise EvidenceError("Region page not included in block pages")
                bbox(region["bbox"], p)
                if p not in pages:
                    pages.append(p)
        if not pages:
            if loc.get("unlocated") is not True:
                raise EvidenceError("A missing physical location must be explicitly unlocated")
        elif loc.get("unlocated"):
            raise EvidenceError("Located block cannot also be unlocated")
        return loc

    def _artifacts(self, artifacts: dict | None) -> dict:
        self._require_writable()
        if artifacts is not None and not isinstance(artifacts, dict):
            raise EvidenceError("artifacts must map safe logical names to files")
        result = {}
        for name, path in (artifacts or {}).items():
            if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name:
                raise EvidenceError("Unsafe artifact logical filename")
            logical = PurePosixPath(name)
            if logical.is_absolute() or any(p in (".", "..") for p in name.split("/")) or "//" in name:
                raise EvidenceError("Unsafe artifact logical filename")
            result[name] = self._snapshot(Path(path))
        return result

    def add_conversion(self, source_id: str, *, parser: str, parser_version: str, blocks: list[dict],
                       settings=None, artifacts=None, issues=None, status="completed") -> dict:
        self._require_writable()
        source = self.source(source_id)
        _nonempty(parser, "parser")
        _nonempty(parser_version, "parser_version")
        if status not in ("completed", "failed", "partial"):
            raise EvidenceError("status must be completed, partial, or failed")
        if not isinstance(blocks, list) or (not blocks and status != "failed"):
            raise EvidenceError("Successful conversion requires evidence blocks")
        if settings is not None and not isinstance(settings, dict):
            raise EvidenceError("settings must be a JSON object")
        if issues is not None and not isinstance(issues, list):
            raise EvidenceError("issues must be a JSON list")
        normalized, seen = [], set()
        for block in blocks:
            if not isinstance(block, dict):
                raise EvidenceError("block must be an object")
            block = json.loads(_json(block))
            block_id = block.get("id")
            if not isinstance(block_id, str) or not _BLOCK_ID.fullmatch(block_id) or block_id in seen:
                raise EvidenceError("Invalid or duplicate block ID")
            seen.add(block_id)
            _nonempty(block.get("kind"), "block kind")
            if not isinstance(block.get("text"), str):
                raise EvidenceError("block text must be a string")
            block["locator"] = self._locator(block.get("locator"), source)
            block_issues = block.setdefault("issues", [])
            if not isinstance(block_issues, list):
                raise EvidenceError("block issues must be a list")
            if block["locator"].get("unlocated"):
                block["location_accuracy"] = "unlocated"
                if "unlocated" not in block_issues:
                    block_issues.append("unlocated")
            else:
                block["location_accuracy"] = (
                    "region" if "bbox" in block["locator"] or "regions" in block["locator"] else "page")
            normalized.append(block)
        record = {"schema": 1, "source_id": source_id, "parser": parser, "parser_version": parser_version,
                  "settings": settings or {}, "artifacts": self._artifacts(artifacts),
                  "issues": issues or [], "status": status, "blocks": normalized}
        conversion_id, data = self._record_data("conversions", record)
        with self._db() as db:
            exists = db.execute("SELECT 1 FROM conversions WHERE conversion_id=?", (conversion_id,)).fetchone()
            if not exists:
                db.execute("INSERT INTO conversions VALUES (?,?)", (conversion_id, data))
                if status != "failed":
                    for block in normalized:
                        citation = f"evidence:{conversion_id}#{block['id']}"
                        db.execute("INSERT INTO blocks VALUES (?,?,?,?,?,?,?)", (
                            citation, conversion_id, block["id"], block["text"], block["kind"],
                            _json(block["locator"]), _json(block)))
                        db.execute("INSERT INTO blocks_fts(citation,text) VALUES (?,?)", (citation, block["text"]))
        return self.conversion(conversion_id)

    def _verify_blocks(self, conversion_id: str, record: dict):
        expected = {} if record["status"] == "failed" else {
            f"evidence:{conversion_id}#{b['id']}": b for b in record["blocks"]}
        with self._db() as db:
            rows = db.execute("SELECT * FROM blocks WHERE conversion_id=?", (conversion_id,)).fetchall()
            fts = db.execute("SELECT citation,text FROM blocks_fts WHERE citation LIKE ?",
                             (f"evidence:{conversion_id}#%",)).fetchall()
        actual = {r["citation"]: r for r in rows}
        if set(actual) != set(expected) or len(fts) != len(expected):
            raise EvidenceError(f"Block index membership differs: {conversion_id}")
        if sorted((r[0], r[1]) for r in fts) != sorted((c, b["text"]) for c, b in expected.items()):
            raise EvidenceError(f"FTS index differs: {conversion_id}")
        for citation, block in expected.items():
            row = actual[citation]
            if (row["block_id"], row["text"], row["kind"], row["locator_json"], row["block_json"]) != (
                    block["id"], block["text"], block["kind"], _json(block["locator"]), _json(block)):
                raise EvidenceError(f"Block index content differs: {citation}")

    def conversion(self, conversion_id: str) -> dict:
        record = self._load("conversions", conversion_id)
        self.source(record["source_id"])
        artifacts = {name: {**info, "path": str(self._blob(info))} for name, info in record["artifacts"].items()}
        self._verify_blocks(conversion_id, record)
        citations = [] if record["status"] == "failed" else [
            f"evidence:{conversion_id}#{block['id']}" for block in record["blocks"]]
        return {**record, "conversion_id": conversion_id, "artifacts": artifacts, "citations": citations}

    def conversions(self, document_id: str | None = None) -> list[dict]:
        result = [self.conversion(x) for x in self._ids("conversions")]
        return [x for x in result if document_id is None or self.source(x["source_id"])["document_id"] == document_id]

    def _citation(self, citation: str) -> tuple[str, str]:
        if not isinstance(citation, str) or not citation.startswith("evidence:") or citation.count("#") != 1:
            raise EvidenceError("Invalid evidence citation")
        conversion_id, block_id = citation[len("evidence:"):].split("#")
        self._record_path("conversions", conversion_id)
        if not _BLOCK_ID.fullmatch(block_id):
            raise EvidenceError("Invalid citation block ID")
        return conversion_id, block_id

    def _item(self, citation: str) -> dict:
        conversion_id, block_id = self._citation(citation)
        conversion = self.conversion(conversion_id)
        if conversion["status"] == "failed":
            raise EvidenceError("Failed conversions are not evidence")
        block = next((b for b in conversion["blocks"] if b["id"] == block_id), None)
        if block is None:
            raise EvidenceError("Citation block does not exist")
        return {"citation": citation, "source": self.source(conversion["source_id"]),
                "conversion_id": conversion_id, "block": block, "conversion_status": conversion["status"],
                "conversion_issues": conversion["issues"], "parser": conversion["parser"],
                "parser_version": conversion["parser_version"]}

    def get(self, citation: str) -> dict:
        item = self._item(citation)
        events = self._events(citation)
        return {**item, "review_status": "quote_specific_reviews" if events else "pending",
                "scientifically_verified": False}

    def search(self, query: str, *, limit=20) -> list[dict]:
        _nonempty(query, "query")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise EvidenceError("limit must be an integer from 1 to 1000")
        # Verify the complete search view before asking FTS: checking only hits
        # cannot detect a removed or rewritten row that hides relevant evidence.
        # This is intentionally O(corpus blocks) for the local v0.1 store.
        expected = set()
        for conversion_id in self._ids("conversions"):
            record = self._load("conversions", conversion_id)
            self._verify_blocks(conversion_id, record)
            if record["status"] != "failed":
                expected.update(f"evidence:{conversion_id}#{block['id']}" for block in record["blocks"])
        with self._db() as db:
            indexed = {row[0] for row in db.execute("SELECT citation FROM blocks")}
            indexed_fts = [row[0] for row in db.execute("SELECT citation FROM blocks_fts")]
        if indexed != expected or set(indexed_fts) != expected or len(indexed_fts) != len(expected):
            raise EvidenceError("Search index membership differs from immutable conversions")
        # An exact FTS phrase makes operators user text, never executable query syntax.
        literal = '"' + query.replace('"', '""') + '"'
        try:
            with self._db() as db:
                rows = db.execute("SELECT citation FROM blocks_fts WHERE blocks_fts MATCH ? "
                                  "ORDER BY rank LIMIT ?", (literal, limit)).fetchall()
            results = []
            for row in rows:
                item = self.get(row[0])
                results.append({**item, "text": item["block"]["text"], "locator": item["block"]["locator"]})
            return results
        except sqlite3.Error as exc:
            raise EvidenceError(f"Search index failure: {exc}") from exc

    @staticmethod
    def _pages(locator: dict) -> set[int]:
        return ({locator["page"]} if "page" in locator else set()) | set(locator.get("pages", [])) | {
            r["page"] for r in locator.get("regions", [])}

    def _packet_binding(self, metadata: dict, item: dict):
        if not isinstance(metadata, dict):
            raise EvidenceError("Packet metadata must be an object")
        expected = {"citation": item["citation"], "source_id": item["source"]["source_id"],
                    "source_sha256": item["source"]["sha256"], "locator": item["block"]["locator"]}
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise EvidenceError("Packet binding does not match original source/citation/locator")
        if not self._pages(item["block"]["locator"]) or item["source"]["media_type"] != "application/pdf":
            raise EvidenceError("Original-page review needs a located PDF block")

    def _render_check(self, metadata: dict, artifacts: dict, item: dict):
        self._packet_binding(metadata, item)
        try:
            import pymupdf as fitz
            if (metadata.get("renderer") != "pymupdf" or metadata.get("renderer_version") != fitz.VersionBind
                    or metadata.get("scale") != 2):
                raise EvidenceError("Packet must use the supported, version-recorded original-page renderer")
            pages = metadata.get("rendered_pages")
            if not isinstance(pages, list) or not pages:
                raise EvidenceError("Packet needs rendered original pages")
            rendered = set()
            with fitz.open(item["source"]["path"]) as pdf:
                for entry in pages:
                    if not isinstance(entry, dict) or type(entry.get("page")) is not int:
                        raise EvidenceError("Invalid rendered page entry")
                    page = entry["page"]
                    if page in rendered or not 1 <= page <= len(pdf) or entry.get("artifact") not in artifacts:
                        raise EvidenceError("Invalid or duplicate packet page/artifact")
                    rendered.add(page)
                    generated = pdf[page - 1].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False, annots=True)
                    actual = self._blob(artifacts[entry["artifact"]]).read_bytes()
                    if _hash(generated.tobytes("png")) != _hash(actual):
                        raise EvidenceError("Packet image is not the rendered original source page")
            if not self._pages(item["block"]["locator"]).issubset(rendered):
                raise EvidenceError("Packet does not cover every physical block page")
        except ImportError as exc:
            raise EvidenceError("Original-page review requires PyMuPDF") from exc
        except Exception as exc:
            if isinstance(exc, EvidenceError):
                raise
            raise EvidenceError(f"Cannot verify original-page rendering: {exc}") from exc

    def register_packet(self, citation: str, *, artifacts: dict[str, Path], metadata: dict) -> dict:
        self._require_writable()
        item = self.get(citation)
        metadata = json.loads(_json(metadata))
        snapshots = self._artifacts(artifacts)
        self._render_check(metadata, snapshots, item)
        record = {"schema": 1, "citation": citation, "metadata": metadata, "artifacts": snapshots}
        packet_id = self._save("packets", record)
        return self._packet(packet_id, item)

    def _packet(self, packet_id: str, item: dict) -> dict:
        record = self._load("packets", packet_id)
        if record["citation"] != item["citation"]:
            raise EvidenceError("Packet is for another citation")
        self._packet_binding(record["metadata"], item)
        artifacts = {name: {**info, "path": str(self._blob(info))} for name, info in record["artifacts"].items()}
        return {**record, "packet_id": packet_id, "artifacts": artifacts}

    def _events(self, citation: str | None = None) -> list[dict]:
        ids = self._ids("reviews")
        events = [{**self._load("reviews", rid), "review_id": rid} for rid in ids]
        events.sort(key=lambda x: x["sequence"])
        previous = None
        with self._db() as db:
            sequences = {r[0]: r[1] for r in db.execute("SELECT review_id,sequence FROM reviews")}
        for sequence, event in enumerate(events, 1):
            if (event["sequence"] != sequence or event["previous_review_id"] != previous
                    or sequences[event["review_id"]] != sequence):
                raise EvidenceError("Append-only review ledger chain or index differs")
            previous = event["review_id"]
        return [event for event in events if citation is None or event["citation"] == citation]

    def reviews(self, citation: str) -> list[dict]:
        self._item(citation)
        return self._events(citation)

    def review(self, citation: str, quote: str, *, decision: str, reviewer: str, reviewer_kind: str,
               note: str, viewed_original: bool = False, packet_id: str | None = None) -> dict:
        self._require_writable()
        normalized_quote = _quote(quote)
        _nonempty(reviewer, "reviewer")
        _nonempty(note, "review note")
        if reviewer_kind not in ("human", "ai") or decision not in ("approve", "reject", "revoke"):
            raise EvidenceError("Unknown reviewer kind or decision")
        if type(viewed_original) is not bool:
            raise EvidenceError("viewed_original must be a boolean")
        if decision == "approve" and reviewer_kind != "human":
            raise EvidenceError("AI diagnostics cannot approve evidence")
        if decision == "revoke" and reviewer_kind != "human":
            raise EvidenceError("AI diagnostics may reject, but cannot issue a human revocation")
        item = self.get(citation)
        if decision == "approve":
            if not _matches(item["block"]["text"], normalized_quote):
                raise EvidenceError("Approval quote is absent or crosses numeric/word boundaries")
            if not viewed_original or not packet_id:
                raise EvidenceError("Human approval requires original-view confirmation and a bound packet")
            bound_packet = self._packet(packet_id, item)
            self._render_check(bound_packet["metadata"], bound_packet["artifacts"], item)
        elif packet_id:
            self._packet(packet_id, item)
        events = self._events()
        record = {"schema": 1, "sequence": len(events) + 1,
                  "previous_review_id": events[-1]["review_id"] if events else None,
                  "citation": citation, "quote": quote, "normalized_quote": normalized_quote,
                  "quote_sha256": _hash(normalized_quote.encode("utf-8")), "decision": decision,
                  "reviewer": reviewer, "reviewer_kind": reviewer_kind, "actor_kind": reviewer_kind,
                  "reviewer_identity_assurance": "self_declared_local", "note": note,
                  "viewed_original": viewed_original, "packet_id": packet_id,
                  "timestamp": datetime.now(UTC).isoformat(), "scientifically_verified": False}
        # A transaction serializes ledger append, avoiding a fork if two CLI processes review together.
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
            if count != len(events):
                raise EvidenceError("Review ledger changed concurrently; retry the review")
            review_id, data = self._record_data("reviews", record)
            db.execute("INSERT INTO reviews VALUES (?,?,?)", (review_id, data, record["sequence"]))
        self._events()
        return {**record, "review_id": review_id}

    def check(self, citation: str, quote: str, *, require_review=True) -> dict:
        if type(require_review) is not bool:
            raise EvidenceError("require_review must be a boolean")
        normalized_quote = _quote(quote)
        item = self.get(citation)
        matches = _matches(item["block"]["text"], normalized_quote)
        events = [e for e in self._events(citation) if e["normalized_quote"] == normalized_quote]
        event = events[-1] if events else None
        approved = bool(event and event["decision"] == "approve" and event["reviewer_kind"] == "human"
                        and event["viewed_original"])
        if approved:
            if not event["packet_id"]:
                raise EvidenceError("Approved event is missing its original packet")
            self._packet(event["packet_id"], item)
        admitted = bool(matches and require_review and approved)
        reason = ("inspection_only" if not require_review else "quote_not_found" if not matches else
                  "human_reviewed_extraction" if admitted else event["decision"] if event else "pending_review")
        return {"citation": citation, "quote": quote, "quote_matches": matches, "admitted": admitted,
                "reason": reason, "review_status": event["decision"] if event else "pending",
                "conversion_status": item["conversion_status"], "conversion_issues": item["conversion_issues"],
                "parser": item["parser"], "parser_version": item["parser_version"],
                "review_id": event["review_id"] if event else None, "scientifically_verified": False,
                "meaning": "Reviewed extraction only; not scientific, external, or clinical validation"}

    def audit(self) -> dict:
        errors, counts = [], {}
        expected_citations = set()
        for kind in _KINDS:
            try:
                ids = self._ids(kind)
                counts[kind] = len(ids)
            except (EvidenceError, sqlite3.Error) as exc:
                errors.append(f"{kind}: {exc}")
                ids = [p.stem for p in (self.root / "records" / kind).glob("*.json")]
                counts[kind] = len(ids)
            for record_id in ids:
                try:
                    if kind == "sources":
                        self.source(record_id)
                    elif kind == "ingestions":
                        record = self._load(kind, record_id)
                        source = self.source(record["source_id"])
                        if any(record[key] != source[key] for key in ("document_id", "sha256", "size")):
                            raise EvidenceError("Acquisition receipt/source binding differs")
                    elif kind == "conversions":
                        expected_citations.update(self.conversion(record_id)["citations"])
                    elif kind == "packets":
                        record = self._load(kind, record_id)
                        self._packet(record_id, self._item(record["citation"]))
                    else:
                        self._load(kind, record_id)
                except (EvidenceError, sqlite3.Error, KeyError) as exc:
                    errors.append(f"{record_id}: {exc}")
        try:
            self._events()
            with self._db() as db:
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    errors.append(f"SQLite: {integrity}")
                known = {r[0] for r in db.execute("SELECT citation FROM blocks")}
                if known != expected_citations:
                    errors.append("Global block index membership differs from immutable conversions")
                fts = [r[0] for r in db.execute("SELECT citation FROM blocks_fts")]
                if set(fts) != known or len(fts) != len(known):
                    errors.append("Global FTS/block membership differs")
        except (EvidenceError, sqlite3.Error) as exc:
            errors.append(str(exc))
        return {"ok": not errors, "errors": errors, "counts": counts,
                "scientifically_verified": False, "identity_assurance": "self_declared_local"}
