"""Synthetic transcription fixtures only; these tests do not approve real papers."""

import copy
import hashlib
import json
from pathlib import Path

import pymupdf
import pytest

from evidence_database.packets import create_packet
from evidence_database.reconciliation import apply_reconciliation
from evidence_database.store import EvidenceError, Store


@pytest.fixture
def reconciliation(tmp_path):
    original = tmp_path / "synthetic.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=300, height=200)
        page.insert_text((20, 40), "Synthetic dose 1.25 mg. Figure label 42.")
        pdf.new_page(width=300, height=200).insert_text((20, 40), "Synthetic second page.")
        pdf.save(original)
        crop = pdf[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=pymupdf.Rect(0, 0, 300, 200), alpha=False)
        crop.save(tmp_path / "crop.png")
    store = Store(tmp_path / "store")
    source = store.ingest(original, "explicit-synthetic-reconciliation")
    native = tmp_path / "native.txt"
    native.write_text("Synthetic dose 125 mg.", encoding="utf8")
    parent = store.add_conversion(source["source_id"], parser="synthetic-ocr", parser_version="1",
                                  blocks=[{"id": "page-1", "kind": "paragraph", "text": "Synthetic dose 125 mg.",
                                           "locator": {"page": 1}}],
                                  artifacts={"native.txt": native}, issues=["synthetic_ocr_warning"])
    manifest = {"schema": 1, "parent_conversion_id": parent["conversion_id"], "source_sha256": source["sha256"],
                "reviewer_kind": "ai", "reviewer": "synthetic AI fixture", "scope": "Only one synthetic number",
                "artifacts": {"crop.png": {"path": "crop.png", "sha256": hashlib.sha256((tmp_path / "crop.png").read_bytes()).hexdigest(),
                                           "render": {"page": 1, "bbox": [0, 0, 300, 200], "scale": 2}}},
                "observations": [{"id": "OCR-1", "status": "corrected", "source_locator": {"page": 1, "bbox": [0, 0, 300, 200]},
                                  "evidence_artifacts": ["crop.png"], "reason": "Synthetic original says 1.25",
                                  "confirmed_original": "1.25", "classification": "decimal_loss"}],
                "edits": [{"block_id": "page-1", "start": 15, "end": 18, "old": "125", "new": "1.25", "observation_ids": ["OCR-1"]}],
                "additions": []}
    path = tmp_path / "manifest.json"
    return store, source, parent, manifest, path


def apply(fixture, manifest=None):
    store, _, parent, original, path = fixture
    path.write_text(json.dumps(original if manifest is None else manifest), encoding="utf8")
    return apply_reconciliation(store, parent["conversion_id"], path)


def test_revision_preserves_parent_and_artifacts_without_approval(reconciliation):
    store, source, parent, manifest, _ = reconciliation
    before = json.dumps(store.conversion(parent["conversion_id"]), sort_keys=True)
    revised = apply(reconciliation)
    assert revised["conversion_id"] != parent["conversion_id"]
    assert revised["source_id"] == source["source_id"]
    assert revised["status"] == "partial"
    assert revised["blocks"][0]["text"] == "Synthetic dose 1.25 mg."
    assert revised["settings"]["observations"] == manifest["observations"]
    assert revised["settings"]["parent_conversion_id"] == parent["conversion_id"]
    assert revised["settings"]["image_verification"] == "independent_original_pdf_pixel_match"
    assert revised["artifacts"]["native.txt"]["sha256"] == parent["artifacts"]["native.txt"]["sha256"]
    assert "synthetic_ocr_warning" in revised["issues"]
    assert json.dumps(store.conversion(parent["conversion_id"]), sort_keys=True) == before
    check = store.check(revised["citations"][0], "dose 1.25 mg")
    assert check["quote_matches"] and not check["admitted"] and not check["scientifically_verified"]
    assert store.reviews(revised["citations"][0]) == []
    assert store.audit()["ok"]
    assert apply(reconciliation)["conversion_id"] == revised["conversion_id"]


@pytest.mark.parametrize("change,match", [
    (lambda m: m.update(parent_conversion_id="cv_" + "0" * 64), "Parent conversion"),
    (lambda m: m.update(source_sha256="0" * 64), "Source SHA256"),
    (lambda m: m.update(reviewer_kind="human"), "AI diagnostics"),
    (lambda m: m["observations"].append(copy.deepcopy(m["observations"][0])), "duplicate observation"),
    (lambda m: m["observations"][0].update(status="unresolved"), "Only corrected"),
    (lambda m: m["edits"][0].update(start=-1), "outside parent"),
    (lambda m: m["edits"][0].update(end=999), "outside parent"),
    (lambda m: m["edits"][0].update(old="999"), "old text"),
    (lambda m: m["edits"].append(copy.deepcopy(m["edits"][0])), "Overlapping"),
    (lambda m: m["edits"][0].update(observation_ids=["missing"]), "unknown observation"),
    (lambda m: m.update(edits=[]), "Every corrected"),
    (lambda m: m["observations"][0]["source_locator"].update(page=3), "physical 1-based"),
    (lambda m: m["observations"][0]["source_locator"].update(page=2), "cover the observation"),
    (lambda m: m["artifacts"]["crop.png"].update(sha256="0" * 64), "artifact SHA256"),
    (lambda m: m["artifacts"]["crop.png"].update(path="../crop.png"), "escapes manifest"),
])
def test_invalid_manifest_fails_before_new_version(reconciliation, change, match):
    store, _, _, manifest, _ = reconciliation
    changed = copy.deepcopy(manifest)
    change(changed)
    with pytest.raises(EvidenceError, match=match):
        apply(reconciliation, changed)
    assert len(store.conversions()) == 1
    assert store.audit()["ok"]


def test_overlapping_different_offsets_rejected(reconciliation):
    manifest = copy.deepcopy(reconciliation[3])
    manifest["edits"].append({"block_id": "page-1", "start": 16, "end": 18,
                              "old": "25", "new": "25.0", "observation_ids": ["OCR-1"]})
    with pytest.raises(EvidenceError, match="Overlapping"):
        apply(reconciliation, manifest)


def test_image_with_valid_hash_but_wrong_pixels_is_rejected(reconciliation):
    manifest, path = reconciliation[3:]
    with pymupdf.open() as fake:
        page = fake.new_page(width=300, height=200)
        page.insert_text((20, 40), "This is not the original source.")
        page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).save(path.parent / "crop.png")
    manifest["artifacts"]["crop.png"]["sha256"] = hashlib.sha256((path.parent / "crop.png").read_bytes()).hexdigest()
    with pytest.raises(EvidenceError, match="independent original rendering"):
        apply(reconciliation)


def test_no_change_and_unresolved_are_preserved_without_changing_text(reconciliation):
    manifest = copy.deepcopy(reconciliation[3])
    manifest["edits"] = []
    manifest["observations"][0]["status"] = "no_change"
    unresolved = copy.deepcopy(manifest["observations"][0])
    unresolved.update(id="OCR-2", status="unresolved", reason="Synthetic insufficient contrast")
    manifest["observations"].append(unresolved)
    revised = apply(reconciliation, manifest)
    assert revised["blocks"][0]["text"] == reconciliation[2]["blocks"][0]["text"]
    assert any("reconciliation_unresolved:OCR-2:" in issue for issue in revised["issues"])
    assert revised["status"] == "partial"


def test_located_figure_supplement_keeps_parent_and_distinct_origin(reconciliation):
    manifest = copy.deepcopy(reconciliation[3])
    manifest["edits"] = []
    manifest["additions"] = [{"block": {"id": "figure-label", "kind": "figure_transcription", "text": "42",
                                        "locator": {"page": 1, "bbox": [0, 0, 300, 200]}},
                              "observation_ids": ["OCR-1"]}]
    revised = apply(reconciliation, manifest)
    assert revised["blocks"][0]["text"] == reconciliation[2]["blocks"][0]["text"]
    assert revised["blocks"][1]["text"] == "42"
    assert "ai_supplemental_transcription_requires_human_review" in revised["blocks"][1]["issues"]


def test_supplement_requires_same_source_page(reconciliation):
    manifest = copy.deepcopy(reconciliation[3])
    manifest["edits"] = []
    manifest["additions"] = [{"block": {"id": "wrong-page", "kind": "paragraph", "text": "42", "locator": {"page": 2}},
                              "observation_ids": ["OCR-1"]}]
    with pytest.raises(EvidenceError, match="different source pages"):
        apply(reconciliation, manifest)


def test_page_only_supplement_is_rejected(reconciliation):
    manifest = copy.deepcopy(reconciliation[3])
    manifest["edits"] = []
    manifest["additions"] = [{"block": {"id": "page-only", "kind": "paragraph", "text": "42", "locator": {"page": 1}},
                              "observation_ids": ["OCR-1"]}]
    with pytest.raises(EvidenceError, match="explicit source region"):
        apply(reconciliation, manifest)


def test_supplement_region_must_fit_its_verified_crop(reconciliation):
    _, source, _, manifest, path = reconciliation
    with pymupdf.open(source["path"]) as pdf:
        pdf[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=pymupdf.Rect(0, 0, 150, 100), alpha=False).save(path.parent / "crop.png")
    crop_spec = manifest["artifacts"]["crop.png"]
    crop_spec["sha256"] = hashlib.sha256((path.parent / "crop.png").read_bytes()).hexdigest()
    crop_spec["render"]["bbox"] = [0, 0, 150, 100]
    manifest["observations"][0]["source_locator"]["bbox"] = [0, 0, 150, 100]
    manifest["edits"] = []
    manifest["additions"] = [{"block": {"id": "outside-crop", "kind": "paragraph", "text": "42",
                                        "locator": {"page": 1, "bbox": [100, 100, 200, 150]}},
                              "observation_ids": ["OCR-1"]}]
    with pytest.raises(EvidenceError, match="outside its verified evidence crops"):
        apply(reconciliation, manifest)


def test_table_text_edit_is_explicitly_rejected(reconciliation):
    store, source, _, manifest, _ = reconciliation
    table = store.add_conversion(source["source_id"], parser="synthetic-table", parser_version="1",
                                 blocks=[{"id": "page-1", "kind": "table", "text": "Synthetic dose 125 mg.",
                                          "locator": {"page": 1}, "table_cells": [["125"]]}])
    manifest["parent_conversion_id"] = table["conversion_id"]
    fixture = store, source, table, manifest, reconciliation[4]
    with pytest.raises(EvidenceError, match="structured-cell reconciliation"):
        apply(fixture)


def test_old_synthetic_human_approval_never_transfers(reconciliation, tmp_path):
    store, _, parent, _, _ = reconciliation
    citation = parent["citations"][0]
    packet = create_packet(store, citation, tmp_path / "synthetic-packet")
    store.review(citation, "Synthetic", decision="approve", reviewer="SYNTHETIC FIXTURE",
                 reviewer_kind="human", note="Automated synthetic lifecycle test only",
                 viewed_original=True, packet_id=packet["packet_id"])
    assert store.check(citation, "Synthetic")["admitted"]
    revised = apply(reconciliation)
    assert not store.check(revised["citations"][0], "Synthetic")["admitted"]
    assert store.reviews(revised["citations"][0]) == []
    assert len(store.reviews(citation)) == 1


def test_cli_persists_partial_revision_with_exit_two(reconciliation, capsys):
    from evidence_database.cli import main

    store, _, parent, manifest, path = reconciliation
    path.write_text(json.dumps(manifest), encoding="utf8")
    assert main(["--db", str(store.root), "reconcile", "--parent", parent["conversion_id"],
                 "--manifest", str(path)]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "partial"
    assert store.conversion(result["conversion_id"])["status"] == "partial"
    assert store.get(result["citations"][0])["review_status"] == "pending"


def test_parent_tampering_fails_closed(reconciliation):
    store, _, parent, _, _ = reconciliation
    Path(parent["artifacts"]["native.txt"]["path"]).write_text("Tampered", encoding="utf8")
    with pytest.raises(EvidenceError, match="Artifact integrity"):
        apply(reconciliation)
    assert len(list((store.root / "records" / "conversions").glob("*.json"))) == 1


def test_affected_blocks_expose_remaining_uncertainty_in_search(reconciliation):
    store, _, _, manifest, _ = reconciliation
    manifest["observations"][0]["affected_block_ids"] = ["page-1"]
    manifest["remaining_limitations"] = ["Synthetic other quantities were not checked"]
    revised = apply(reconciliation)
    hit = next(hit for hit in store.search("dose") if hit["conversion_id"] == revised["conversion_id"])
    assert any("other quantities were not checked" in item for item in hit["conversion_issues"])
    assert revised["blocks"][0]["reconciliation_observation_ids"] == ["OCR-1"]
    assert any("ai_reconciliation:OCR-1:corrected" in item for item in revised["blocks"][0]["issues"])


def test_wrong_affected_block_rejected(reconciliation):
    reconciliation[3]["observations"][0]["affected_block_ids"] = ["missing"]
    with pytest.raises(EvidenceError, match="unknown affected block"):
        apply(reconciliation)
