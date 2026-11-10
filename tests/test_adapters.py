"""Adapters must retain provenance and fail closed on corrupt historic output."""
import hashlib
import json

import pytest

from evidence_database.adapters import import_legacy, import_pilot, run_baseline, run_grobid
from evidence_database.store import EvidenceError, Store


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def pdf(tmp_path):
    import pymupdf

    path = tmp_path / "paper.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=200, height=300)
        page.insert_text((20, 40), "Synthetic evidence 42 mg.")
        document.save(path)
    return path


def pilot(tmp_path, pdf, parser="baseline", blocks=None, native=None):
    folder = tmp_path / "parser" / "synthetic"
    source_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    dump(manifest, {"documents": [{"id": "synthetic", "path": str(pdf), "sha256": source_hash,
                                  "pages": 1, "role": "synthetic_fixture"}]})
    blocks = blocks or [{"id": "b1", "kind": "paragraph", "text": "42 mg",
                         "locator": {"page": 1, "bbox": [20, 20, 80, 40]}}]
    blocks_hash = dump(folder / "blocks.json", blocks)
    result = {"status": "ok", "parser": parser, "parser_version": "fixture",
              "source_sha256": source_hash, "blocks_sha256": blocks_hash, "input_pages": 1}
    if native is not None:
        native_hash = dump(folder / "document.json", native)
        dump(folder / "receipt.json", {"sha256": source_hash, "versions": {"docling": "2.148.0"},
             "settings": {"do_ocr": False}, "output_sha256": {"document.json": native_hash,
                                                                "blocks.json": blocks_hash}})
    dump(folder / "result.json", result)
    return manifest, folder.parent


def test_source_mismatch_prevents_ingestion(tmp_path, pdf):
    manifest, root = pilot(tmp_path, pdf)
    pdf.write_bytes(pdf.read_bytes() + b"changed")
    store = Store(tmp_path / "db")
    with pytest.raises(EvidenceError, match="source.*SHA256|Source.*SHA256"):
        import_pilot(store, manifest, {"baseline": root})
    assert store.sources() == []


def test_changed_blocks_and_missing_parser_are_explicit_failures(tmp_path, pdf):
    manifest, root = pilot(tmp_path, pdf)
    (root / "synthetic" / "blocks.json").write_text("[]", encoding="utf-8")
    store = Store(tmp_path / "db")
    report = import_pilot(store, manifest, {"baseline": root, "grobid": tmp_path / "missing"})
    assert report["failed"] == 2
    assert report["completed"] == 0
    assert store.search("42") == []
    assert all(c["status"] == "failed" for c in store.conversions())


def test_docling_bottom_left_preserved_and_canonicalized(tmp_path, pdf):
    native_box = {"l": 20, "t": 280, "r": 80, "b": 260, "coord_origin": "BOTTOMLEFT"}
    block = {"id": "b1", "kind": "text", "text": "42 mg",
             "locator": {"page": 1, "bbox": native_box,
                         "regions": [{"page": 1, "bbox": native_box}]}}
    manifest, root = pilot(tmp_path, pdf, "docling", [block], {"texts": []})
    store = Store(tmp_path / "db")
    report = import_pilot(store, manifest, {"docling": root})
    assert report["completed"] == 1
    item = store.get(report["conversions"][0]["citations"][0])
    assert item["block"]["locator"]["bbox"] == [20, 20, 80, 40]
    assert item["block"]["native_locator"]["bbox"] == native_box
    assert item["review_status"] != "approved"


def test_corrupt_native_is_not_successfully_imported(tmp_path, pdf):
    manifest, root = pilot(tmp_path, pdf, "docling", native={"texts": []})
    (root / "synthetic" / "document.json").write_text('{"changed": true}', encoding="utf-8")
    store = Store(tmp_path / "db")
    report = import_pilot(store, manifest, {"docling": root})
    assert report["failed"] == 1
    assert store.search("42") == []


def test_legacy_page_ocr_issues_and_incomplete_historical_provenance(tmp_path, pdf):
    receipt = pdf.with_suffix(".extract.json")
    dump(receipt, {"sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "pages": 1,
                   "page_stats": [{"page": 1, "method": "ocr", "chars": 20}],
                   "ocr_number_disagreements": 1, "tools": {"pymupdf": "fixture"}})
    pdf.with_suffix(".text.md").write_text("# paper\n\n## Page 1 (ocr)\n\nDose 42 mg.\n", encoding="utf-8")
    pdf.with_suffix(".tables.tsv").write_text("page\ttable\trow\tcells\n1\t1\t1\tDose | 42\n", encoding="utf-8")
    pdf.with_suffix(".ocr_check.tsv").write_text("page\ttesseract\trapidocr\n1\t42\t47\n", encoding="utf-8")
    store = Store(tmp_path / "db")
    report = import_legacy(store, pdf, receipt, document_id="scan")
    conversion = store.conversion(report["conversion_id"])
    assert "historical_output_hashes_unavailable" in conversion["issues"]
    blocks = [store.get(c)["block"] for c in report["citations"]]
    assert any("ocr_disagreement_requires_review" in b.get("issues", []) for b in blocks)
    assert any(b["kind"] == "table" for b in blocks)
    assert all(b["locator"]["page"] == 1 and "bbox" not in b["locator"] for b in blocks)
    assert not store.check(report["citations"][0], "42 mg.")["admitted"]


def test_legacy_source_hash_is_required(tmp_path, pdf):
    receipt = tmp_path / "old.json"
    dump(receipt, {"sha256": "0" * 64})
    store = Store(tmp_path / "db")
    with pytest.raises(EvidenceError, match="source.*SHA256|Source.*SHA256"):
        import_legacy(store, pdf, receipt, document_id="bad")
    assert store.sources() == []


def test_fresh_baseline_keeps_positions_and_native_outputs(tmp_path, pdf):
    store = Store(tmp_path / "db")
    source = store.ingest(pdf, "fresh")
    report = run_baseline(store, source["source_id"])
    assert report["status"] == "completed"
    hit = store.search("Synthetic")[0]
    block = store.get(hit["citation"])["block"]
    assert block["locator"]["page"] == 1
    assert len(block["locator"]["bbox"]) == 4
    conversion = store.conversion(report["conversion_id"])
    assert conversion["settings"]["execution"] == "fresh"
    assert conversion["artifacts"]


def test_grobid_http_failure_is_explicit_conversion(tmp_path, pdf, monkeypatch):
    import requests

    def unavailable(*args, **kwargs):
        raise requests.ConnectionError("fixture endpoint unavailable")

    monkeypatch.setattr(requests.Session, "get", unavailable)
    store = Store(tmp_path / "db")
    source = store.ingest(pdf, "fresh")
    report = run_grobid(store, source["source_id"], endpoint="http://127.0.0.1:9999")
    assert report["status"] == "failed"
    assert store.search("Synthetic") == []
    assert "fixture endpoint unavailable" in str(store.conversion(report["conversion_id"])["issues"])


def test_grobid_coordinates_and_unlocated_nodes_are_separate(tmp_path, pdf, monkeypatch):
    import requests

    class Response:
        status_code = 200

        def __init__(self, content):
            self.content = content
            self.text = content.decode()

        def raise_for_status(self):
            pass

    observed = {}

    def post(_session, _url, **kwargs):
        observed.update(kwargs)
        return Response(b'<TEI><text><body><p coords="1,20,20,60,20">Dose 42 mg.</p>'
                        b'<note>Unlocated note.</note></body></text></TEI>')

    monkeypatch.setattr(requests.Session, "get", lambda *a, **k: Response(b"fixture-version"))
    monkeypatch.setattr(requests.Session, "post", post)
    store = Store(tmp_path / "db")
    source = store.ingest(pdf, "fresh")
    report = run_grobid(store, source["source_id"], endpoint="http://127.0.0.1:9999")
    assert report["status"] == "completed"
    blocks = [store.get(c)["block"] for c in report["citations"]]
    assert blocks[0]["locator"]["bbox"] == [20, 20, 80, 40]
    assert blocks[1]["locator"]["unlocated"] is True
    assert ("consolidateCitations", "0") in observed["data"]
    assert ("teiCoordinates", "p") in observed["data"]


def test_stale_legacy_ocr_file_is_not_a_current_disagreement(tmp_path, pdf):
    receipt = pdf.with_suffix(".extract.json")
    dump(receipt, {"sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "n_pages": 1,
                   "pages": [{"page": 1, "method": "text"}], "ocr_number_disagreements": 0})
    pdf.with_suffix(".text.md").write_text("## page 1 (text)\n\nDose 42 mg.\n", encoding="utf-8")
    pdf.with_suffix(".ocr_check.tsv").write_text("page\ttesseract_only\trapidocr_only\n1\t42\t47\n",
                                                 encoding="utf-8")
    store = Store(tmp_path / "db")
    report = import_legacy(store, pdf, receipt, document_id="legacy")
    assert report["status"] == "completed"
    conversion = store.conversion(report["conversion_id"])
    assert "stale_ocr_check_file_receipt_reports_zero" in conversion["issues"]
    block = store.get(report["citations"][0])["block"]
    assert "ocr_disagreement_requires_review" not in block["issues"]


def test_receipt_cannot_reference_output_outside_parser_folder(tmp_path, pdf):
    manifest, root = pilot(tmp_path, pdf, "docling", native={"texts": []})
    receipt_path = root / "synthetic" / "receipt.json"
    record = json.loads(receipt_path.read_text())
    record["output_sha256"]["../../paper.pdf"] = hashlib.sha256(pdf.read_bytes()).hexdigest()
    dump(receipt_path, record)
    store = Store(tmp_path / "db")
    report = import_pilot(store, manifest, {"docling": root})
    assert report["failed"] == 1
    assert "Unsafe artifact path" in str(store.conversion(report["conversions"][0]["conversion_id"])["issues"])


def test_mineru_supported_normalized_coordinates_and_unknown_version(tmp_path, pdf):
    from evidence_database.adapters import _normalize_blocks

    native = {"id": "m:p1:b1", "kind": "text", "text": "42 mg",
              "locator": {"page": 1, "bbox": [0.1, 0.2, 0.4, 0.3]}}
    known = _normalize_blocks([native], {1: (200, 300)}, "mineru", "4.1.7")[0]
    assert known["locator"]["bbox"] == [20, 60, 80, 90]
    unknown = _normalize_blocks([native], {1: (200, 300)}, "mineru", "future")[0]
    assert "bbox" not in unknown["locator"]
    assert unknown["native_locator"]["bbox"] == native["locator"]["bbox"]
    assert "native_coordinate_convention_unverified" in unknown["issues"]
    store = Store(tmp_path / "db")
    source = store.ingest(pdf, "unknown-coordinate-convention")
    conversion = store.add_conversion(source["source_id"], parser="mineru", parser_version="future",
                                      blocks=[unknown])
    assert store.get(conversion["citations"][0])["block"]["locator"]["page"] == 1


def test_page_only_and_unlocated_pilot_blocks_remain_distinct(tmp_path, pdf):
    blocks = [{"id": "page", "kind": "text", "text": "Page text", "locator": {"page": 1}},
              {"id": "unknown", "kind": "note", "text": "Unlocated note", "locator": {}}]
    manifest, root = pilot(tmp_path, pdf, blocks=blocks)
    store = Store(tmp_path / "db")
    report = import_pilot(store, manifest, {"baseline": root})
    assert report["completed"] == 1
    first, second = [store.get(c)["block"] for c in report["conversions"][0]["citations"]]
    assert first["locator"]["page"] == 1 and "regions" not in first["locator"]
    assert second["locator"]["unlocated"] is True


def test_fresh_external_run_retains_verified_runner_and_rejects_changed_runner(tmp_path, pdf):
    manifest, root = pilot(tmp_path, pdf, "docling", native={"texts": []})
    script = tmp_path / "runner.py"
    script.write_text("# Synthetic runner fixture\n", encoding="utf-8")
    native_receipt = json.loads((root / "synthetic" / "receipt.json").read_text())
    run_receipt = tmp_path / "run.json"
    run_receipt_hash = dump(run_receipt, native_receipt)
    provenance = {"script_path": str(script), "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
                  "command": ["python", "runner.py", "--synthetic"],
                  "run_receipt_path": str(run_receipt), "run_receipt_sha256": run_receipt_hash,
                  "source_sha256": native_receipt["sha256"], "output_sha256": native_receipt["output_sha256"]}
    store = Store(tmp_path / "db")
    report = import_pilot(store, manifest, {"docling": root}, run_provenance=provenance)
    conversion = store.conversion(report["conversions"][0]["conversion_id"])
    assert conversion["settings"]["execution"] == "fresh_external_import"
    assert "runner.py" in conversion["artifacts"]
    script.write_text("# Changed runner\n", encoding="utf-8")
    report = import_pilot(store, manifest, {"docling": root}, run_provenance=provenance)
    assert report["failed"] == 1


def test_invalid_native_bbox_retains_text_and_page_as_partial(tmp_path, pdf):
    invalid_box = [20, 201740, 80, 2219141]
    blocks = [{"id": "bad", "kind": "text", "text": "42 mg",
               "locator": {"page": 1, "bbox": invalid_box}},
              {"id": "good", "kind": "text", "text": "Normal text",
               "locator": {"page": 1, "bbox": [20, 20, 80, 40]}}]
    manifest, root = pilot(tmp_path, pdf, blocks=blocks)
    store = Store(tmp_path / "db")
    report = import_pilot(store, manifest, {"baseline": root})
    assert (report["completed"], report["partial"], report["failed"]) == (0, 1, 0)
    bad, good = [store.get(c)["block"] for c in report["conversions"][0]["citations"]]
    assert bad["native_locator"]["bbox"] == invalid_box
    assert bad["locator"]["page"] == 1 and "bbox" not in bad["locator"]
    assert "native_bbox_out_of_bounds_page_only" in bad["issues"]
    assert good["locator"]["bbox"] == [20, 20, 80, 40]


def test_rotated_page_demotes_even_in_bounds_native_boxes(tmp_path, pdf):
    import pymupdf

    with pymupdf.open(pdf) as document:
        document[0].set_rotation(90)
        document.saveIncr()
    manifest, root = pilot(tmp_path, pdf)
    store = Store(tmp_path / "db")
    report = import_pilot(store, manifest, {"baseline": root})
    assert report["partial"] == 1
    block = store.get(report["conversions"][0]["citations"][0])["block"]
    assert "bbox" not in block["locator"]
    assert block["native_locator"]["bbox"] == [20, 20, 80, 40]
    assert "native_bbox_rotation_unverified_page_only" in block["issues"]


def test_legacy_empty_page_markers_fail_without_evidence(tmp_path, pdf):
    receipt = pdf.with_suffix(".extract.json")
    dump(receipt, {"sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "n_pages": 1})
    pdf.with_suffix(".text.md").write_text("## page 1 (text)\n\n", encoding="utf-8")
    store = Store(tmp_path / "db")
    conversion = import_legacy(store, pdf, receipt, document_id="empty")
    assert conversion["status"] == "failed"
    assert "no extracted evidence" in str(store.conversion(conversion["conversion_id"])["issues"])


def test_legacy_freezes_sidecars_before_parsing_and_storage(tmp_path, pdf, monkeypatch):
    receipt = pdf.with_suffix(".extract.json")
    dump(receipt, {"sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "n_pages": 1})
    markdown = pdf.with_suffix(".text.md")
    original = "## page 1 (text)\n\nDose 42 mg.\n"
    markdown.write_text(original, encoding="utf-8")
    original_hash = hashlib.sha256(markdown.read_bytes()).hexdigest()
    store = Store(tmp_path / "db")
    original_add = store.add_conversion

    def changed_original(*args, **kwargs):
        markdown.write_text("## page 1 (text)\n\nChanged 99 mg.\n", encoding="utf-8")
        return original_add(*args, **kwargs)

    monkeypatch.setattr(store, "add_conversion", changed_original)
    conversion = import_legacy(store, pdf, receipt, document_id="frozen")
    assert conversion["status"] == "completed"
    assert "42 mg" in store.get(conversion["citations"][0])["block"]["text"]
    snapshot = store.conversion(conversion["conversion_id"])["artifacts"][markdown.name]
    assert snapshot["sha256"] == original_hash


def test_legacy_unsubstantiated_fresh_claim_fails(tmp_path, pdf):
    receipt = pdf.with_suffix(".extract.json")
    dump(receipt, {"sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(), "n_pages": 1})
    pdf.with_suffix(".text.md").write_text("## page 1 (text)\n\nDose 42 mg.\n", encoding="utf-8")
    store = Store(tmp_path / "db")
    conversion = import_legacy(store, pdf, receipt, document_id="claim", run_provenance={"note": "hello"})
    assert conversion["status"] == "failed"
    assert store.conversion(conversion["conversion_id"])["settings"]["execution"] != "fresh"


def test_cropped_baseline_table_has_page_only_locator(tmp_path):
    import pymupdf

    pdf = tmp_path / "cropped.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=300, height=400)
        for y in (60, 90, 120, 150):
            page.draw_line((30, y), (170, y))
        for x in (30, 100, 170):
            page.draw_line((x, 60), (x, 150))
        page.insert_text((40, 80), "A")
        page.insert_text((110, 80), "42")
        page.set_cropbox(pymupdf.Rect(20, 30, 280, 370))
        document.save(pdf)
    store = Store(tmp_path / "db")
    source = store.ingest(pdf, "cropped")
    conversion = run_baseline(store, source["source_id"])
    assert conversion["status"] == "partial"
    blocks = [store.get(c)["block"] for c in conversion["citations"]]
    table = next(b for b in blocks if b["kind"] == "table")
    assert "bbox" not in table["locator"]
    assert table["native_locator"]["parser_reference"]["bbox"] == [30, 60, 170, 150]
    assert "native_bbox_cropbox_offset_unverified_page_only" in table["issues"]


def test_baseline_table_exception_preserves_text_as_partial(tmp_path, pdf, monkeypatch):
    import pdfplumber.page

    def broken_tables(*args, **kwargs):
        raise ValueError("synthetic table parser failure")

    monkeypatch.setattr(pdfplumber.page.Page, "find_tables", broken_tables)
    store = Store(tmp_path / "db")
    source = store.ingest(pdf, "partial-tables")
    conversion = run_baseline(store, source["source_id"])
    assert conversion["status"] == "partial"
    assert store.search("Synthetic")
    assert "synthetic table parser failure" in str(store.conversion(conversion["conversion_id"])["issues"])
