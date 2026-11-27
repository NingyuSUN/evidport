"""Run the EvidPort side of the shared contract against synthetic, temporary stores."""
import hashlib
import json
import re
from operator import itemgetter
from pathlib import Path

import pymupdf
import pytest

from evidport.acquisition import AcquisitionQueue
from evidport.literature import _europe_record
from evidport.mcp_evidence import EvidenceTools
from evidport.memory import _CITATION, MEMORY_CODES, MemoryLedger
from evidport.packets import create_packet
from evidport.store import EvidenceError, Store
from evidport.triage import RULE_TO_MEMORY_CODE, import_validator_report

FIXTURE = Path(__file__).parent / "fixtures" / "golden_contract_vectors_v1.json"
VECTORS = json.loads(FIXTURE.read_bytes())
DOCUMENT = VECTORS["fixture_document"]
MEMORY = VECTORS["memory_ledger_and_pitfall_vectors"]


def test_shared_fixture_digest():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "c177b91e7037f7e6562671a7a400da91f6bcd61a262cc23d90831252e0721d2a"
    )


@pytest.mark.parametrize("vec", VECTORS["citation_vectors"], ids=itemgetter("id"))
def test_citation_syntax(tmp_path, vec):
    citation = vec["citation"]
    library_valid = bool(_CITATION.fullmatch(citation))
    assert library_valid is vec["library_citation_valid"]

    store = Store(tmp_path / "store")
    if vec["library_citation_valid"]:
        conversion_id, block_id = store._citation(citation)
        assert f"evidence:{conversion_id}#{block_id}" == citation
    else:
        with pytest.raises(EvidenceError):
            store._citation(citation)

    strict = library_valid and bool(re.search(r"#b\d+\Z", citation))
    assert strict is vec["ro_citation_re_valid"] is vec["valid"]


def write_pdf(tmp_path, degree_record=None):
    path = tmp_path / "paper.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        if degree_record is None or degree_record["include_title_and_doi_on_page_1"]:
            header = f"{DOCUMENT['title']}\n{DOCUMENT['doi']}"
            assert page.insert_textbox((30, 20, 540, 75), header, fontsize=10) >= 0
        for block in DOCUMENT["blocks"]:
            assert page.insert_textbox(block["locator"]["bbox"], block["text"], fontsize=10) >= 0
        if degree_record is not None:
            page.insert_text((30, 280), degree_record["cjk_degree_line"],
                             fontname=degree_record["pymupdf_cjk_fontname"])
        pdf.save(path)
    return path


@pytest.mark.parametrize("vec", [
    v for v in VECTORS["identity_rule_vectors"] if v["mode"] == "ingested_receipt"
], ids=itemgetter("id"))
def test_ingested_identity_receipt_round_trip(tmp_path, vec):
    # Consumers interpret rule versions. EvidPort preserves the recorded version and observations.
    identity = {
        "status": vec["identity_status"],
        "identity_rule_version": vec["identity_rule_version"],
        "title_match": vec["title_match"],
        "supplementary_frontmatter": vec["supplementary_frontmatter"],
    }
    store = Store(tmp_path / "store")
    source = store.ingest(write_pdf(tmp_path), DOCUMENT["document_id"], role=DOCUMENT["role"],
                          metadata={"identity_check": identity})
    receipt, = store.ingestion_receipts(source["source_id"])
    assert receipt["metadata"]["identity_check"] == identity


@pytest.mark.parametrize("vec", [
    v for v in VECTORS["identity_rule_vectors"]
    if v["mode"] == "acquisition_attach_pdf" or v["id"] == "identity_v3_confirmed"
], ids=itemgetter("id"))
def test_current_pdf_identity_rule(tmp_path, vec):
    store = Store(tmp_path / "store")
    queue = AcquisitionQueue(tmp_path / "queue", store)
    record = _europe_record(DOCUMENT["europe_pmc_record"])
    record.update(document_id=DOCUMENT["document_id"], role=vec.get("role", DOCUMENT["role"]))
    paper, = queue.register([record], provenance={"scope": "golden contract fixture"})["papers"]
    is_degree_record = vec["mode"] == "acquisition_attach_pdf"
    state = queue.attach(paper["paper_id"], write_pdf(tmp_path, vec if is_degree_record else None))
    identity = state["attachments"][-1]["identity"]
    assert identity["title_match"] and identity["doi_match"]
    if is_degree_record:
        assert {
            "identity_rule_version": identity["identity_rule_version"],
            "degree_record_wording": identity["degree_record_wording"],
            "identity_status": state["identity_status"],
            "availability": state["availability"],
            "reason": state["last_failure"],
            "source_ingested": bool(store.sources()),
        } == vec["expected_acquisition"]
        assert state["source_id"] is None
    else:
        assert state["identity_status"] == vec["identity_status"]
        assert identity["identity_rule_version"] == vec["identity_rule_version"]
        assert identity["supplementary_frontmatter"] is vec["supplementary_frontmatter"]
        receipt, = store.ingestion_receipts(state["source_id"])
        assert receipt["metadata"]["identity_check"] == identity


def make_conversion(tmp_path, has_matching_ocr):
    store = Store(tmp_path / "store")
    source = store.ingest(write_pdf(tmp_path), DOCUMENT["document_id"], role=DOCUMENT["role"])
    blocks = []
    for data in DOCUMENT["blocks"]:
        block = {key: data[key] for key in ("id", "kind", "text", "locator")}
        block.update(superscripts=[], symbol_font_spans=[])
        if has_matching_ocr:
            block["independent_reading"] = {
                "text": data["text"], "engine": "synthetic", "engine_version": "1",
            }
        blocks.append(block)
    conversion = store.add_conversion(
        source["source_id"], parser="synthetic", parser_version="1", blocks=blocks,
    )
    citations = {c.split("#")[1]: c for c in conversion["citations"]}
    return store, citations


@pytest.mark.parametrize("vec", VECTORS["quote_admission_vectors"], ids=itemgetter("id"))
def test_quote_admission(tmp_path, vec):
    store, citations = make_conversion(tmp_path, vec["has_matching_ocr"])
    citation, quote = citations[vec["block_id"]], vec["quote"]
    for index, event in enumerate(vec["review_events"]):
        review = event.copy()
        packet_id = None
        if review.pop("with_packet"):
            packet_id = create_packet(store, citation, tmp_path / f"packet-{index}")["packet_id"]
        store.review(citation, quote, packet_id=packet_id, **review)

    checked = store.check(citation, quote, accept_independent_reading=vec["accept_independent_reading"])
    expected = vec["expected_evidport"]
    for field in ("quote_matches", "admitted", "admission_route", "reason", "review_status",
                  "scientifically_verified"):
        assert checked[field] == expected[field], field
    suggestions = [s["quote"] for s in checked["suggested_quotes"]]
    assert bool(suggestions) is expected["suggested_quotes_nonempty"]
    assert suggestions == expected.get("expected_suggested_quotes", [])


def test_memory_vocabulary():
    assert list(MEMORY_CODES) == MEMORY["memory_codes"]
    assert {rule: code for rule, code in RULE_TO_MEMORY_CODE.items() if rule.startswith("BEV")} == (
        MEMORY["rule_to_memory_code_mapping"]
    )


def validator_report(vec, citations):
    block_text = {block["id"]: block["text"] for block in DOCUMENT["blocks"]}
    anchors = [{"source": "S1", "block_id": vec["anchor_block"], "quote": vec["anchor_quote"]}]
    for sibling in vec["sibling_blocks"]:
        anchors.append({"source": "S1", "block_id": sibling, "quote": block_text[sibling]})
    facet = {key: vec[key] for key in ("verdict", "reason", "source_fact", "claim_fact")}
    return {"semantic_review": {
        "checks": {vec["facet_name"]: {**facet, "evidence": anchors}},
        "reviewed_quotes": [{**anchor, "citation": citations[anchor["block_id"]]} for anchor in anchors],
    }}


@pytest.mark.parametrize("vec", MEMORY["import_vectors"], ids=itemgetter("id"))
def test_advisory_memory_import(tmp_path, vec):
    store, citations = make_conversion(tmp_path, has_matching_ocr=True)
    citation, quote = citations[vec["anchor_block"]], vec["anchor_quote"]
    policies = (False, True)
    before = {opt_in: store.check(citation, quote, accept_independent_reading=opt_in) for opt_in in policies}
    assert not before[False]["admitted"] and before[True]["admitted"]

    ledger = MemoryLedger(tmp_path / "memory")
    imported = import_validator_report([validator_report(vec, citations)], store=store, ledger=ledger)
    memory_id, = imported["added_memory_ids"]
    invariants = MEMORY["public_note_invariants"]
    for opt_in in policies:
        after = store.check(citation, quote, accept_independent_reading=opt_in)
        assert (after["admitted"] != before[opt_in]["admitted"]) is invariants["alters_store_check_admission"]
        assert after == before[opt_in]
        tools = EvidenceTools(store.root, tmp_path / "review", memory_root=ledger.root,
                              accept_independent_reading=opt_in)
        response = tools.check_quote(citation, quote)
        assert {field: response[field] for field in after} == after
        note, = response["memory"]["notes"]
        assert note["memory_id"] == memory_id
        assert note["citation"] == citation
        assert note["related_citations"] == [citations[b] for b in vec["sibling_blocks"]]
        assert note["code"] == vec["expected_memory_code"]
        override = MEMORY["conflict_uncertain_override"]
        if vec["verdict"] == override["verdict"] and override["reason_keyword"] in vec["reason"].lower():
            assert note["code"] == override["mapped_code"]
        else:
            assert note["code"] == RULE_TO_MEMORY_CODE[vec["validator_rule"]]
        assert note["citable"] is vec["expected_citable"] is invariants["citable"]
        assert note["authority"] == invariants["authority"]
        assert note["scientifically_verified"] is invariants["scientifically_verified"]
        assert ("actor" not in note) is invariants["omits_local_actor"]
