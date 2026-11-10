"""Standalone parser adapters; old outputs are evidence, never runtime imports.

The pilot formats and TEI walking approach derive from the user's local
bioai-evidence-validator tools/pilot_*.py (2026-11-06). No code or environments
from that project are imported. Native files remain immutable snapshots;
adapter-normalized coordinates are PDF points with a top-left origin.
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import hashlib
import importlib.metadata
import json
import math
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .store import EvidenceError

ADAPTER_VERSION = "1"


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _dump(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _verify(path: Path, expected: str, label: str) -> None:
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise EvidenceError(f"{label}: required SHA256 missing or invalid")
    if not path.is_file() or _sha(path) != expected.lower():
        raise EvidenceError(f"{label}: SHA256 mismatch or missing file: {path.name}")


def _relative_file(folder: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if (relative.is_absolute() or not relative.parts or ".." in relative.parts
            or "\\" in name or ":" in name):
        raise EvidenceError(f"Unsafe artifact path: {name!r}")
    path = folder.joinpath(*relative.parts)
    if not path.resolve().is_relative_to(folder.resolve()):
        raise EvidenceError("Artifact resolves outside import folder")
    return path


def _pdf_dimensions(path: Path) -> dict[int, tuple[float, float]]:
    import pymupdf

    with pymupdf.open(path) as pdf:
        return {i: (float(page.cropbox.width), float(page.cropbox.height)) for i, page in enumerate(pdf, 1)}


def _pdf_rotations(path: Path) -> dict[int, int]:
    import pymupdf

    with pymupdf.open(path) as pdf:
        return {i: page.rotation for i, page in enumerate(pdf, 1)}


def _pdf_crop_offsets(path: Path) -> dict[int, bool]:
    import pymupdf

    with pymupdf.open(path) as pdf:
        return {i: bool(page.cropbox.x0 or page.cropbox.y0) for i, page in enumerate(pdf, 1)}


def _bbox(box, page: int, dimensions: dict, parser: str, version: str) -> list | None:
    if box is None:
        return None
    if page not in dimensions:
        raise EvidenceError(f"Invalid physical page for bbox: {page}")
    width, height = dimensions[page]
    if isinstance(box, dict):
        origin = box.get("coord_origin", "").upper().replace("_", "")
        if origin == "BOTTOMLEFT":
            values = [box["l"], height - box["t"], box["r"], height - box["b"]]
        elif origin == "TOPLEFT":
            values = [box["l"], box["t"], box["r"], box["b"]]
        else:
            return None  # Keep native geometry; no guess about coordinate origin.
    elif parser == "mineru":
        # MinerU 4.1.7 structured_content uses 0..1 top-left x0,y0,x1,y1.
        # Its content_list export is different (0..1000) and is not accepted here.
        if version != "4.1.7" or len(box) != 4 or not all(0 <= x <= 1 for x in box):
            return None
        values = [box[0] * width, box[1] * height, box[2] * width, box[3] * height]
    else:
        values = list(box)
    if len(values) != 4 or any(not isinstance(x, (float, int)) or not math.isfinite(x) for x in values):
        raise EvidenceError("Invalid or non-finite bbox")
    x0, y0, x1, y1 = values
    if x0 < 0 or y0 < 0 or x1 <= x0 or y1 <= y0 or x1 > width + 0.01 or y1 > height + 0.01:
        raise EvidenceError("Native bbox outside physical page or empty/reversed")
    return values


def _normalize_blocks(rows: list, dimensions: dict, parser: str, version: str,
                      rotations: dict | None = None, crop_offsets: dict | None = None) -> list[dict]:
    blocks = []
    ids = set()
    for index, original in enumerate(rows, 1):
        block = copy.deepcopy(original)
        original_id = block.get("id", f"b{index:06d}")
        if original_id in ids:
            raise EvidenceError(f"Duplicate native block ID: {original_id}")
        ids.add(original_id)
        block["native_id"] = original_id
        block["id"] = f"b{index:06d}"  # Citation-safe ID, stable for this frozen conversion.
        native = copy.deepcopy(original.get("locator") or {})
        if original.get("native_locator") is not None:
            native["parser_reference"] = copy.deepcopy(original["native_locator"])
        block["native_locator"] = native
        issues = list(block.get("issues") or [])
        page = native.get("page")
        pages = native.get("pages") or ([page] if page is not None else [])
        regions = native.get("regions") or ([{"page": page, "bbox": native.get("bbox")}]
                                             if page is not None else [])
        canonical_regions = []
        for region in regions:
            number = region.get("page")
            if number is None:
                issues.append("region_missing_physical_page")
                continue
            if isinstance(number, bool) or not isinstance(number, int) or number not in dimensions:
                raise EvidenceError(f"Invalid physical page: {number}")
            item = {"page": number}
            box = None
            has_box = region.get("bbox") is not None
            if has_box and (rotations or {}).get(number, 0):
                # Historical parser outputs mix displayed and unrotated axes,
                # sometimes within one page. No unevidenced rotation transform.
                issues.append("native_bbox_rotation_unverified_page_only")
            elif (has_box and (crop_offsets or {}).get(number, False)
                  and (parser != "baseline" or block.get("kind") == "table")):
                issues.append("native_bbox_cropbox_offset_unverified_page_only")
            else:
                try:
                    box = _bbox(region.get("bbox"), number, dimensions, parser, version)
                except (EvidenceError, KeyError, TypeError, ValueError):
                    issues.append("native_bbox_out_of_bounds_page_only")
            if box is not None:
                item["bbox"] = box
            elif has_box and not any(issue.startswith("native_bbox_") for issue in issues):
                issues.append("native_coordinate_convention_unverified")
            if region.get("charspan") is not None:
                item["charspan"] = region["charspan"]
            canonical_regions.append(item)
        pages = sorted(set(pages) | {r["page"] for r in canonical_regions})
        if any(isinstance(p, bool) or not isinstance(p, int) or p not in dimensions for p in pages):
            raise EvidenceError("Invalid physical page list")
        locator = {"origin": "top_left", "coordinate_units": "pdf_points"}
        if pages:
            locator.update(page=pages[0], pages=pages)
            geometric_regions = [r for r in canonical_regions if "bbox" in r]
            if geometric_regions:
                locator["regions"] = geometric_regions
                primary = next((r for r in geometric_regions if r["page"] == pages[0]), None)
                if primary:
                    locator["bbox"] = primary["bbox"]
            if "bbox" not in locator:
                locator["location_accuracy"] = "page"
        else:
            locator["unlocated"] = True
            issues.append("unlocated")
        block.update(locator=locator, issues=list(dict.fromkeys(issues)))
        block.setdefault("kind", "unknown")
        block.setdefault("text", "")
        blocks.append(block)
    return blocks


def _geometry_fallback(blocks: list[dict]) -> bool:
    return any(issue.startswith("native_bbox_") or issue == "native_coordinate_convention_unverified"
               for block in blocks for issue in block.get("issues", []) if isinstance(issue, str))


def _folder_artifacts(folder: Path) -> dict[str, Path]:
    # Only selected document files; never import a parser environment/cache.
    result = {}
    for name in ("result.json", "receipt.json", "blocks.json", "document.json", "document.md",
                 "document.tei.xml", "structured_content.json", "error.log", "failure.log"):
        path = _relative_file(folder, name)
        if path.is_file():
            result[name] = path
    native = folder / "native"
    if native.is_dir():
        for path in sorted(native.rglob("*")):
            if path.is_file():
                name = path.relative_to(folder).as_posix()
                result[name] = _relative_file(folder, name)
    return result


def _pilot_one(store, source: dict, folder: Path, parser: str, manifest: Path, dimensions: dict,
               run_provenance=None) -> dict:
    # Verify the exact bytes subsequently handed to Store, even if another
    # process changes the old parser directory during an import.
    with tempfile.TemporaryDirectory(prefix="evidence-import-") as temporary:
        frozen = Path(temporary)
        try:
            for name, path in _folder_artifacts(folder).items():
                target = frozen / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
            shutil.copyfile(manifest, frozen / "pilot_manifest.json")
        except (EvidenceError, OSError) as exc:
            return store.add_conversion(
                source["source_id"], parser=parser, parser_version="unknown", blocks=[], status="failed",
                settings={"execution": "reused_historical_output", "adapter_version": ADAPTER_VERSION},
                artifacts={"pilot_manifest.json": manifest}, issues=[f"import_snapshot_failed: {exc}"])
        return _pilot_snapshot(store, source, frozen, parser, frozen / "pilot_manifest.json", dimensions,
                               original_folder=folder, run_provenance=run_provenance)


def _pilot_snapshot(store, source: dict, folder: Path, parser: str, manifest: Path, dimensions: dict,
                    *, original_folder: Path, run_provenance=None) -> dict:
    result, receipt, version = {}, {}, "unknown"
    artifacts = {"pilot_manifest.json": manifest}
    settings = {"execution": "reused_historical_output", "adapter_version": ADAPTER_VERSION,
                "imported_from": str(original_folder.resolve()), "historical_provenance": "partial"}
    issues = []
    try:
        artifacts.update(_folder_artifacts(folder))
        if run_provenance:
            required = {"script_path", "script_sha256", "command", "run_receipt_path", "run_receipt_sha256",
                        "source_sha256", "output_sha256"}
            if not required.issubset(run_provenance) or not run_provenance["command"]:
                raise EvidenceError("Fresh external import requires runner, command, run receipt and hashes")
            if run_provenance["source_sha256"] != source["sha256"]:
                raise EvidenceError("Fresh external import source SHA256 mismatch")
            script = Path(run_provenance["script_path"])
            runner = folder / "runner.py"
            shutil.copyfile(script, runner)
            _verify(runner, run_provenance["script_sha256"], "fresh parser script")
            artifacts["runner.py"] = runner
            run_receipt = folder / "external_run.json"
            shutil.copyfile(Path(run_provenance["run_receipt_path"]), run_receipt)
            _verify(run_receipt, run_provenance.get("run_receipt_sha256"), "fresh run receipt")
            artifacts["external_run.json"] = run_receipt
            proof = _json(run_receipt)
            candidates = [proof] + proof.get("results", [])
            matches = [row for row in candidates
                       if row.get("sha256", row.get("source_sha256")) == source["sha256"]]
            expected_outputs = run_provenance["output_sha256"]
            if (not matches or "blocks.json" not in expected_outputs
                    or not any(row.get("output_sha256") == expected_outputs for row in matches)):
                raise EvidenceError("Fresh run receipt does not bind this source and exact output hashes")
            for name, digest in expected_outputs.items():
                _verify(_relative_file(folder, name), digest, f"fresh output {name}")
            settings.update(execution="fresh_external_import", run_provenance=run_provenance)
        result = _json(folder / "result.json")
        if (folder / "receipt.json").is_file():
            receipt = _json(folder / "receipt.json")
        versions = receipt.get("versions") or result.get("versions") or {}
        version = result.get("parser_version") or versions.get(parser) or "unknown"
        settings.update(parser_settings=receipt.get("settings", result.get("settings", {})),
                        versions=versions,
                        original_status=result.get("status"),
                        mode=result.get("mode"), tier=result.get("tier"))
        if result.get("source_sha256", result.get("sha256")) != source["sha256"]:
            raise EvidenceError("Parser source SHA256 binding mismatch or missing")
        if receipt and receipt.get("sha256", receipt.get("source_sha256")) != source["sha256"]:
            raise EvidenceError("Native receipt source SHA256 binding mismatch")
        if result.get("status") not in {"ok", "success", "completed"}:
            raise EvidenceError(f"Historical parser status={result.get('status')}: {result.get('error', '')}")
        _verify(folder / "blocks.json", result.get("blocks_sha256"), "blocks")
        hashes = receipt.get("output_sha256", {})
        for name, digest in hashes.items():
            path = _relative_file(folder, name)
            _verify(path, digest, f"native output {name}")
            artifacts[name] = path
        rows = _json(folder / "blocks.json")
        if not isinstance(rows, list) or not rows:
            raise EvidenceError("No parsed blocks")
        if parser == "grobid":
            _verify(folder / "document.tei.xml", result.get("native_sha256"), "native TEI")
            settings["historical_provenance"] = "source_blocks_native_hashes"
        elif parser == "docling":
            if "document.json" not in hashes:
                raise EvidenceError("Required Docling native hash missing")
            settings["historical_provenance"] = "source_blocks_native_hashes"
        elif parser == "baseline":
            if ((folder / "document.json").is_file()
                    and _json(folder / "document.json").get("blocks") != rows):
                raise EvidenceError("Baseline native document/blocks mismatch")
            issues.append("historical_native_hashes_unavailable")
        elif parser == "mineru":
            native = folder / "native" / "structured_content.json"
            if not native.is_file():
                raise EvidenceError("MinerU native structured content missing")
            if ((folder / "structured_content.json").is_file()
                    and _json(native) != _json(folder / "structured_content.json")):
                raise EvidenceError("MinerU native export copies disagree")
            issues.append("historical_native_hashes_unavailable")
        source_path = Path(source["path"])
        blocks = _normalize_blocks(rows, dimensions, parser, version, _pdf_rotations(source_path),
                                   _pdf_crop_offsets(source_path))
        issues.extend(result.get("issues") or [])
        issues.extend(receipt.get("adapter_issues") or [])
        status = "partial" if _geometry_fallback(blocks) else "completed"
        if status == "partial":
            issues.append("native_geometry_requires_page_only_review")
        return store.add_conversion(source["source_id"], parser=parser, parser_version=version,
                                    blocks=blocks, artifacts=artifacts, settings=settings,
                                    issues=issues, status=status)
    except (EvidenceError, OSError, ValueError, TypeError, KeyError) as exc:
        issues.append(f"import_failed: {type(exc).__name__}: {exc}")
        return store.add_conversion(source["source_id"], parser=parser, parser_version=version,
                                    blocks=[], artifacts=artifacts, settings=settings,
                                    issues=issues, status="failed")


def import_pilot(store, manifest: Path, parser_roots: dict[str, Path], *, run_provenance=None) -> dict:
    """Import frozen pilot outputs; corrupted/missing conversions become failures.

    All source hashes are checked before ingestion, preventing a partially
    imported source batch on a wrong manifest. ``parser_roots`` can point to
    either the parser directory or its ``results`` child.
    """
    manifest = Path(manifest).resolve()
    rows = _json(manifest)["documents"]
    if not parser_roots or set(parser_roots) - {"baseline", "grobid", "docling", "mineru"}:
        raise EvidenceError("Select baseline/grobid/docling/mineru parser roots")
    prepared = []
    identifiers = set()
    for row in rows:
        identifier = row["id"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", identifier) or identifier in identifiers:
            raise EvidenceError("Unsafe or duplicate document ID")
        identifiers.add(identifier)
        path = Path(row["path"])
        if not path.is_absolute():
            path = manifest.parent / path
        _verify(path, row.get("sha256"), "source")
        dimensions = _pdf_dimensions(path)
        if row.get("pages") is not None and row["pages"] != len(dimensions):
            raise EvidenceError("Source PDF page count does not match manifest")
        prepared.append((row, path, dimensions))
    report = {"sources": [], "conversions": [], "completed": 0, "partial": 0, "failed": 0}
    for row, path, dimensions in prepared:
        source = store.ingest(path, row["id"], role=row.get("role", "primary"),
                              metadata={"pilot_manifest": str(manifest), "manifest_sha256": _sha(manifest),
                                        "original_source_path": row.get("source_path"),
                                        "selection": _json(manifest).get("selection")})
        report["sources"].append(source)
        for parser, root in parser_roots.items():
            root = Path(root)
            if (root / "results").is_dir():
                root = root / "results"
            folder = root / row["id"]
            if not folder.resolve().is_relative_to(root.resolve()):
                conversion = store.add_conversion(
                    source["source_id"], parser=parser, parser_version="unknown", blocks=[], status="failed",
                    settings={"execution": "reused_historical_output", "adapter_version": ADAPTER_VERSION},
                    issues=["import_failed: Document folder resolves outside declared parser root"],
                    artifacts={"pilot_manifest.json": manifest})
            else:
                conversion = _pilot_one(store, source, folder, parser, manifest, dimensions, run_provenance)
            report["conversions"].append({"document_id": row["id"], "parser": parser, **conversion})
            report[conversion["status"]] += 1
    return report


def import_legacy(store, pdf: Path, receipt: Path, *, document_id: str, run_provenance=None) -> dict:
    """Freeze every legacy input before parsing or binding blocks to artifacts."""
    pdf, receipt = Path(pdf).resolve(), Path(receipt).resolve()
    with tempfile.TemporaryDirectory(prefix="evidence-legacy-import-") as temporary:
        folder = Path(temporary)
        source_copy, receipt_copy = folder / pdf.name, folder / receipt.name
        shutil.copyfile(pdf, source_copy)
        shutil.copyfile(receipt, receipt_copy)
        for suffix in (".text.md", ".tables.tsv", ".ocr_check.tsv", ".tei.xml", ".ocr.pdf"):
            path = _relative_file(pdf.parent, pdf.stem + suffix)
            if path.is_file():
                shutil.copyfile(path, folder / path.name)
        page_folder = pdf.parent / (pdf.stem + ".pages")
        if page_folder.is_dir():
            for path in page_folder.glob("*.png"):
                relative = path.relative_to(pdf.parent).as_posix()
                checked = _relative_file(pdf.parent, relative)
                target = folder / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(checked, target)
        provenance = copy.deepcopy(run_provenance)
        if provenance:
            # Also freeze listed run logs/receipts so output-hash verification
            # checks the same bytes that can be retained in the conversion.
            for name in provenance.get("output_sha256", {}):
                checked = _relative_file(pdf.parent, name)
                target = _relative_file(folder, name)
                if checked.is_file() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(checked, target)
            for prefix in ("script", "run_receipt"):
                if provenance.get(prefix + "_path"):
                    target = folder / "provenance" / (prefix + ".snapshot")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(Path(provenance[prefix + "_path"]), target)
                    provenance["original_" + prefix + "_path"] = provenance[prefix + "_path"]
                    provenance[prefix + "_path"] = str(target)
        return _import_legacy_snapshot(store, source_copy, receipt_copy, document_id=document_id,
                                       run_provenance=provenance,
                                       original_paths={"pdf": str(pdf), "receipt": str(receipt)})


def _import_legacy_snapshot(store, pdf: Path, receipt: Path, *, document_id: str,
                            run_provenance=None, original_paths=None) -> dict:
    """Import paper_text.py page Markdown, TSV tables and unresolved OCR flags.

    Old receipts bind only the original PDF. New caller-supplied provenance can
    provide ``output_sha256`` keyed relative to the PDF folder; every supplied
    hash is verified. Neither kind of receipt creates a human approval.
    """
    pdf, receipt = Path(pdf).resolve(), Path(receipt).resolve()
    record = _json(receipt)
    _verify(pdf, record.get("sha256"), "source")
    dimensions = _pdf_dimensions(pdf)
    if record.get("n_pages", len(dimensions)) != len(dimensions):
        raise EvidenceError("Legacy page count mismatch")
    source = store.ingest(pdf, document_id, metadata={"legacy_receipt": original_paths["receipt"],
                                                     "original_source_path": original_paths["pdf"]})
    stem = pdf.with_suffix("")
    artifacts = {"receipt.json": receipt}
    issues = ["legacy_page_only_locators", "ocr_number_set_agreement_is_not_accuracy"]
    settings = {"execution": "reused_historical_output",
                "adapter_version": ADAPTER_VERSION, "run_provenance": copy.deepcopy(run_provenance or {}),
                "tools": record.get("tools", {}), "legacy_utc": record.get("utc"),
                "grobid": record.get("grobid")}
    for prefix in ("script", "run_receipt"):
        original = settings["run_provenance"].get("original_" + prefix + "_path")
        if original:
            settings["run_provenance"][prefix + "_path"] = original
    try:
        if run_provenance:
            required_proof = {"script_path", "script_sha256", "command", "run_receipt_path",
                              "run_receipt_sha256", "source_sha256", "output_sha256"}
            if not required_proof.issubset(run_provenance) or not run_provenance.get("command"):
                raise EvidenceError("Fresh legacy provenance requires runner, command, run receipt and hashes")
            if run_provenance["source_sha256"] != source["sha256"]:
                raise EvidenceError("Fresh legacy provenance source SHA256 mismatch")
            run_receipt = Path(run_provenance["run_receipt_path"])
            _verify(run_receipt, run_provenance["run_receipt_sha256"], "fresh legacy run receipt")
            proof = _json(run_receipt)
            if proof.get("sha256", proof.get("source_sha256")) != source["sha256"]:
                raise EvidenceError("Fresh legacy run receipt source SHA256 mismatch")
            artifacts["provenance/run_receipt.json"] = run_receipt
        for suffix in (".text.md", ".tables.tsv", ".ocr_check.tsv", ".tei.xml", ".ocr.pdf"):
            path = _relative_file(pdf.parent, stem.name + suffix)
            if path.is_file():
                artifacts[path.name] = path
        pages_dir = Path(str(stem) + ".pages")
        if pages_dir.is_dir():
            for path in sorted(pages_dir.glob("*.png")):
                _relative_file(pdf.parent, path.relative_to(pdf.parent).as_posix())
                artifacts[f"pages/{path.name}"] = path
        hashes = (run_provenance or {}).get("output_sha256") or record.get("output_sha256") or {}
        for name, digest in hashes.items():
            output = _relative_file(pdf.parent, name)
            _verify(output, digest, f"legacy output {name}")
            artifacts["run_outputs/" + name] = output
        if run_provenance and run_provenance.get("script_path"):
            script = Path(run_provenance["script_path"])
            _verify(script, run_provenance.get("script_sha256"), "legacy script")
            artifacts["runner/paper_text.py"] = script
        required = {path.relative_to(pdf.parent).as_posix() for name, path in artifacts.items()
                    if name != "receipt.json" and not name.startswith(("runner/", "provenance/"))
                    and path.is_relative_to(pdf.parent)}
        if not required.issubset(hashes):
            if run_provenance:
                raise EvidenceError("Fresh legacy output hashes incomplete")
            issues.append("historical_output_hashes_unavailable")
        if run_provenance:
            settings["execution"] = "fresh_external_import"
        checks_path = Path(str(stem) + ".ocr_check.tsv")
        disagreements = set()
        if checks_path.is_file() and record.get("ocr_number_disagreements", 0):
            with checks_path.open(encoding="utf-8-sig", newline="") as stream:
                check_rows = list(csv.DictReader(stream, delimiter="\t"))
                disagreements = {int(row["page"]) for row in check_rows}
            if len(check_rows) != record["ocr_number_disagreements"]:
                raise EvidenceError("OCR disagreement count does not match receipt")
        elif checks_path.is_file():
            issues.append("stale_ocr_check_file_receipt_reports_zero")
        elif record.get("ocr_number_disagreements", 0):
            issues.append("ocr_disagreement_detail_missing")
        if record.get("ocr_number_disagreements", 0):
            issues.append("ocr_disagreement_requires_review")
        text = Path(str(stem) + ".text.md").read_text(encoding="utf-8-sig")
        headings = list(re.finditer(r"^##\s+page\s+(\d+)\s*\(([^)]+)\)\s*$", text,
                                   re.MULTILINE | re.IGNORECASE))
        blocks = []
        pages = record.get("pages")
        page_rows = pages if isinstance(pages, list) else record.get("page_stats", [])
        methods = {row["page"]: row.get("method", "unknown") for row in page_rows}
        for index, heading in enumerate(headings):
            number, method = int(heading.group(1)), heading.group(2)
            if number not in dimensions:
                raise EvidenceError("Legacy Markdown page outside source PDF")
            if methods.get(number, method) != method:
                raise EvidenceError("Legacy receipt/Markdown extraction method mismatch")
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            block_issues = ["page_only_locator"]
            if method.startswith("ocr"):
                block_issues.append("ocr_requires_original_review")
            if number in disagreements:
                block_issues.append("ocr_disagreement_requires_review")
            blocks.append({"id": f"page-{number}", "kind": "paragraph",
                           "text": text[heading.end():end].strip(), "locator": {"page": number},
                           "native_locator": {"markdown_heading": heading.group(0), "method": method},
                           "issues": block_issues})
        if sorted(int(h.group(1)) for h in headings) != list(dimensions):
            raise EvidenceError("Legacy Markdown physical page coverage missing/duplicated")
        table_path = Path(str(stem) + ".tables.tsv")
        tables = {}
        if table_path.is_file():
            with table_path.open(encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream, delimiter="\t"):
                    key = (int(row["page"]), int(row["table"]))
                    if key[0] not in dimensions:
                        raise EvidenceError("Legacy table page outside source PDF")
                    tables.setdefault(key, []).append((int(row["row"]), row["cells"].split("\t")))
        for (number, table), rows in tables.items():
            cells = [cells for _, cells in sorted(rows)]
            blocks.append({"id": f"table-{number}-{table}", "kind": "table",
                           "text": "\n".join("\t".join(row_cells) for row_cells in cells), "table_cells": cells,
                           "locator": {"page": number}, "native_locator": {"table": table},
                           "issues": ["page_only_locator", "table_structure_requires_review"]
                           + (["ocr_disagreement_requires_review"] if number in disagreements else [])})
        if not any(block["text"].strip() for block in blocks):
            raise EvidenceError("Legacy conversion has no extracted evidence text")
        for name, error in record.get("tools", {}).items():
            if name.endswith("_error"):
                issues.append(f"{name}: {error}")
        return store.add_conversion(source["source_id"], parser="legacy-paper-text", parser_version="unknown",
                                    blocks=blocks, settings=settings, artifacts=artifacts, issues=issues)
    except (EvidenceError, OSError, ValueError, KeyError, TypeError) as exc:
        return store.add_conversion(source["source_id"], parser="legacy-paper-text", parser_version="unknown",
                                    blocks=[], settings=settings, artifacts=artifacts,
                                    issues=issues + [f"import_failed: {exc}"], status="failed")


def _versions(names) -> str:
    versions = []
    for name in names:
        try:
            versions.append(f"{name}={importlib.metadata.version(name)}")
        except importlib.metadata.PackageNotFoundError:
            versions.append(f"{name}=not-installed")
    return ";".join(versions)


def run_baseline(store, source_id: str) -> dict:
    """Fresh local text-layer extraction with PyMuPDF and pdfplumber tables."""
    source = store.source(source_id)
    settings = {"execution": "fresh", "ocr": False, "text_sort": "pymupdf_sort_true",
                "table_settings": {}, "adapter_version": ADAPTER_VERSION,
                "started_at": dt.datetime.now(dt.UTC).isoformat()}
    version = _versions(["pymupdf", "pdfplumber"])
    issues = ["layout_table_and_numeric_accuracy_requires_review", "images_not_interpreted"]
    blocks, native, artifacts = [], {"pages": []}, {}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="evidence-baseline-") as temporary:
        folder = Path(temporary)
        status = "completed"
        try:
            import pdfplumber
            import pymupdf

            with pymupdf.open(source["path"]) as pdf, pdfplumber.open(source["path"]) as tables_pdf:
                for number, page in enumerate(pdf, 1):
                    text_blocks = page.get_text("blocks", sort=True)
                    page_native = {"page": number, "width": page.cropbox.width, "height": page.cropbox.height,
                                   "text_blocks": text_blocks, "tables": []}
                    chars = 0
                    for row in text_blocks:
                        if row[6] != 0 or not row[4].strip():
                            continue
                        chars += len(row[4])
                        blocks.append({"id": f"b{len(blocks) + 1:06d}", "kind": "paragraph",
                                       "text": row[4].strip(),
                                       "locator": {"page": number, "bbox": list(row[:4]),
                                                   "origin": "top_left", "coordinate_units": "pdf_points"},
                                       "native_locator": {"page": number, "block_number": row[5]}})
                    if chars < 80:
                        issues.append(f"low_text_page_requires_ocr_or_review: {number}")
                    try:
                        for table in tables_pdf.pages[number - 1].find_tables():
                            cells = table.extract()
                            page_native["tables"].append({"bbox": list(table.bbox), "cells": cells})
                            table_locator = {"page": number, "bbox": list(table.bbox),
                                             "origin": "top_left", "coordinate_units": "pdf_points"}
                            table_issues = ["table_structure_requires_review"]
                            native_table_locator = copy.deepcopy(table_locator)
                            if page.cropbox.x0 or page.cropbox.y0:
                                table_locator.pop("bbox")
                                table_issues.append("native_bbox_cropbox_offset_unverified_page_only")
                            blocks.append({"id": f"b{len(blocks) + 1:06d}", "kind": "table",
                                           "text": "\n".join("\t".join(str(v or "") for v in r) for r in cells),
                                           "table_cells": cells,
                                           "locator": table_locator, "native_locator": native_table_locator,
                                           "issues": table_issues})
                    except Exception as exc:  # noqa: BLE001 - parser failures are retained as explicit issues.
                        issues.append(f"table_extraction_failed_page_{number}: {type(exc).__name__}: {exc}")
                    native["pages"].append(page_native)
            if not blocks:
                raise EvidenceError("No text/table blocks; OCR may be required")
            _dump(folder / "document.json", native)
            _dump(folder / "blocks.json", blocks)
            artifacts.update({name: folder / name for name in ("document.json", "blocks.json")})
        except Exception as exc:  # noqa: BLE001 - retain backend/import failures without inventing success.
            issues.append(f"parser_failed: {type(exc).__name__}: {exc}")
            blocks, status = [], "failed"
        settings["elapsed_seconds"] = round(time.monotonic() - started, 6)
        _dump(folder / "run.json", {"source_sha256": source["sha256"], "parser_version": version,
                                    "settings": settings, "status": status, "issues": issues})
        artifacts["run.json"] = folder / "run.json"
        return _store_run(store, source_id, "baseline", version, blocks, settings, artifacts, issues, status)


def _tei_text(node: ET.Element) -> str:
    text = node.text or ""
    for child in node:
        tag = child.tag.rsplit("}", 1)[-1]
        content = _tei_text(child)
        if tag == "ref":
            text += f" [ref:{content}] "
        elif tag in {"row", "cell", "head", "p", "table", "figDesc", "forename", "surname", "persName",
                     "affiliation", "orgName", "address", "date", "idno", "title", "label", "biblScope"}:
            text += "\n" + content + "\n"
        else:
            text += content
        text += child.tail or ""
    return text.strip()


def _tei_blocks(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    parents = {child: parent for parent in root.iter() for child in parent}
    tags = {"title", "head", "p", "figure", "formula", "biblStruct", "note"}
    blocks = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag not in tags:
            continue
        parent, nested = parents.get(element), False
        while parent is not None:
            if parent.tag.rsplit("}", 1)[-1] in tags:
                nested = True
                break
            parent = parents.get(parent)
        if nested or not (text := _tei_text(element)):
            continue
        regions, native_coords = [], []
        for child in element.iter():
            for entry in child.get("coords", "").split(";"):
                if not entry:
                    continue
                native_coords.append(entry)
                values = [float(value) for value in entry.split(",")]
                if len(values) != 5 or values[0] != int(values[0]):
                    raise EvidenceError("Invalid GROBID coordinates")
                page, x, y, width, height = values
                regions.append({"page": int(page), "bbox": [x, y, x + width, y + height]})
        pages = sorted({r["page"] for r in regions})
        block = {"id": f"b{len(blocks) + 1:06d}", "text": text,
                 "kind": "table" if tag == "figure" and element.get("type") == "table" else tag,
                 "locator": {},
                 "native_locator": {"coords": native_coords,
                                    "xml_id": element.get("{http://www.w3.org/XML/1998/namespace}id")}}
        if pages:
            block["locator"].update(page=pages[0], pages=pages, regions=regions,
                                    bbox=regions[0]["bbox"], origin="top_left",
                                    coordinate_units="pdf_points")
        else:
            block["locator"]["unlocated"] = True
            block["issues"] = ["unlocated"]
        blocks.append(block)
    return blocks


def run_grobid(store, source_id: str, *, endpoint: str) -> dict:
    """POST a PDF to an explicitly selected service; freeze TEI and settings.

    Coordinates are requested; metadata consolidation (external lookup) is off.
    This function does not discover/start a service or change server settings.
    """
    import requests

    source = store.source(source_id)
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise EvidenceError("Explicit HTTP(S) GROBID endpoint without credentials is required")
    endpoint = endpoint.rstrip("/")
    request_settings = [("consolidateHeader", "0"), ("consolidateCitations", "0"),
                        ("includeRawCitations", "1"), ("includeRawAffiliations", "1")]
    request_settings += [("teiCoordinates", tag) for tag in
                         ("p", "head", "figure", "formula", "ref", "biblStruct", "title", "note")]
    settings = {"execution": "fresh", "endpoint": endpoint, "request": request_settings,
                "adapter_version": ADAPTER_VERSION, "started_at": dt.datetime.now(dt.UTC).isoformat()}
    version, issues, blocks, artifacts, status = "unknown", [], [], {}, "completed"
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="evidence-grobid-") as temporary:
        folder = Path(temporary)
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.get(endpoint + "/api/version", timeout=(10, 30), allow_redirects=False)
                if 300 <= response.status_code < 400:
                    raise EvidenceError("GROBID endpoint redirected; explicit final endpoint required")
                response.raise_for_status()
                version = response.text.strip()
                with Path(source["path"]).open("rb") as stream:
                    response = session.post(endpoint + "/api/processFulltextDocument", data=request_settings,
                                            files={"input": ("source.pdf", stream, "application/pdf")},
                                            timeout=(10, 600), allow_redirects=False)
                settings["http_status"] = response.status_code
                if 300 <= response.status_code < 400:
                    raise EvidenceError("GROBID upload redirected; explicit final endpoint required")
                response.raise_for_status()
                (folder / "document.tei.xml").write_bytes(response.content)
                artifacts["document.tei.xml"] = folder / "document.tei.xml"
                blocks = _tei_blocks(response.content.decode("utf-8"))
                if not blocks:
                    raise EvidenceError("No content blocks in GROBID TEI")
                _dump(folder / "blocks.json", blocks)
                artifacts["blocks.json"] = folder / "blocks.json"
                issues.append("tei_text_and_coordinates_require_original_review")
        except (requests.RequestException, EvidenceError, OSError, ValueError, ET.ParseError, UnicodeError) as exc:
            blocks, status = [], "failed"
            issues.append(f"parser_failed: {type(exc).__name__}: {exc}")
        settings["elapsed_seconds"] = round(time.monotonic() - started, 6)
        _dump(folder / "run.json", {"source_sha256": source["sha256"], "parser_version": version,
                                    "settings": settings, "status": status, "issues": issues})
        artifacts["run.json"] = folder / "run.json"
        return _store_run(store, source_id, "grobid", version, blocks, settings, artifacts, issues, status)


def _store_run(store, source_id, parser, version, blocks, settings, artifacts, issues, status):
    try:
        if blocks and status != "failed":
            source = store.source(source_id)
            path = Path(source["path"])
            blocks = _normalize_blocks(blocks, _pdf_dimensions(path), parser, version, _pdf_rotations(path),
                                       _pdf_crop_offsets(path))
            if _geometry_fallback(blocks):
                status = "partial"
                issues = issues + ["native_geometry_requires_page_only_review"]
            if any(issue.startswith("table_extraction_failed_") for issue in issues):
                status = "partial"
        return store.add_conversion(source_id, parser=parser, parser_version=version, blocks=blocks,
                                    settings=settings, artifacts=artifacts, issues=issues, status=status)
    except EvidenceError as exc:
        # The source is reverified by add_conversion; source/artifact integrity
        # failures still raise if they also affect the failed-run snapshot.
        return store.add_conversion(source_id, parser=parser, parser_version=version, blocks=[],
                                    settings=settings, artifacts=artifacts,
                                    issues=issues + [f"conversion_validation_failed: {exc}"], status="failed")
