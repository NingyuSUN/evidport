"""Append-only, source-bound AI transcription corrections, never human approval."""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import re
import tempfile
from pathlib import Path, PurePosixPath

from .store import EvidenceError


def _need(condition, message):
    if not condition:
        raise EvidenceError(message)


def _text(value, label):
    _need(isinstance(value, str) and bool(value.strip()), f"{label} must be nonempty text")
    return value


def _pages(locator):
    result = set(locator.get("pages", []))
    if "page" in locator:
        result.add(locator["page"])
    result.update(region["page"] for region in locator.get("regions", []))
    return result


def _contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def apply_reconciliation(store, parent_conversion_id: str, manifest_path: Path) -> dict:
    """Validate a frozen manifest and create a partial derived conversion.

    All edits use offsets in the unchanged parent text. Render evidence must be
    PNG crops of the bound original PDF, checked independently by pixel content.
    The initial interface accepts unrotated pages only and rejects table edits;
    a located supplemental block may preserve a verified table omission without
    inventing a row/column assignment. Extra observation fields are retained.
    """
    manifest_path = Path(manifest_path).resolve()
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, ValueError) as exc:
        raise EvidenceError(f"Cannot read reconciliation manifest: {exc}") from exc
    _need(isinstance(manifest, dict) and type(manifest.get("schema")) is int
          and manifest["schema"] == 1, "Reconciliation schema must be 1")
    _need(manifest.get("parent_conversion_id") == parent_conversion_id, "Parent conversion binding differs")
    _need(manifest.get("reviewer_kind") == "ai", "Reconciliation is AI diagnostics, not human review")
    _text(manifest.get("reviewer"), "reviewer")
    _text(manifest.get("scope"), "scope")
    parent = store.conversion(parent_conversion_id)
    _need(parent["status"] != "failed", "Cannot reconcile a failed conversion")
    source = store.source(parent["source_id"])
    _need(manifest.get("source_sha256") == source["sha256"], "Source SHA256 binding differs")
    _need(source.get("media_type") == "application/pdf", "Reconciliation requires original PDF images")
    artifacts = manifest.get("artifacts")
    observations = manifest.get("observations")
    edits, additions = manifest.get("edits", []), manifest.get("additions", [])
    limitations = manifest.get("remaining_limitations", [])
    _need(isinstance(artifacts, dict) and artifacts, "Evidence artifacts are required")
    _need(isinstance(observations, list) and observations, "Observations are required")
    _need(isinstance(edits, list) and isinstance(additions, list), "Edits and additions must be lists")
    _need(isinstance(limitations, list) and all(isinstance(item, str) and item.strip() for item in limitations),
          "Remaining limitations must be a list of nonempty strings")

    # Read once and retain these bytes through the final immutable snapshot.
    artifact_bytes, render_locations = {}, {}
    try:
        import pymupdf
    except ImportError as exc:
        raise EvidenceError("Install PDF extras to verify original image evidence") from exc
    try:
        pdf = pymupdf.open(source["path"])
    except Exception as exc:
        raise EvidenceError(f"Cannot render original PDF: {exc}") from exc
    with pdf:
        for name, spec in artifacts.items():
            _need(isinstance(name, str) and bool(name) and "\\" not in name and ":" not in name
                  and not PurePosixPath(name).is_absolute()
                  and all(part not in ("", ".", "..") for part in name.split("/")), "Unsafe evidence artifact name")
            _need(isinstance(spec, dict), "Artifact specification must be an object")
            relative = spec.get("path")
            _need(isinstance(relative, str) and bool(relative) and not Path(relative).is_absolute(),
                  "Evidence artifact path must be relative to manifest")
            path = (manifest_path.parent / relative).resolve()
            _need(path.is_relative_to(manifest_path.parent), "Evidence artifact escapes manifest directory")
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise EvidenceError(f"Cannot read evidence artifact: {exc}") from exc
            _need(hashlib.sha256(data).hexdigest() == spec.get("sha256"), "Evidence artifact SHA256 differs")
            render = spec.get("render")
            _need(isinstance(render, dict), "Evidence artifact requires independent rendering parameters")
            page, bbox, scale = render.get("page"), render.get("bbox"), render.get("scale")
            location = store._locator({"page": page, "bbox": bbox}, source)
            _need(source.get("page_rotations", [0] * source["pages"])[page - 1] == 0,
                  "Rotated-page crop convention is not supported by reconciliation")
            _need(type(scale) in (int, float) and math.isfinite(scale) and 0 < scale <= 8,
                  "Render scale must be finite and in (0, 8]")
            try:
                expected = pdf[page - 1].get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                                 clip=pymupdf.Rect(*bbox), alpha=False)
                actual = pymupdf.Pixmap(data)
                identical = ((actual.width, actual.height, actual.n, actual.samples)
                             == (expected.width, expected.height, expected.n, expected.samples))
            except Exception as exc:
                raise EvidenceError(f"Cannot verify original crop: {exc}") from exc
            _need(identical, "Evidence crop does not match independent original rendering")
            artifact_bytes[name], render_locations[name] = data, location

    by_observation = {}
    for observation in observations:
        _need(isinstance(observation, dict), "Observation must be an object")
        identifier = observation.get("id")
        _need(isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", identifier)
              and identifier not in by_observation, "Invalid or duplicate observation ID")
        _need(observation.get("status") in ("corrected", "no_change", "unresolved"), "Unknown observation status")
        _text(observation.get("reason"), "observation reason")
        locator = store._locator(observation.get("source_locator"), source)
        _need("page" in locator and not locator.get("unlocated"), "Observation requires one physical source page")
        _need(_pages(locator) == {locator["page"]}, "Observation must refer to one physical source page")
        references = observation.get("evidence_artifacts")
        _need(isinstance(references, list) and references and all(isinstance(x, str) for x in references)
              and len(references) == len(set(references)), "Observation requires unique evidence artifact references")
        page = locator["page"]
        width, height = source["page_sizes"][page - 1]
        area = locator.get("bbox", [0, 0, width, height])
        for name in references:
            _need(name in render_locations, "Observation references unknown evidence artifact")
            crop = render_locations[name]
            _need(crop["page"] == page and _contains(crop["bbox"], area),
                  "Evidence crop must cover the observation source region")
        by_observation[identifier] = observation

    blocks = copy.deepcopy(parent["blocks"])
    by_block = {block["id"]: block for block in blocks}
    for observation in observations:
        affected = observation.get("affected_block_ids", [])
        _need(isinstance(affected, list) and all(isinstance(item, str) for item in affected)
              and len(affected) == len(set(affected)), "Affected blocks must be unique block IDs")
        for identifier in affected:
            _need(identifier in by_block, "Observation references unknown affected block")
            block = by_block[identifier]
            _need(observation["source_locator"]["page"] in _pages(block["locator"]),
                  "Affected block and observation refer to different source pages")
            block.setdefault("reconciliation_observation_ids", []).append(observation["id"])
            block.setdefault("issues", []).append(
                f"ai_reconciliation:{observation['id']}:{observation['status']}:{observation['reason']}")
            block["issues"].extend(f"reconciliation_limitation:{item}" for item in limitations)
    used_observations, grouped_edits = set(), {}

    def operation_observations(operation, locator):
        references = operation.get("observation_ids")
        _need(isinstance(references, list) and references and all(isinstance(x, str) for x in references)
              and len(references) == len(set(references)), "Correction needs unique observation references")
        for identifier in references:
            _need(identifier in by_observation, "Correction references unknown observation")
            observation = by_observation[identifier]
            _need(observation["status"] == "corrected", "Only corrected observations may change content")
            _need(observation["source_locator"]["page"] in _pages(locator),
                  "Correction block and observation refer to different source pages")
            used_observations.add(identifier)
        return references

    for edit in edits:
        _need(isinstance(edit, dict) and isinstance(edit.get("block_id"), str)
              and edit["block_id"] in by_block, "Edit references unknown block")
        block = by_block[edit["block_id"]]
        _need(block["kind"] != "table" and "table_cells" not in block,
              "Table text edits require structured-cell reconciliation; use a located supplement")
        start, end, old, new = (edit.get(key) for key in ("start", "end", "old", "new"))
        _need(type(start) is int and type(end) is int and 0 <= start <= end <= len(block["text"]),
              "Edit offsets are outside parent text")
        _need(isinstance(old, str) and isinstance(new, str) and old != new, "Edit requires distinct old/new text")
        _need(block["text"][start:end] == old, "Edit old text does not match parent offsets")
        operation_observations(edit, block["locator"])
        grouped_edits.setdefault(block["id"], []).append(edit)
    for identifier, changes in grouped_edits.items():
        ordered = sorted(changes, key=lambda edit: (edit["start"], edit["end"]))
        for left, right in itertools.pairwise(ordered):
            _need(left["end"] <= right["start"] and left["start"] != right["start"],
                  "Overlapping or duplicate edits are not allowed")
        block = by_block[identifier]
        for edit in reversed(ordered):
            block["text"] = block["text"][:edit["start"]] + edit["new"] + block["text"][edit["end"]:]
        block.setdefault("issues", []).append("ai_corrected_transcription_requires_human_review")
        block["reconciliation_observation_ids"] = sorted(set(block.get("reconciliation_observation_ids", []))
                                                        | {x for edit in ordered for x in edit["observation_ids"]})

    for addition in additions:
        _need(isinstance(addition, dict) and isinstance(addition.get("block"), dict), "Addition requires a block")
        block = copy.deepcopy(addition["block"])
        identifier = block.get("id")
        _need(isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", identifier)
              and identifier not in by_block, "Invalid or duplicate added block ID")
        _text(block.get("text"), "added text")
        _text(block.get("kind"), "added block kind")
        _need(block["kind"] != "table" and "table_cells" not in block,
              "Added table structure is outside transcription reconciliation scope")
        block["locator"] = store._locator(block.get("locator"), source)
        references = operation_observations(addition, block["locator"])
        regions = list(block["locator"].get("regions", []))
        if "bbox" in block["locator"]:
            regions.append({"page": block["locator"]["page"], "bbox": block["locator"]["bbox"]})
        _need(regions, "Added transcription requires an explicit source region, not page-only location")
        covered_pages = {region["page"] for region in regions}
        _need(covered_pages == _pages(block["locator"]), "Every added transcription page needs a source region")
        crops = [render_locations[name] for identifier in references
                 for name in by_observation[identifier]["evidence_artifacts"]]
        for region in regions:
            _need(any(crop["page"] == region["page"] and _contains(crop["bbox"], region["bbox"])
                      for crop in crops), "Added source region is outside its verified evidence crops")
        _need(isinstance(block.setdefault("issues", []), list), "Added block issues must be a list")
        block["issues"].append("ai_supplemental_transcription_requires_human_review")
        block["reconciliation_observation_ids"] = references
        blocks.append(block)
        by_block[identifier] = block
    _need(used_observations == {key for key, obs in by_observation.items() if obs["status"] == "corrected"},
          "Every corrected observation must correspond to an edit or addition")

    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    issues = copy.deepcopy(parent["issues"])
    issues.append("ai_visual_reconciliation_only_not_human_approval")
    issues.append("partial_transcription_scope_not_full_document_validation")
    issues.extend(f"reconciliation_limitation:{item}" for item in limitations)
    issues.extend(f"reconciliation_unresolved:{obs['id']}:{obs['reason']}" for obs in observations
                  if obs["status"] == "unresolved")
    settings = {"execution": "ai_visual_reconciliation", "parent_conversion_id": parent_conversion_id,
                "parent_parser": parent["parser"], "parent_parser_version": parent["parser_version"],
                "parent_settings": parent["settings"], "source_sha256": source["sha256"],
                "reviewer": manifest["reviewer"], "reviewer_kind": "ai", "scope": manifest["scope"],
                "manifest_sha256": manifest_sha, "observations": observations,
                "remaining_limitations": limitations,
                "image_verification": "independent_original_pdf_pixel_match",
                "scientifically_verified": False, "human_approved": False}
    with tempfile.TemporaryDirectory(prefix="evidence-reconciliation-") as temporary:
        folder, snapshots = Path(temporary), {}
        for index, (name, info) in enumerate(parent["artifacts"].items()):
            data = Path(info["path"]).read_bytes()
            _need(hashlib.sha256(data).hexdigest() == info["sha256"], "Parent artifact changed during reconciliation")
            path = folder / f"parent-{index}"
            path.write_bytes(data)
            snapshots[name] = path
        prefix = f"reconciliation_{manifest_sha}"
        for index, (name, data) in enumerate(artifact_bytes.items()):
            path = folder / f"evidence-{index}"
            path.write_bytes(data)
            snapshots[f"{prefix}/evidence/{name}"] = path
        path = folder / "manifest.json"
        path.write_bytes(manifest_bytes)
        snapshots[f"{prefix}/manifest.json"] = path
        # Revalidate the parent immediately before mutation. Neither history nor
        # review rows are written by this operation.
        store.conversion(parent_conversion_id)
        return store.add_conversion(parent["source_id"], parser="ai-visual-reconciliation", parser_version="1",
                                    blocks=blocks, settings=settings, artifacts=snapshots, issues=issues,
                                    status="partial")
