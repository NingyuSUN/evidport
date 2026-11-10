"""End-to-end synthetic fixtures: these reviews never approve real literature."""
from pathlib import Path

import pymupdf
import pytest

from evidence_database.adapters import run_baseline
from evidence_database.packets import create_packet
from evidence_database.store import EvidenceError, Store


def make_pdf(path: Path, text: str) -> None:
    with pymupdf.open() as document:
        page = document.new_page(width=400, height=300)
        page.insert_text((25, 50), text)
        document.save(path)


@pytest.fixture
def workflow(tmp_path):
    original = tmp_path / "same filename.pdf"
    make_pdf(original, "Synthetic experiment: dose 1.25 mg; count 20.")
    store = Store(tmp_path / "store")
    source = store.ingest(original, "explicit-synthetic-fixture", metadata={"synthetic": True})
    conversion = run_baseline(store, source["source_id"])
    hits = store.search("Synthetic experiment")
    hit = next(item for item in hits if "dose 1.25 mg" in item["text"])
    return store, source, conversion, hit["citation"], original


def test_discovery_to_quote_check_keeps_realistic_unreviewed_state(workflow):
    store, source, conversion, citation, _ = workflow
    checked = store.check(citation, "dose 1.25 mg")
    assert checked["quote_matches"] and not checked["admitted"]
    assert checked["scientifically_verified"] is False
    assert not store.check(citation, "dose 25 mg")["quote_matches"]
    assert not store.check(citation, "25 mg")["quote_matches"]
    assert not store.check(citation, "dose 1.25 mg", require_review=False)["admitted"]
    assert store.get(citation)["source"]["source_id"] == source["source_id"]
    assert citation.startswith(f"evidence:{conversion['conversion_id']}#")


def test_synthetic_review_revoke_and_same_filename_new_version(workflow, tmp_path):
    store, source, conversion, citation, original = workflow
    packet = create_packet(store, citation, tmp_path / "synthetic-review")
    store.review(citation, "dose 1.25 mg", decision="approve", reviewer="SYNTHETIC TEST FIXTURE",
                 reviewer_kind="human", note="Automated synthetic lifecycle test, no real human review",
                 viewed_original=True, packet_id=packet["packet_id"])
    assert store.check(citation, "dose 1.25 mg")["admitted"]
    assert not store.check(citation, "count 20")["admitted"]
    old_bytes = Path(store.source(source["source_id"])["path"]).read_bytes()
    original.unlink()
    make_pdf(original, "Synthetic revised experiment: dose 2.50 mg; count 20.")
    revised_source = store.ingest(original, "explicit-synthetic-fixture", metadata={"synthetic": True})
    revised = run_baseline(store, revised_source["source_id"])
    assert source["source_id"] != revised_source["source_id"]
    assert conversion["conversion_id"] != revised["conversion_id"]
    assert Path(store.source(source["source_id"])["path"]).read_bytes() == old_bytes
    assert len(store.conversions("explicit-synthetic-fixture")) == 2
    assert not store.check(revised["citations"][0], "dose 2.50 mg")["admitted"]
    store.review(citation, "dose 1.25 mg", decision="revoke", reviewer="SYNTHETIC TEST FIXTURE",
                 reviewer_kind="human", note="Synthetic revoke demonstration")
    assert not store.check(citation, "dose 1.25 mg")["admitted"]
    assert len(store.reviews(citation)) == 2


def test_original_snapshot_tamper_blocks_previously_reviewed_quote(workflow, tmp_path):
    store, source, _, citation, _ = workflow
    packet = create_packet(store, citation, tmp_path / "synthetic-review")
    store.review(citation, "dose 1.25 mg", decision="approve", reviewer="SYNTHETIC TEST FIXTURE",
                 reviewer_kind="human", note="Synthetic-only approval", viewed_original=True,
                 packet_id=packet["packet_id"])
    stored = Path(store.source(source["source_id"])["path"])
    stored.write_bytes(stored.read_bytes() + b"tampered")
    with pytest.raises(EvidenceError):
        store.check(citation, "dose 1.25 mg")
    with pytest.raises(EvidenceError):
        store.search("Synthetic experiment")


def test_review_packet_cannot_be_reused_for_another_conversion(workflow, tmp_path):
    store, source, _, citation, _ = workflow
    packet = create_packet(store, citation, tmp_path / "synthetic-review")
    second = store.add_conversion(source["source_id"], parser="synthetic-other", parser_version="1",
                                  blocks=[{"id": "same-quote", "kind": "text",
                                           "text": "dose 1.25 mg", "locator": {"page": 1}}])
    other_citation = second["citations"][0]
    with pytest.raises(EvidenceError):
        store.review(other_citation, "dose 1.25 mg", decision="approve", reviewer="SYNTHETIC TEST FIXTURE",
                     reviewer_kind="human", note="Packet substitution must fail", viewed_original=True,
                     packet_id=packet["packet_id"])
    assert not store.check(other_citation, "dose 1.25 mg")["admitted"]


def test_partial_geometry_remains_visible_after_exact_synthetic_quote_review(workflow, tmp_path):
    store, source, _, _, _ = workflow
    partial = store.add_conversion(source["source_id"], parser="synthetic-partial", parser_version="1",
                                   status="partial", issues=["native geometry requires review"],
                                   blocks=[{"id": "page-only", "kind": "text", "text": "dose 1.25 mg",
                                            "locator": {"page": 1},
                                            "issues": ["bbox unavailable; physical page retained"]}])
    citation = partial["citations"][0]
    pending = store.check(citation, "dose 1.25 mg")
    assert not pending["admitted"] and pending["conversion_status"] == "partial"
    packet = create_packet(store, citation, tmp_path / "synthetic-partial-review")
    assert "native geometry requires review" in Path(packet["html"]).read_text(encoding="utf-8")
    store.review(citation, "dose 1.25 mg", decision="approve", reviewer="SYNTHETIC TEST FIXTURE",
                 reviewer_kind="human", note="Synthetic exact quote only; partial geometry remains disclosed",
                 viewed_original=True, packet_id=packet["packet_id"])
    checked = store.check(citation, "dose 1.25 mg")
    assert checked["admitted"] and checked["conversion_status"] == "partial"
    assert checked["conversion_issues"] == ["native geometry requires review"]
    assert not checked["scientifically_verified"]
