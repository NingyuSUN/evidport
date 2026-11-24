"""Batch extraction-defect scanning and Validator semantic-report memory import.

``scan_store`` inspects conversions offline and pinpoints the ~0.5% of sentences
with text-layer character corruption or OCR disagreements so humans never inspect
clean paragraphs sentence by sentence.

``import_validator_report`` converts BioAI Evidence Validator semantic findings
(BEV027/028/029) into structured, deduplicated ``MemoryLedger`` reading notes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .memory import _CITATION, _LOCAL_PATH, MAX_NOTE_CHARS, MEMORY_CODES, MemoryLedger
from .quoting import sentences
from .store import EvidenceError, Store, _matches
from .verification import collapsed_positions, lenient, text_defects

HIGH_DEFECTS = frozenset({
    "independent_reading_differs",
    "control_character",
    "invisible_character",
    "unmapped_glyph",
    "detached_diacritic",
    "missing_word_space",
})
TYPOGRAPHY_DEFECTS = frozenset({"symbol_font_letter", "superscript_position_lost"})
RULE_TO_MEMORY_CODE = {
    "BEV027": "numbers_denominators",
    "BEV028": "direction_comparator",
    "BEV029": "causal_strength",
    "numbers_denominators": "numbers_denominators",
    "direction_comparator": "direction_comparator",
    "causal_strength": "causal_strength",
}
VALID_IMPORT_VERDICTS = ("contradicted", "not_established", "uncertain")


def _pages_of(locator: dict) -> list[int]:
    pages = set(locator.get("pages", []))
    if isinstance(locator.get("page"), int):
        pages.add(locator["page"])
    for region in locator.get("regions", []):
        if isinstance(region, dict) and isinstance(region.get("page"), int):
            pages.add(region["page"])
    return sorted(pages)


def _typography_flags(text: str, block: dict) -> tuple[list[tuple[int, int, str]], bool]:
    """Return (collapsed_ranges, typography_recorded) for superscripts and symbol fonts."""
    if "superscripts" not in block or "symbol_font_spans" not in block:
        return [], False
    try:
        positions = collapsed_positions(text)
        out: list[tuple[int, int, str]] = []
        mapping = (("superscripts", "superscript_position_lost"), ("symbol_font_spans", "symbol_font_letter"))
        for field, code in mapping:
            spans = block[field]
            if not isinstance(spans, list):
                return [], False
            for start, end in spans:
                if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
                    return [], False
                out.append((positions[start], positions[end - 1] + 1, code))
        return out, True
    except (KeyError, TypeError, ValueError):
        return [], False


def scan_store(
    store: Store,
    *,
    document_id: str | None = None,
    conversion_id: str | None = None,
    include_typography: bool = False,
    limit: int = 200,
) -> dict:
    """Scan archived conversions read-only for sentence-level OCR mismatches and defects."""
    if type(include_typography) is not bool:
        raise EvidenceError("include_typography must be a boolean")
    if type(limit) is not int or not 1 <= limit <= 5000:
        raise EvidenceError("limit must be an integer between 1 and 5000")

    if conversion_id is not None:
        conv = store.conversion(conversion_id)
        if document_id is not None and store.source(conv["source_id"])["document_id"] != document_id:
            raise EvidenceError("Requested conversion does not belong to document_id")
        conversions = [conv]
    else:
        conversions = store.conversions(document_id)

    eligible = [c for c in conversions if c["status"] != "failed"]
    flagged_sentences: list[dict] = []
    conversion_summaries: list[dict] = []
    totals = {
        "conversions_scanned": len(eligible),
        "blocks_scanned": 0,
        "table_blocks": 0,
        "unlocated_blocks": 0,
        "paragraphs_with_ocr": 0,
        "paragraphs_without_ocr": 0,
        "sentences_scanned": 0,
        "ocr_agreed_sentences": 0,
        "high_severity_sentences": 0,
        "typography_only_sentences": 0,
    }

    for conv in eligible:
        cid = conv["conversion_id"]
        src = store.source(conv["source_id"])
        doc_id, role = src["document_id"], src["role"]
        conv_high, conv_typo, conv_sents = 0, 0, 0
        for block in conv["blocks"]:
            totals["blocks_scanned"] += 1
            citation = f"evidence:{cid}#{block['id']}"
            pages = _pages_of(block.get("locator") or {})
            if block.get("location_accuracy") == "unlocated":
                totals["unlocated_blocks"] += 1
            if block.get("kind") == "table":
                totals["table_blocks"] += 1
                continue
            text = block.get("text") or ""
            collapsed = " ".join(text.split())
            if not collapsed:
                continue
            spans = sentences(text)
            char_flags = text_defects(collapsed)
            typo_flags, _ = _typography_flags(text, block)
            reading = block.get("independent_reading")
            ocr_text = reading.get("text") if isinstance(reading, dict) else None
            has_ocr = isinstance(ocr_text, str)
            if block.get("kind") == "paragraph":
                if has_ocr:
                    totals["paragraphs_with_ocr"] += 1
                else:
                    totals["paragraphs_without_ocr"] += 1
            folded_ocr = lenient(ocr_text) if has_ocr else None

            for idx, (start, end) in enumerate(spans, 1):
                totals["sentences_scanned"] += 1
                conv_sents += 1
                sent_text = collapsed[start:end]
                high_codes = {
                    code for low, high, code in char_flags if low < end and high > start
                }
                typo_codes = {
                    code for low, high, code in typo_flags if low < end and high > start
                }
                ocr_agrees = None
                if folded_ocr is not None:
                    ocr_agrees = _matches(folded_ocr, lenient(sent_text))
                    if not ocr_agrees:
                        high_codes.add("independent_reading_differs")
                    elif not high_codes and not typo_codes:
                        totals["ocr_agreed_sentences"] += 1

                if high_codes:
                    totals["high_severity_sentences"] += 1
                    conv_high += 1
                elif typo_codes:
                    totals["typography_only_sentences"] += 1
                    conv_typo += 1

                should_emit = bool(high_codes or (include_typography and typo_codes))
                if should_emit and len(flagged_sentences) < limit:
                    entry: dict[str, Any] = {
                        "severity": "high" if high_codes else "medium",
                        "document_id": doc_id,
                        "role": role,
                        "conversion_id": cid,
                        "citation": citation,
                        "pages": pages,
                        "sentence": idx,
                        "codes": sorted(high_codes | typo_codes),
                        "text": sent_text[:500],
                    }
                    if ocr_agrees is False and isinstance(ocr_text, str):
                        entry["ocr_block_excerpt"] = " ".join(ocr_text.split())[:500]
                    flagged_sentences.append(entry)

        conversion_summaries.append({
            "conversion_id": cid,
            "document_id": doc_id,
            "role": role,
            "status": conv["status"],
            "issues": list(conv.get("issues") or []),
            "sentences_scanned": conv_sents,
            "high_severity_sentences": conv_high,
            "typography_only_sentences": conv_typo,
        })

    flagged_sentences.sort(key=lambda r: (0 if r["severity"] == "high" else 1, r["citation"], r["sentence"]))
    total_matching = totals["high_severity_sentences"] + (
        totals["typography_only_sentences"] if include_typography else 0
    )
    return {
        "summary": totals,
        "conversions": conversion_summaries,
        "flagged_sentences": flagged_sentences,
        "returned_flagged": len(flagged_sentences),
        "truncated": total_matching > len(flagged_sentences),
        "include_typography": include_typography,
        "scientifically_verified": False,
    }


def _sanitize_for_note(text: str) -> str:
    cleaned = "".join(" " if ord(ch) < 32 or 127 <= ord(ch) <= 159 else ch for ch in text)
    cleaned = _LOCAL_PATH.sub("[path-redacted]", " ".join(cleaned.split()))
    return cleaned


def _format_note(verdict: str, facet_name: str, facet: dict, siblings: list[str]) -> str:
    parts = [f"[{verdict}]"]
    source_fact = _sanitize_for_note(str(facet.get("source_fact") or ""))
    reason = _sanitize_for_note(str(facet.get("reason") or ""))
    if source_fact:
        parts.append(f"Source: {source_fact}")
    if reason:
        parts.append(f"Check {facet_name}: {reason}")
    if siblings:
        parts.append("Cross-check " + ", ".join(s.split("#", 1)[-1] for s in siblings[:3]) + ".")
    joined = " ".join(parts).strip()
    if len(joined) <= MAX_NOTE_CHARS:
        return joined
    return joined[: MAX_NOTE_CHARS - 3].rstrip() + "..."


def _extract_from_validate_report(
    report: dict, verdicts_set: set[str]
) -> list[dict]:
    sem = report.get("semantic_review")
    if not isinstance(sem, dict):
        return []
    checks = sem.get("checks")
    if not isinstance(checks, dict):
        return []
    validity = sem.get("facet_validity") if isinstance(sem.get("facet_validity"), dict) else {}
    reviewed = sem.get("reviewed_quotes") if isinstance(sem.get("reviewed_quotes"), list) else []

    fallback_citations = [
        row["citation"]
        for section in ("evidence_library", "evidence_mcp")
        for row in (report.get(section) or {}).get("checks", [])
        if (isinstance(row, dict) and isinstance(row.get("citation"), str)
            and _CITATION.fullmatch(row["citation"]))
    ]

    candidates: list[dict] = []
    for dim_name, mem_code in (
        ("numbers_denominators", "numbers_denominators"),
        ("direction_comparator", "direction_comparator"),
        ("causal_strength", "causal_strength"),
    ):
        if validity and validity.get(dim_name) is False:
            continue
        facet = checks.get(dim_name)
        if not isinstance(facet, dict) or facet.get("verdict") not in verdicts_set:
            continue
        verdict = facet["verdict"]
        ev_list = facet.get("evidence") if isinstance(facet.get("evidence"), list) else []
        resolved_anchors: list[tuple[str, str | None]] = []
        for ev in ev_list:
            if not isinstance(ev, dict):
                continue
            matched = [
                rq for rq in reviewed
                if isinstance(rq, dict)
                and rq.get("source") == ev.get("source")
                and rq.get("block_id") == ev.get("block_id")
                and (rq.get("facet") in (None, dim_name))
                and isinstance(rq.get("citation"), str)
                and _CITATION.fullmatch(rq["citation"])
            ]
            if matched:
                rq = matched[0]
                quote = rq.get("quote") if rq.get("quote_origin") != "source_block" else ev.get("quote")
                resolved_anchors.append((rq["citation"], quote if isinstance(quote, str) else None))
        if not resolved_anchors and fallback_citations:
            resolved_anchors = [(c, None) for c in dict.fromkeys(fallback_citations)]
        if not resolved_anchors:
            continue
        primary_cit, primary_quote = resolved_anchors[0]
        siblings = [c for c, _ in resolved_anchors[1:] if c != primary_cit]
        code = "source_inconsistency" if verdict == "uncertain" and "conflict" in str(
            facet.get("reason", "")
        ).lower() else mem_code
        candidates.append({
            "code": code,
            "citation": primary_cit,
            "quote": primary_quote,
            "related_citations": siblings,
            "note": _format_note(verdict, dim_name, facet, siblings),
        })
    return candidates


def _extract_from_receipt(
    receipt: dict, request: dict, verdicts_set: set[str]
) -> list[dict]:
    sources = {
        s["id"]: s["conversion_id"]
        for s in (request.get("sources") or [])
        if isinstance(s, dict) and isinstance(s.get("id"), str) and isinstance(s.get("conversion_id"), str)
    }
    results = ((receipt.get("response") or {}).get("results")) or []
    if not sources or not isinstance(results, list):
        raise EvidenceError("Semantic receipt import requires matching --request with sources and results")
    candidates: list[dict] = []
    for row in results:
        if not isinstance(row, dict) or not isinstance(row.get("checks"), dict):
            continue
        for dim_name in ("numbers_denominators", "direction_comparator", "causal_strength"):
            facet = row["checks"].get(dim_name)
            if not isinstance(facet, dict) or facet.get("verdict") not in verdicts_set:
                continue
            verdict = facet["verdict"]
            anchors: list[tuple[str, str | None]] = []
            for ev in facet.get("evidence") or []:
                if not isinstance(ev, dict):
                    continue
                conv_id = sources.get(ev.get("source"))
                block_id = ev.get("block_id")
                if not conv_id or not isinstance(block_id, str):
                    continue
                cit = f"evidence:{conv_id}#{block_id}"
                if _CITATION.fullmatch(cit):
                    quote = ev.get("quote") if isinstance(ev.get("quote"), str) else None
                    anchors.append((cit, quote))
            if not anchors:
                continue
            primary_cit, primary_quote = anchors[0]
            siblings = [c for c, _ in anchors[1:] if c != primary_cit]
            code = RULE_TO_MEMORY_CODE[dim_name]
            candidates.append({
                "code": code,
                "citation": primary_cit,
                "quote": primary_quote,
                "related_citations": siblings,
                "note": _format_note(verdict, dim_name, facet, siblings),
            })
    return candidates


def load_report_payloads(path: Path) -> list[dict]:
    raw = Path(path).read_text(encoding="utf-8-sig").strip()
    if not raw:
        raise EvidenceError("Validator report file is empty")
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list) and all(isinstance(x, dict) for x in parsed):
            return parsed
        raise EvidenceError("Validator report JSON must be an object or list of objects")
    except json.JSONDecodeError:
        rows = []
        for line in raw.splitlines():
            if line.strip():
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise EvidenceError("Each JSONL line must be a JSON object")
                rows.append(item)
        if not rows:
            raise EvidenceError("No valid JSON objects found in Validator report")
        return rows


def import_validator_report(
    payloads: list[dict],
    *,
    store: Store,
    ledger: MemoryLedger | None,
    actor: str = "validator_semantic_import",
    actor_kind: str = "ai",
    request_data: dict | None = None,
    verdicts: tuple[str, ...] = VALID_IMPORT_VERDICTS,
    dry_run: bool = False,
) -> dict:
    """Extract semantic pitfalls from Validator reports/receipts and optionally commit."""
    bad_verdicts = set(verdicts) - set(VALID_IMPORT_VERDICTS)
    if not verdicts or bad_verdicts:
        raise EvidenceError(f"verdicts must be drawn from {VALID_IMPORT_VERDICTS}")
    if not dry_run and ledger is None:
        raise EvidenceError("ledger is required unless dry_run=True")

    verdicts_set = set(verdicts)
    extracted: list[dict] = []
    for payload in payloads:
        if "semantic_review" in payload:
            extracted.extend(_extract_from_validate_report(payload, verdicts_set))
        elif "response" in payload and "contract_version" in payload:
            if request_data is None:
                raise EvidenceError("Raw semantic receipt requires --request REQUEST_JSON")
            extracted.extend(_extract_from_receipt(payload, request_data, verdicts_set))

    added, deduplicated, skipped = [], [], []
    preview: list[dict] = []
    for cand in extracted:
        if cand["code"] not in MEMORY_CODES:
            continue
        try:
            item = store.get(cand["citation"])
            for rc in cand["related_citations"]:
                store.get(rc)
        except EvidenceError as exc:
            skipped.append({"citation": cand["citation"], "reason": str(exc)})
            continue

        quote = cand["quote"]
        # If the supplied quote does not match verbatim (e.g. truncated), fall back to block-level note.
        if (
            quote is not None
            and not store.check(cand["citation"], quote, require_review=False)["quote_matches"]
        ):
            quote = None

        if dry_run:
            preview.append({
                "code": cand["code"],
                "document_id": item["source"]["document_id"],
                "role": item["source"]["role"],
                "citation": cand["citation"],
                "quote_anchored": quote is not None,
                "related_citations": cand["related_citations"],
                "note": cand["note"],
            })
        else:
            assert ledger is not None
            res = ledger.add(
                code=cand["code"],
                note=cand["note"],
                actor=actor,
                actor_kind=actor_kind,
                citation=cand["citation"],
                quote=quote,
                related_citations=cand["related_citations"],
                store=store,
            )
            if res.get("deduplicated"):
                deduplicated.append(res["memory_id"])
            else:
                added.append(res["memory_id"])

    return {
        "dry_run": dry_run,
        "candidates_found": len(extracted),
        "added_count": len(added),
        "deduplicated_count": len(deduplicated),
        "skipped_count": len(skipped),
        "added_memory_ids": added,
        "deduplicated_memory_ids": deduplicated,
        "skipped": skipped,
        **({"preview": preview} if dry_run else {}),
        "scientifically_verified": False,
    }
