"""Append-only advisory reading-pitfall ledger and stateless quote hazard checks.

Memory notes guide attention toward known cohort, direction, causal-scope or
internal source conflicts without altering Store.check() admission verdicts.
Transcription hazards are computed statelessly from each conversion block.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .mcp_locks import OperationLock
from .quoting import sentences
from .store import EvidenceError, Store, _match_starts, _quote
from .verification import text_defects

MEMORY_CODES = (
    "numbers_denominators",
    "direction_comparator",
    "causal_strength",
    "source_inconsistency",
    "scope_context",
)
MAX_NOTE_CHARS = 400
_MEM_ID = re.compile(r"mem_[0-9a-f]{64}\Z")
_RV_ID = re.compile(r"rv_[0-9a-f]{64}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CITATION = re.compile(r"evidence:cv_[0-9a-f]{64}#[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_LOCAL_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|file://|/(?:home|Users|mnt|tmp|var|private)/)")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_LINE_HYPHEN = re.compile(r"(?<=[A-Za-z])[-\u00ad] (?=[a-z])")


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise EvidenceError(f"Not finite JSON data: {exc}") from exc


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _clean_text(value: Any, label: str, *, maximum: int = MAX_NOTE_CHARS) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{label} must be a nonempty string")
    if _CONTROL.search(value):
        raise EvidenceError(f"{label} cannot contain control characters")
    collapsed = " ".join(value.split())
    if len(collapsed) > maximum:
        raise EvidenceError(f"{label} must be at most {maximum} characters")
    if _LOCAL_PATH.search(collapsed):
        raise EvidenceError(f"{label} cannot contain local filesystem paths")
    return collapsed


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 300:
        raise EvidenceError(f"{label} must be a nonempty string of at most 300 characters")
    if any(ch in value for ch in ("/", "\\", "\x00")) or value in (".", "..") or _CONTROL.search(value):
        raise EvidenceError(f"Unsafe {label}")
    return value


def validate_memory_root(root: Path, *, other_roots: tuple[Path, ...] = ()) -> Path:
    raw = Path(root)
    if raw.is_symlink():
        raise EvidenceError("Memory root cannot be a symbolic link")
    resolved = raw.resolve()
    if resolved.exists() and not resolved.is_dir():
        raise EvidenceError("Memory root must be a directory")
    for other in other_roots:
        other_resolved = Path(other).resolve()
        nested = resolved in other_resolved.parents or other_resolved in resolved.parents
        if resolved == other_resolved or nested:
            raise EvidenceError("Memory directory and managed store/review/project directories must not nest")
    return resolved


def compute_quote_hazards(block: dict, *, text_truncated: bool = False) -> dict:
    """Statelessly flag text-layer and typography hazards on a conversion block."""
    text = block.get("text") if isinstance(block, dict) else ""
    if not isinstance(text, str):
        text = ""
    collapsed = " ".join(text.split())
    codes = {code for _, _, code in text_defects(collapsed)}
    if _LINE_HYPHEN.search(collapsed):
        codes.add("line_end_hyphenation")
    if isinstance(block, dict) and block.get("superscripts"):
        codes.add("superscript_position_lost")
    if isinstance(block, dict) and block.get("symbol_font_spans"):
        codes.add("symbol_font_letter")
    ordered = sorted(codes)
    return {"codes": ordered, "prefer_sentence_positions": bool(ordered and not text_truncated)}


def block_sentence_hashes(block_text: str) -> set[str]:
    collapsed = " ".join(block_text.split())
    return {_hash(collapsed[start:end].encode("utf-8")) for start, end in sentences(block_text)}


def quote_sentence_hashes(block_text: str, quote: str, *, require_match: bool = True) -> list[str]:
    """Ordered SHA-256 digests of sentences in ``block_text`` touched by ``quote``."""
    normalized = _quote(quote)
    starts = list(_match_starts(block_text, normalized))
    if not starts:
        if require_match:
            raise EvidenceError("Anchor quote is absent from block or crosses numeric/word boundaries")
        return []
    collapsed = " ".join(block_text.split())
    spans = sentences(block_text)
    length = len(normalized)
    digests: list[str] = []
    for s_start, s_end in spans:
        if any(s_start < m_start + length and s_end > m_start for m_start in starts):
            digest = _hash(collapsed[s_start:s_end].encode("utf-8"))
            if digest not in digests:
                digests.append(digest)
    return digests


def _public_note(record: dict, *, match_scope: str) -> dict:
    out = {
        "memory_id": record["memory_id"],
        "code": record["code"],
        "match_scope": match_scope,
        "document_id": record["document_id"],
        "role": record["role"],
        "citation": record.get("citation"),
        "related_citations": list(record.get("related_citations") or []),
        "review_id": record.get("review_id"),
        "note": record["note"],
        "actor_kind": record["actor_kind"],
        "citable": False,
        "authority": "advisory_note_only",
        "scientifically_verified": False,
    }
    if match_scope == "sentence_across_conversion" and record.get("citation"):
        out["recorded_citation"] = record["citation"]
    return out


@dataclass(frozen=True)
class MemorySnapshot:
    status: str
    head: dict | None
    active: tuple[dict, ...]
    error: str | None = None

    def summary_dict(self, notes: list[dict] | None = None) -> dict:
        base: dict[str, Any] = {"status": self.status, "head": self.head}
        if self.error is not None:
            base["error"] = self.error
        if notes is not None:
            base["notes"] = notes
        return base

    def for_item(self, *, citation: str, document_id: str, role: str, block_text: str,
                 limit: int = 3) -> list[dict]:
        if self.status != "ok" or not self.active:
            return []
        sent_hashes: set[str] | None = None
        exact, across, doc_level = [], [], []
        for entry in self.active:
            if entry["citation"] == citation:
                exact.append(_public_note(entry, match_scope="citation"))
            elif entry["document_id"] == document_id and entry["role"] == role:
                if entry["citation"] is None:
                    doc_level.append(_public_note(entry, match_scope="document"))
                elif entry.get("sentence_sha256s"):
                    if sent_hashes is None:
                        sent_hashes = block_sentence_hashes(block_text)
                    if sent_hashes.intersection(entry["sentence_sha256s"]):
                        across.append(_public_note(entry, match_scope="sentence_across_conversion"))
        return (exact + across + doc_level)[:limit]

    def for_quote(self, *, citation: str, document_id: str, role: str, block_text: str,
                  quote: str, limit: int = 3) -> list[dict]:
        if self.status != "ok" or not self.active:
            return []
        touched = set(quote_sentence_hashes(block_text, quote, require_match=False))
        exact, across, doc_level = [], [], []
        for entry in self.active:
            entry_hashes = set(entry.get("sentence_sha256s") or ())
            if entry["citation"] == citation:
                if not entry_hashes or (touched and entry_hashes.intersection(touched)):
                    exact.append(_public_note(entry, match_scope="citation"))
            elif entry["document_id"] == document_id and entry["role"] == role:
                if entry["citation"] is None:
                    doc_level.append(_public_note(entry, match_scope="document"))
                elif entry_hashes and touched and entry_hashes.intersection(touched):
                    across.append(_public_note(entry, match_scope="sentence_across_conversion"))
        return (exact + across + doc_level)[:limit]


class MemoryLedger:
    """Append-only canonical JSON hash-chain ledger for reading-pitfall notes."""

    def __init__(self, root: Path, *, read_only: bool = False, other_roots: tuple[Path, ...] = ()):
        if type(read_only) is not bool:
            raise EvidenceError("read_only must be a boolean")
        self.original_root = Path(root)
        self.root = validate_memory_root(root, other_roots=other_roots)
        self.read_only = read_only
        self.records_dir = self.root / "records"
        self.lock_path = self.root / "ledger.lock"

    def _check_root_unchanged(self):
        if self.original_root.is_symlink() or self.root.resolve() != self.root:
            raise EvidenceError("Configured memory root changed through a symbolic link")
        if self.records_dir.exists() and (self.records_dir.is_symlink() or not self.records_dir.is_dir()):
            raise EvidenceError("Memory records directory must be a regular directory")

    @staticmethod
    def _validate_event(memory_id: str, record: dict):
        if not isinstance(record, dict) or record.get("schema") != 1:
            raise EvidenceError(f"Unsupported memory schema in {memory_id}")
        kind = record.get("kind")
        if kind not in ("add", "resolve"):
            raise EvidenceError(f"Invalid memory event kind in {memory_id}")
        common = {"schema", "kind", "sequence", "previous_memory_id", "actor", "actor_kind",
                  "identity_assurance", "timestamp"}
        if type(record.get("sequence")) is not int or record["sequence"] < 1:
            raise EvidenceError(f"Invalid sequence in {memory_id}")
        prev = record.get("previous_memory_id")
        if prev is not None and (not isinstance(prev, str) or not _MEM_ID.fullmatch(prev)):
            raise EvidenceError(f"Invalid previous_memory_id in {memory_id}")
        _clean_text(record.get("actor"), "actor", maximum=200)
        if record.get("actor_kind") not in ("human", "ai"):
            raise EvidenceError(f"Invalid actor_kind in {memory_id}")
        if record.get("identity_assurance") != "self_declared_local":
            raise EvidenceError(f"Invalid identity_assurance in {memory_id}")
        _clean_text(record.get("timestamp"), "timestamp", maximum=64)
        if kind == "add":
            allowed = common | {"code", "document_id", "role", "citation", "sentence_sha256s",
                                "related_citations", "note", "review_id"}
            if set(record) != allowed:
                raise EvidenceError(f"Unexpected or missing fields in add event {memory_id}")
            if record["code"] not in MEMORY_CODES:
                raise EvidenceError(f"Unknown memory code in {memory_id}")
            _safe_id(record["document_id"], "document_id")
            _safe_id(record["role"], "role")
            citation = record["citation"]
            if citation is not None and (not isinstance(citation, str) or not _CITATION.fullmatch(citation)):
                raise EvidenceError(f"Invalid citation in {memory_id}")
            hashes = record["sentence_sha256s"]
            if (not isinstance(hashes, list) or len(hashes) > 50
                    or any(not isinstance(h, str) or not _SHA256.fullmatch(h) for h in hashes)):
                raise EvidenceError(f"Invalid sentence_sha256s in {memory_id}")
            if hashes and citation is None:
                raise EvidenceError(f"sentence_sha256s requires a citation in {memory_id}")
            related = record["related_citations"]
            if (not isinstance(related, list) or len(related) > 20
                    or any(not isinstance(c, str) or not _CITATION.fullmatch(c) for c in related)):
                raise EvidenceError(f"Invalid related_citations in {memory_id}")
            review_id = record["review_id"]
            if review_id is not None and (not isinstance(review_id, str) or not _RV_ID.fullmatch(review_id)):
                raise EvidenceError(f"Invalid review_id in {memory_id}")
            _clean_text(record["note"], "note", maximum=MAX_NOTE_CHARS)
        else:
            allowed = common | {"target_memory_id", "reason"}
            if set(record) != allowed:
                raise EvidenceError(f"Unexpected or missing fields in resolve event {memory_id}")
            target = record["target_memory_id"]
            if not isinstance(target, str) or not _MEM_ID.fullmatch(target):
                raise EvidenceError(f"Invalid target_memory_id in {memory_id}")
            _clean_text(record["reason"], "reason", maximum=MAX_NOTE_CHARS)

    def _load_verified(self) -> tuple[list[dict], list[dict]]:
        self._check_root_unchanged()
        if not self.root.exists():
            return [], []
        if not self.records_dir.exists():
            unexpected = [p.name for p in self.root.iterdir()
                          if p.name != "ledger.lock" and not p.name.startswith(".tmp-")]
            if unexpected:
                raise EvidenceError("Memory directory contains unexpected files without a records ledger")
            return [], []
        events: list[dict] = []
        for entry in sorted(self.records_dir.iterdir()):
            if entry.is_symlink() or not entry.is_file() or not entry.name.endswith(".json"):
                raise EvidenceError(f"Invalid file in memory records directory: {entry.name}")
            memory_id = entry.stem
            if not _MEM_ID.fullmatch(memory_id):
                raise EvidenceError(f"Invalid memory record filename: {entry.name}")
            raw = entry.read_bytes()
            if _hash(raw) != memory_id.split("_", 1)[1]:
                raise EvidenceError(f"Memory record hash mismatch: {memory_id}")
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise EvidenceError(f"Corrupted JSON in {memory_id}: {exc}") from exc
            if raw != _json(record).encode("utf-8"):
                raise EvidenceError(f"Noncanonical memory record: {memory_id}")
            self._validate_event(memory_id, record)
            events.append({**record, "memory_id": memory_id})
        events.sort(key=lambda item: (item["sequence"], item["memory_id"]))
        previous = None
        active_by_id: dict[str, dict] = {}
        seen_adds: set[str] = set()
        for expected_seq, event in enumerate(events, 1):
            if event["sequence"] != expected_seq or event["previous_memory_id"] != previous:
                raise EvidenceError("Memory ledger hash chain or sequence is broken")
            previous = event["memory_id"]
            if event["kind"] == "add":
                seen_adds.add(event["memory_id"])
                active_by_id[event["memory_id"]] = event
            else:
                target = event["target_memory_id"]
                if target not in seen_adds:
                    raise EvidenceError(f"Resolve event references unknown add record: {target}")
                active_by_id.pop(target, None)
        return events, list(active_by_id.values())

    @classmethod
    def load_snapshot(cls, root: Path | None, *, other_roots: tuple[Path, ...] = ()) -> MemorySnapshot:
        if root is None:
            return MemorySnapshot(status="not_configured", head=None, active=())
        try:
            ledger = cls(root, read_only=True, other_roots=other_roots)
            events, active = ledger._load_verified()
            head = ({"sequence": events[-1]["sequence"], "memory_id": events[-1]["memory_id"]}
                    if events else None)
            return MemorySnapshot(status="ok", head=head, active=tuple(active))
        except (EvidenceError, OSError, ValueError):
            return MemorySnapshot(status="unavailable", head=None, active=(),
                                  error="memory_ledger_unavailable")

    def _append_locked(self, build_record) -> dict:
        if self.read_only:
            raise EvidenceError("Memory ledger is opened read-only")
        self._check_root_unchanged()
        self.root.mkdir(parents=True, exist_ok=True)
        with OperationLock(self.lock_path):
            self.records_dir.mkdir(parents=True, exist_ok=True)
            events, active = self._load_verified()
            early, record = build_record(events, active)
            if early is not None:
                return early
            data = _json(record).encode("utf-8")
            memory_id = "mem_" + _hash(data)
            self._validate_event(memory_id, record)
            target = self.records_dir / f"{memory_id}.json"
            if target.exists():
                raise EvidenceError("Duplicate memory event collision")
            temp = self.root / f".tmp-{uuid.uuid4().hex}"
            try:
                with temp.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, target)
            finally:
                if temp.exists():
                    temp.unlink(missing_ok=True)
            return {**record, "memory_id": memory_id, "deduplicated": False}

    def add(self, *, code: str, note: str, actor: str, actor_kind: str,
            document_id: str | None = None, role: str | None = None,
            citation: str | None = None, quote: str | None = None,
            sentence_range: tuple[int, int | None] | None = None,
            related_citations: list[str] | None = None,
            review_id: str | None = None, store: Store | None = None) -> dict:
        if code not in MEMORY_CODES:
            raise EvidenceError(f"code must be one of: {', '.join(MEMORY_CODES)}")
        cleaned_note = _clean_text(note, "note", maximum=MAX_NOTE_CHARS)
        cleaned_actor = _clean_text(actor, "actor", maximum=200)
        if actor_kind not in ("human", "ai"):
            raise EvidenceError("actor_kind must be human or ai")
        if quote is not None and sentence_range is not None:
            raise EvidenceError("Supply either quote or sentence_range, not both")
        if (quote is not None or sentence_range is not None) and citation is None:
            raise EvidenceError("quote and sentence_range require an exact citation")
        related = list(dict.fromkeys(related_citations or []))
        for rc in related:
            if not isinstance(rc, str) or not _CITATION.fullmatch(rc):
                raise EvidenceError("Each related_citation must be an exact evidence:cv_<64 hex>#<block> ID")
        if review_id is not None and (not isinstance(review_id, str) or not _RV_ID.fullmatch(review_id)):
            raise EvidenceError("review_id must be an exact rv_<64 hex> identifier")

        sent_hashes: list[str] = []
        if citation is not None:
            if not isinstance(citation, str) or not _CITATION.fullmatch(citation):
                raise EvidenceError("citation must be an exact evidence:cv_<64 hex>#<block> identifier")
            if store is None:
                raise EvidenceError("A read-only Store is required to verify citation anchors")
            item = store.get(citation)
            src_doc, src_role = item["source"]["document_id"], item["source"]["role"]
            if document_id is not None and document_id != src_doc:
                raise EvidenceError("Supplied document_id conflicts with the citation's archived source")
            if role is not None and role != src_role:
                raise EvidenceError("Supplied role conflicts with the citation's archived source")
            document_id, role = src_doc, src_role
            if sentence_range is not None:
                first, last = sentence_range
                quote = store.sentence_quote(citation, first, last)
            if quote is not None:
                sent_hashes = quote_sentence_hashes(item["block"]["text"], quote, require_match=True)
        else:
            if document_id is None:
                raise EvidenceError("Provide either citation or document_id")
            document_id = _safe_id(document_id, "document_id")
            if store is not None:
                matching = [s for s in store.sources() if s["document_id"] == document_id
                            and (role is None or s["role"] == role)]
                if not matching:
                    raise EvidenceError("Requested document_id/role does not exist in the evidence store")
                roles = {s["role"] for s in matching}
                if role is None and len(roles) > 1:
                    raise EvidenceError("Multiple source roles share document_id; specify --role explicitly")
                role = role or next(iter(roles))
            else:
                role = _safe_id(role or "primary", "role")

        if store is not None:
            for rc in related:
                store.get(rc)
            if review_id is not None:
                ev = store._load("reviews", review_id)
                if citation is not None and ev["citation"] != citation:
                    raise EvidenceError("review_id belongs to a different citation")

        dedup_key = (
            document_id, role, citation, tuple(sent_hashes), tuple(related), code, cleaned_note, review_id,
        )

        def build(events: list[dict], active: list[dict]):
            for existing in active:
                key = (existing["document_id"], existing["role"], existing["citation"],
                       tuple(existing["sentence_sha256s"]), tuple(existing["related_citations"]),
                       existing["code"], existing["note"], existing["review_id"])
                if key == dedup_key:
                    return {**existing, "deduplicated": True}, None
            record = {
                "schema": 1,
                "kind": "add",
                "sequence": len(events) + 1,
                "previous_memory_id": events[-1]["memory_id"] if events else None,
                "code": code,
                "document_id": document_id,
                "role": role,
                "citation": citation,
                "sentence_sha256s": sent_hashes,
                "related_citations": related,
                "note": cleaned_note,
                "review_id": review_id,
                "actor": cleaned_actor,
                "actor_kind": actor_kind,
                "identity_assurance": "self_declared_local",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            return None, record

        return self._append_locked(build)

    def resolve(self, target_memory_id: str, *, reason: str, actor: str, actor_kind: str) -> dict:
        if not isinstance(target_memory_id, str) or not _MEM_ID.fullmatch(target_memory_id):
            raise EvidenceError("target_memory_id must be an exact mem_<64 hex> identifier")
        cleaned_reason = _clean_text(reason, "reason", maximum=MAX_NOTE_CHARS)
        cleaned_actor = _clean_text(actor, "actor", maximum=200)
        if actor_kind not in ("human", "ai"):
            raise EvidenceError("actor_kind must be human or ai")

        def build(events: list[dict], active: list[dict]):
            if all(entry["memory_id"] != target_memory_id for entry in active):
                raise EvidenceError("Target memory note does not exist or is already resolved")
            record = {
                "schema": 1,
                "kind": "resolve",
                "sequence": len(events) + 1,
                "previous_memory_id": events[-1]["memory_id"] if events else None,
                "target_memory_id": target_memory_id,
                "reason": cleaned_reason,
                "actor": cleaned_actor,
                "actor_kind": actor_kind,
                "identity_assurance": "self_declared_local",
                "timestamp": datetime.now(UTC).isoformat(),
            }
            return None, record

        return self._append_locked(build)

    def list_notes(self, *, document_id: str | None = None, citation: str | None = None,
                   code: str | None = None, include_history: bool = False) -> dict:
        events, active = self._load_verified()
        items = [
            entry for entry in active
            if (document_id is None or entry["document_id"] == document_id)
            and (citation is None or entry["citation"] == citation)
            and (code is None or entry["code"] == code)
        ]
        head = {"sequence": events[-1]["sequence"], "memory_id": events[-1]["memory_id"]} if events else None
        result: dict[str, Any] = {
            "head": head,
            "active_count": len(items),
            "items": items,
            "scientifically_verified": False,
        }
        if include_history:
            result["events"] = events
        return result

    def audit(self) -> dict:
        errors: list[str] = []
        events, active = [], []
        try:
            events, active = self._load_verified()
        except (EvidenceError, OSError, ValueError) as exc:
            errors.append(str(exc))
        head = {"sequence": events[-1]["sequence"], "memory_id": events[-1]["memory_id"]} if events else None
        return {
            "ok": not errors,
            "errors": errors,
            "head": head,
            "counts": {"events": len(events), "active": len(active)},
            "scientifically_verified": False,
            "identity_assurance": "self_declared_local",
        }
