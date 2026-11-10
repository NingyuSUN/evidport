import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from evidence_database.store import EvidenceError, Store


@pytest.mark.parametrize("mutation", ["delete_fts", "rewrite_fts", "delete_both"])
def test_search_detects_omitted_evidence_before_returning_no_hits(evidence, mutation):
    store, _, conversion, _, _ = evidence
    assert store.search("efficacy")[0]["citation"] == conversion["citations"][0]
    with closing(sqlite3.connect(store.db_path)) as db, db:
        if mutation == "rewrite_fts":
            db.execute("UPDATE blocks_fts SET text='unrelated content'")
        else:
            db.execute("DELETE FROM blocks_fts")
            if mutation == "delete_both":
                db.execute("DELETE FROM blocks")
    with pytest.raises(EvidenceError):
        store.search("efficacy")


@pytest.mark.parametrize(("text", "quote", "expected"), [
    ("Value 5.25 mg", "Value 5.", False),
    ("Count 5,000 patients", "5,", False),
    ("Value 5e-3", "5e-", False),
    ("Value 10^-5", "10^", False),
    ("Value 5. Next sentence", "Value 5.", True),
    ("Count 5, followed by a note", "Count 5,", True),
])
def test_numeric_punctuation_cannot_truncate_a_value(evidence, text, quote, expected):
    store, source, _, _, _ = evidence
    conversion = store.add_conversion(source["source_id"], parser="synthetic", parser_version="1",
        blocks=[{"id": "b1", "kind": "text", "text": text, "locator": {"page": 1}}])
    assert store.check(conversion["citations"][0], quote)["quote_matches"] is expected


@pytest.fixture
def evidence(tmp_path):
    import pymupdf as fitz

    pdf = tmp_path / "paper.pdf"
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    page.insert_text((30, 30), "Synthetic value 15.5 and efficacy 0.05.")
    doc.save(pdf)
    doc.close()
    native = tmp_path / "native.txt"
    native.write_text("Synthetic value 15.5 and efficacy 0.05.", encoding="utf-8")
    store = Store(tmp_path / "db")
    source = store.ingest(pdf, "synthetic")
    conversion = store.add_conversion(
        source["source_id"], parser="fixture", parser_version="1",
        blocks=[{"id": "b1", "kind": "paragraph", "text": native.read_text(),
                 "locator": {"page": 1, "bbox": [0, 0, 300, 100]}}],
        artifacts={"native/text.txt": native},
    )
    return store, source, conversion, pdf, native


def packet(store, citation, tmp_path):
    import pymupdf as fitz

    item = store.get(citation)
    image = tmp_path / "page.png"
    with fitz.open(item["source"]["path"]) as doc:
        image.write_bytes(doc[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False, annots=True).tobytes("png"))
    return store.register_packet(citation, artifacts={"page.png": image}, metadata={
        "citation": citation, "source_id": item["source"]["source_id"],
        "source_sha256": item["source"]["sha256"], "locator": item["block"]["locator"],
        "renderer": "pymupdf", "renderer_version": fitz.VersionBind, "scale": 2,
        "rendered_pages": [{"page": 1, "artifact": "page.png"}],
    })


def approve(store, citation, packet_id, quote="Synthetic value"):
    return store.review(citation, quote, decision="approve", reviewer="Synthetic tester",
                        reviewer_kind="human", note="Synthetic test only", viewed_original=True,
                        packet_id=packet_id)


def test_versions_snapshots_and_idempotence(evidence):
    store, source, conversion, pdf, native = evidence
    assert store.ingest(pdf, "synthetic")["source_id"] == source["source_id"]
    native.write_text("CHANGED", encoding="utf-8")
    pdf.write_bytes(b"changed external input")
    assert store.get(conversion["citations"][0])["block"]["text"].startswith("Synthetic")
    revised = store.ingest(pdf, "synthetic")
    assert revised["source_id"] != source["source_id"]
    assert len(store.sources()) == 2
    assert store.audit()["ok"]


def test_discovery_does_not_admit_and_numeric_boundaries(evidence):
    store, _, conversion, _, _ = evidence
    citation = conversion["citations"][0]
    hit = store.search("efficacy")[0]
    assert hit["citation"] == citation
    assert hit["review_status"] == "pending"
    assert store.check(citation, "15.5")["quote_matches"]
    assert not store.check(citation, "5.5")["quote_matches"]
    assert not store.check(citation, "15")["quote_matches"]
    assert not store.check(citation, "0.0")["quote_matches"]
    assert not store.check(citation, "Synthetic value")["admitted"]
    assert store.search('efficacy OR "DROP"') == []


def test_quote_specific_human_review_revoke_and_ai_rejected(evidence, tmp_path):
    store, _, conversion, _, _ = evidence
    citation = conversion["citations"][0]
    packet_id = packet(store, citation, tmp_path)["packet_id"]
    with pytest.raises(EvidenceError):
        store.review(citation, "Synthetic value", decision="approve", reviewer="bot",
                     reviewer_kind="ai", note="not human", viewed_original=True, packet_id=packet_id)
    with pytest.raises(EvidenceError):
        approve(store, citation, packet_id, "fabricated")
    event = approve(store, citation, packet_id)
    assert event["reviewer_identity_assurance"] == "self_declared_local"
    result = store.check(citation, "Synthetic value")
    assert result["admitted"] and not result["scientifically_verified"]
    assert not store.check(citation, "efficacy 0.05")["admitted"]
    assert not store.check(citation, "Synthetic value", require_review=False)["admitted"]
    store.review(citation, "Synthetic value", decision="revoke", reviewer="Synthetic tester",
                 reviewer_kind="human", note="withdrawn")
    assert not store.check(citation, "Synthetic value")["admitted"]
    assert [x["decision"] for x in store.reviews(citation)] == ["approve", "revoke"]


@pytest.mark.parametrize("target", ["source", "native", "conversion", "packet", "index", "fts"])
def test_tampering_fails_closed_after_approval(evidence, tmp_path, target):
    store, source, conversion, _, _ = evidence
    citation = conversion["citations"][0]
    packet_record = packet(store, citation, tmp_path)
    approve(store, citation, packet_record["packet_id"])
    if target == "source":
        Path(source["path"]).write_bytes(b"tampered")
    elif target == "native":
        record = store.conversion(conversion["conversion_id"])
        Path(record["artifacts"]["native/text.txt"]["path"]).write_bytes(b"tampered")
    elif target == "conversion":
        path = store.root / "records" / "conversions" / (conversion["conversion_id"] + ".json")
        value = json.loads(path.read_text())
        value["blocks"][0]["text"] = "tampered"
        path.write_text(json.dumps(value))
    elif target == "packet":
        Path(packet_record["artifacts"]["page.png"]["path"]).write_bytes(b"tampered")
    else:
        with closing(sqlite3.connect(store.db_path)) as db, db:
            if target == "index":
                db.execute("UPDATE blocks SET text='tampered' WHERE citation=?", (citation,))
            else:
                db.execute("UPDATE blocks_fts SET text='tampered' WHERE citation=?", (citation,))
    with pytest.raises(EvidenceError):
        store.check(citation, "Synthetic value")
    assert not store.audit()["ok"]


@pytest.mark.parametrize("locator", [
    {"page": 0}, {"page": 2}, {"page": True}, {"page": 1, "bbox": [0, 0, 301, 10]},
    {"page": 1, "bbox": [10, 0, 0, 10]}, {"page": 1, "bbox": [0, 0, float("nan"), 1]},
    {"pages": [1, 2]}, {"bbox": [0, 0, 10, 10]},
])
def test_invalid_locators_rejected(evidence, locator):
    store, source, _, _, _ = evidence
    with pytest.raises(EvidenceError):
        store.add_conversion(source["source_id"], parser="test", parser_version="1",
                             blocks=[{"id": "x", "kind": "text", "text": "text", "locator": locator}])


def test_unlocated_failed_and_new_versions_never_inherit_reviews(evidence, tmp_path):
    store, source, conversion, _, _ = evidence
    citation = conversion["citations"][0]
    approve(store, citation, packet(store, citation, tmp_path)["packet_id"])
    second = store.add_conversion(source["source_id"], parser="fixture", parser_version="2",
                                  blocks=[{"id": "b1", "kind": "text", "text": "Synthetic value"}])
    item = store.get(second["citations"][0])
    assert item["block"]["locator"]["unlocated"]
    assert item["review_status"] == "pending"
    assert not store.check(second["citations"][0], "Synthetic value")["admitted"]
    failed = store.add_conversion(source["source_id"], parser="broken", parser_version="1",
                                  blocks=[], status="failed", issues=["no output"])
    assert failed["citations"] == []
    assert len(store.conversions("synthetic")) == 3
    assert store.search("no output") == []


def test_rejects_path_traversal_duplicate_ids_and_empty_review(evidence, tmp_path):
    store, source, conversion, _, native = evidence
    for bad_id in ["../../foo", "src_../x", "cv_" + "g" * 64]:
        with pytest.raises(EvidenceError):
            store.source(bad_id)
    block = {"id": "x", "kind": "text", "text": "x"}
    with pytest.raises(EvidenceError):
        store.add_conversion(source["source_id"], parser="test", parser_version="1", blocks=[block, block])
    with pytest.raises(EvidenceError):
        store.add_conversion(source["source_id"], parser="test", parser_version="1", blocks=[block],
                             artifacts={"../native": native})
    with pytest.raises(EvidenceError):
        store.review(conversion["citations"][0], "", decision="reject", reviewer="a",
                     reviewer_kind="human", note="")


def test_review_record_and_deleted_index_detected(evidence, tmp_path):
    store, _, conversion, _, _ = evidence
    citation = conversion["citations"][0]
    event = approve(store, citation, packet(store, citation, tmp_path)["packet_id"])
    with closing(sqlite3.connect(store.db_path)) as db, db:
        db.execute("DELETE FROM reviews WHERE review_id=?", (event["review_id"],))
    with pytest.raises(EvidenceError):
        store.check(citation, "Synthetic value")
    assert not store.audit()["ok"]


@pytest.mark.parametrize("text,quote", [
    ("Value ⁻5", "5"), ("Value 5%", "5"), ("Value 5×10", "5"),
    ("Value 5^2", "5"), ("Value 5,000", "5"), ("Value 5²", "5"),
    ("Value 5 %", "5"), ("Value - 5", "5"), ("Value 5e-3", "5"),
    ("Value 5·10", "5"), ("Value 5‰", "5"), ("Value 50", "5 0"),
    ("Value μg", "µg"), ("Count 1,234 patients", "234 patients"),
    ("Value .5", "5"), ("Value 0.5", ".5"), ("Value 10⁻5", "⁻5"),
])
def test_quote_must_not_truncate_or_rewrite_numeric_tokens(evidence, text, quote):
    store, source, _, _, _ = evidence
    result = store.add_conversion(source["source_id"], parser="boundary", parser_version="1",
                                  blocks=[{"id": "b", "kind": "text", "text": text, "locator": {"page": 1}}])
    assert not store.check(result["citations"][0], quote)["quote_matches"]


def test_forged_original_image_is_not_a_review_packet(evidence, tmp_path):
    import pymupdf

    store, source, conversion, _, _ = evidence
    citation = conversion["citations"][0]
    image = tmp_path / "forged.png"
    fake = pymupdf.open()
    fake.new_page(width=300, height=400).get_pixmap(matrix=pymupdf.Matrix(2, 2)).save(image)
    fake.close()
    with pytest.raises(EvidenceError, match="not the rendered original"):
        store.register_packet(citation, artifacts={"fake.png": image}, metadata={
            "citation": citation, "source_id": source["source_id"], "source_sha256": source["sha256"],
            "locator": store.get(citation)["block"]["locator"], "renderer": "pymupdf",
            "renderer_version": pymupdf.VersionBind, "scale": 2,
            "rendered_pages": [{"page": 1, "artifact": "fake.png"}],
        })


def test_region_only_cross_page_and_rotation_bounds(tmp_path):
    import pymupdf

    path = tmp_path / "rotated.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page(width=300, height=400).set_rotation(90)
        pdf.new_page(width=300, height=400)
        pdf.save(path)
    store = Store(tmp_path / "store")
    source = store.ingest(path, "rotated")
    result = store.add_conversion(source["source_id"], parser="fixture", parser_version="1", blocks=[{
        "id": "b", "kind": "text", "text": "cross-page", "locator": {"regions": [
            {"page": 1, "bbox": [0, 300, 200, 400]}, {"page": 2, "bbox": [0, 0, 200, 100]},
        ]},
    }])
    assert store.get(result["citations"][0])["block"]["location_accuracy"] == "region"


def test_ingestion_provenance_does_not_change_content_identity(evidence, tmp_path):
    store, source, _, pdf, _ = evidence
    copied = tmp_path / "another-location.pdf"
    copied.write_bytes(pdf.read_bytes())
    second = store.ingest(copied, "synthetic")
    assert second["source_id"] == source["source_id"]
    receipts = store.ingestion_receipts(source["source_id"])
    assert len(receipts) == 2
    assert {r["original_path"] for r in receipts} == {str(pdf.resolve()), str(copied.resolve())}
    assert all(r["acquisition"] == "local_copy" and r["sha256"] == source["sha256"] for r in receipts)
    assert all(r["timestamp"].endswith("+00:00") for r in receipts)
    receipt_file = store.root / "records" / "ingestions" / (receipts[0]["ingestion_id"] + ".json")
    receipt_file.write_text("tampered")
    assert not store.audit()["ok"]


def test_different_import_metadata_keeps_same_raw_revision(evidence):
    store, source, _, pdf, _ = evidence
    second = store.ingest(pdf, "synthetic", metadata={"origin": "parser_two"})
    assert second["source_id"] == source["source_id"]
    assert len(store.sources()) == 1
    receipts = store.ingestion_receipts(source["source_id"])
    assert [r["metadata"] for r in receipts] == [{}, {"origin": "parser_two"}]


def test_ai_may_reject_but_not_approve_or_revoke(evidence):
    store, _, conversion, _, _ = evidence
    citation = conversion["citations"][0]
    with pytest.raises(EvidenceError):
        store.review(citation, "Synthetic", decision="revoke", reviewer="AI", reviewer_kind="ai", note="test")
    event = store.review(citation, "Synthetic", decision="reject", reviewer="AI", reviewer_kind="ai", note="test")
    assert event["actor_kind"] == "ai"


def test_audit_detects_invented_block_in_both_indexes(evidence):
    store, _, _, _, _ = evidence
    fake_conversion = "cv_" + "a" * 64
    citation = f"evidence:{fake_conversion}#invented"
    with closing(sqlite3.connect(store.db_path)) as db, db:
        db.execute("INSERT INTO blocks VALUES (?,?,?,?,?,?,?)", (
            citation, fake_conversion, "invented", "fake", "text", "{}", "{}"))
        db.execute("INSERT INTO blocks_fts(citation,text) VALUES (?,?)", (citation, "fake"))
    assert not store.audit()["ok"]


def test_partial_conversion_warning_survives_every_discovery_path(evidence):
    store, source, _, _, _ = evidence
    conversion = store.add_conversion(source["source_id"], parser="partial-parser", parser_version="1.2",
                                      blocks=[{"id": "p", "kind": "text", "text": "partialword",
                                               "locator": {"page": 1}}],
                                      status="partial", issues=["table parsing failed"])
    citation = conversion["citations"][0]
    for item in (store.get(citation), store.check(citation, "partialword"), store.search("partialword")[0]):
        assert item["conversion_status"] == "partial"
        assert item["conversion_issues"] == ["table parsing failed"]
        assert item["parser"] == "partial-parser"
        assert item["parser_version"] == "1.2"
