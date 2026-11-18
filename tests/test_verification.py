"""Machine verification with synthetic papers and a stand-in OCR command; no real approvals."""
import json
import sys

import pymupdf
import pytest

from evidence_database.adapters import run_baseline
from evidence_database.cli import main
from evidence_database.mcp_evidence import EvidenceTools
from evidence_database.store import EvidenceError, Store
from evidence_database.verification import (
    RegionReader,
    collapsed_positions,
    lenient,
    marked_ranges,
    text_defects,
)
from evidence_database.workflow import load_project, sync_project

SENTENCE = "The synthetic measured concentration was 17.25 units per litre in marker samples."

# Stands in for an OCR engine: reports a version, otherwise prints the text of a file next to it,
# whatever image arrives on standard input. Exits non-zero when that file says FAIL.
FAKE_ENGINE = """import pathlib, sys
if "--version" in sys.argv:
    print("fakeocr 9.9")
    raise SystemExit(0)
sys.stdin.buffer.read()
text = pathlib.Path(__file__).with_name("reading.txt").read_text(encoding="utf-8")
if text == "FAIL":
    raise SystemExit(3)
sys.stdout.buffer.write(text.encode("utf-8"))
"""


def engine(tmp_path, reading):
    folder = tmp_path / "engine"
    folder.mkdir(exist_ok=True)
    (folder / "fake_ocr.py").write_text(FAKE_ENGINE, encoding="utf-8")
    (folder / "reading.txt").write_text(reading, encoding="utf-8")
    return [sys.executable, str(folder / "fake_ocr.py")]


def paper(tmp_path, text=SENTENCE):
    path = tmp_path / "paper.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=400, height=300).insert_textbox((30, 30, 370, 200), text)
        doc.save(path)
    return path


def converted(tmp_path, reading):
    store = Store(tmp_path / "store")
    source = store.ingest(paper(tmp_path), "synthetic-paper")
    conversion = run_baseline(store, source["source_id"], ocr_command=engine(tmp_path, reading))
    return store, conversion


def block_store(tmp_path, **block):
    """A stored block with chosen machine-verification fields, bypassing extraction."""
    store = Store(tmp_path / "store")
    source = store.ingest(paper(tmp_path), "synthetic-paper")
    value = {"id": "b1", "kind": "paragraph", "text": SENTENCE, "locator": {"page": 1, "bbox": [30, 30, 370, 60]},
             "superscripts": [], "symbol_font_spans": [],
             "independent_reading": {"text": SENTENCE, "engine": "fakeocr",
                                                         "engine_version": "9.9"}}
    value.update(block)
    value = {key: item for key, item in value.items() if item is not None}
    conversion = store.add_conversion(source["source_id"], parser="fixture", parser_version="1", blocks=[value])
    return store, conversion["citations"][0]


def test_lenient_folds_typography_but_keeps_characters():
    assert lenient("signifi-\ncant \u201cresult\u201d 5 \u2212 3 \ufb01eld") == 'significant "result" 5 - 3 field'
    assert lenient("0.05") != lenient("0.06")


@pytest.mark.parametrize(("text", "code"), [
    ("change \x0533.33 fold", "control_character"),
    ("2019;\u200b17:\u200b123", "invisible_character"),
    ("informa\u019fon about code", "unmapped_glyph"),
    ("molecules (R \u00a8odel et al.)", "detached_diacritic"),
    ("adult brain samples,which occur", "missing_word_space"),
])
def test_known_text_layer_defects_are_located(text, code):
    found = text_defects(text)
    assert {item[2] for item in found} == {code}
    assert all(0 <= start < end <= len(text) for start, end, _ in found)


def test_ordinary_scientific_text_has_no_defects():
    assert text_defects("The 5\u00b4-UTR of HLA-G (P < 0.05; n = 12) was up-regulated, e.g., in B\u00fcrger et al.") == []


def test_collapsed_positions_follow_whitespace_collapsing():
    text = " a \n b  c "
    collapsed = " ".join(text.split())
    positions = collapsed_positions(text)
    assert len(positions) == len(text) + 1
    assert all(collapsed[positions[i]] == ch for i, ch in enumerate(text) if not ch.isspace())


def test_marked_ranges_need_an_exact_reconstruction():
    text = " P < 5 \u00d7 10\u22128)\nnext line\n"
    lines = [[(" P < 5 \u00d7 10", False), ("\u22128", True), (")", False)], [("next line", False)]]
    ranges = marked_ranges(text, lines)
    assert [text.strip()[start:end] for start, end in ranges] == ["\u22128"]
    assert marked_ranges(text, [[("something else", False)]]) is None
    assert marked_ranges(text, [lines[0]]) is None  # spans stop before the block text does
    with_empty_line = [lines[0], [("", False)], lines[1]]  # span data may carry lines the block text omits
    assert marked_ranges(text, with_empty_line) == ranges
    split = [[(" P < 5 \u00d7 10", False), ("\u2212", True), ("8", True), (")", False)], lines[1]]
    assert marked_ranges(text, split) == ranges  # adjacent raised spans merge


def test_baseline_records_reading_and_engine_without_changing_text(tmp_path):
    store, conversion = converted(tmp_path, SENTENCE)
    block = conversion["blocks"][0]
    assert " ".join(block["text"].split()) == SENTENCE
    assert block["independent_reading"] == {"text": SENTENCE, "engine": "fakeocr", "engine_version": "9.9"}
    assert block["superscripts"] == [] and block["symbol_font_spans"] == []
    recorded = conversion["settings"]["independent_reading"]
    assert recorded["engine"] == "fakeocr" and recorded["blocks_read"] == 1 and recorded["blocks_failed"] == 0
    assert conversion["settings"]["adapter_version"] == "2" and conversion["settings"]["ocr"] is False
    assert store.audit()["ok"]


def test_agreement_admits_only_when_the_route_is_enabled(tmp_path):
    store, conversion = converted(tmp_path, SENTENCE)
    citation = conversion["citations"][0]
    default = store.check(citation, "concentration was 17.25 units")
    assert default["admitted"] is False and default["reason"] == "pending_review"
    assert default["machine_verification"]["status"] == "agrees" and default["admission_route"] is None
    enabled = store.check(citation, "concentration was 17.25 units", accept_independent_reading=True)
    assert enabled["admitted"] is True and enabled["reason"] == "independent_reading_agreement"
    assert enabled["admission_route"] == "independent_reading" and enabled["review_id"] is None
    assert enabled["machine_verification"] == {"status": "agrees", "blocking": [], "engine": "fakeocr",
                                               "engine_version": "9.9"}
    assert "no person reviewed it" in enabled["meaning"] and enabled["scientifically_verified"] is False
    assert store.audit()["counts"]["reviews"] == 0
    inspected = store.check(citation, "concentration was 17.25 units", require_review=False,
                            accept_independent_reading=True)
    assert inspected["admitted"] is False and inspected["reason"] == "inspection_only"


def test_differing_reading_or_fabricated_quote_is_not_admitted(tmp_path):
    store, conversion = converted(tmp_path, SENTENCE.replace("17.25", "17.26"))
    citation = conversion["citations"][0]
    differing = store.check(citation, "concentration was 17.25 units", accept_independent_reading=True)
    assert differing["admitted"] is False and differing["reason"] == "pending_review"
    assert differing["machine_verification"]["status"] == "disagrees"
    words = store.check(citation, "units per litre in marker samples", accept_independent_reading=True)
    assert words["admitted"] is True  # the part both readings share
    fabricated = store.check(citation, "concentration was 71.25 units", accept_independent_reading=True)
    assert fabricated["admitted"] is False and fabricated["reason"] == "quote_not_found"
    assert fabricated["machine_verification"]["status"] == "not_applicable"


def test_numeric_boundaries_also_apply_to_the_reading(tmp_path):
    store, citation = block_store(tmp_path, independent_reading={"text": SENTENCE.replace("17.25", "117.25")})
    checked = store.check(citation, "17.25 units", accept_independent_reading=True)
    assert checked["quote_matches"] is True and checked["admitted"] is False
    assert checked["machine_verification"]["status"] == "disagrees"


def test_quote_touching_a_superscript_or_defect_stays_with_a_person(tmp_path):
    text = "Loci were significant (P < 5 \u00d7 10\u22128) in the discovery sample of 33,992 participants."
    start = text.index("\u22128")
    store, citation = block_store(tmp_path, text=text, superscripts=[[start, start + 2]],
                                  independent_reading={"text": text})
    raised = store.check(citation, "P < 5 \u00d7 10\u22128", accept_independent_reading=True)
    assert raised["admitted"] is False
    assert raised["machine_verification"] == {"status": "not_eligible", "blocking": ["superscript_position_lost"],
                                              "engine": None, "engine_version": None}
    clean = store.check(citation, "discovery sample of 33,992 participants", accept_independent_reading=True)
    assert clean["admitted"] is True


def test_control_character_blocks_only_the_affected_quote(tmp_path):
    text = "MT1H fold change \x0533.33 was observed in eutopic endometrium samples."
    store, citation = block_store(tmp_path, text=text, independent_reading={"text": text})
    sign = store.check(citation, "fold change \x0533.33", accept_independent_reading=True)
    assert sign["admitted"] is False and sign["machine_verification"]["blocking"] == ["control_character"]
    assert store.check(citation, "observed in eutopic endometrium samples",
                       accept_independent_reading=True)["admitted"] is True


@pytest.mark.parametrize(("change", "status", "blocking"), [
    ({"independent_reading": None}, "not_run", ["independent_reading_not_recorded"]),
    ({"superscripts": None}, "not_eligible", ["typography_not_recorded"]),
    ({"superscripts": [[5, 9999]]}, "not_eligible", ["typography_not_recorded"]),
    ({"symbol_font_spans": None}, "not_eligible", ["typography_not_recorded"]),
    ({"kind": "table"}, "not_eligible", ["not_a_text_paragraph"]),
    ({"issues": ["native_bbox_rotation_unverified_page_only"]}, "not_eligible", ["block_has_extraction_issues"]),
    ({"locator": {"page": 1}}, "not_eligible", ["no_exact_page_region"]),
])
def test_ineligible_blocks_are_never_machine_admitted(tmp_path, change, status, blocking):
    store, citation = block_store(tmp_path, **change)
    checked = store.check(citation, "concentration was 17.25 units", accept_independent_reading=True)
    assert checked["admitted"] is False and checked["reason"] == "pending_review"
    assert checked["machine_verification"]["status"] == status
    assert checked["machine_verification"]["blocking"] == blocking


def test_recorded_rejection_overrides_agreement(tmp_path):
    store, citation = block_store(tmp_path)
    quote = "concentration was 17.25 units"
    assert store.check(citation, quote, accept_independent_reading=True)["admitted"] is True
    store.review(citation, quote, decision="reject", reviewer="synthetic reviewer", reviewer_kind="human",
                 note="synthetic fixture rejection")
    rejected = store.check(citation, quote, accept_independent_reading=True)
    assert rejected["admitted"] is False and rejected["reason"] == "reject"
    assert rejected["machine_verification"]["status"] == "agrees"
    other = store.check(citation, "units per litre", accept_independent_reading=True)
    assert other["admitted"] is True  # reviews are quotation-specific


def test_unavailable_engine_keeps_the_extraction_and_says_so(tmp_path):
    store = Store(tmp_path / "store")
    source = store.ingest(paper(tmp_path), "synthetic-paper")
    missing = run_baseline(store, source["source_id"], ocr_command=[sys.executable, str(tmp_path / "absent.py")])
    assert missing["status"] == "completed" and missing["settings"]["independent_reading"] is None
    assert any(issue.startswith("independent_reading_unavailable") for issue in missing["issues"])
    assert "independent_reading" not in missing["blocks"][0]
    failing = run_baseline(store, source["source_id"], ocr_command=engine(tmp_path, "FAIL"))
    assert failing["settings"]["independent_reading"]["blocks_failed"] == 1
    assert "independent_reading_failed_blocks: 1" in failing["issues"]
    checked = store.check(failing["citations"][0], "17.25 units", accept_independent_reading=True)
    assert checked["admitted"] is False and checked["machine_verification"]["status"] == "not_run"


def test_no_command_means_no_reading_and_environment_can_supply_one(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    source = store.ingest(paper(tmp_path), "synthetic-paper")
    monkeypatch.delenv("EVIDPORT_OCR_COMMAND", raising=False)
    plain = run_baseline(store, source["source_id"])
    assert plain["settings"]["independent_reading"] is None and "independent_reading" not in plain["blocks"][0]
    monkeypatch.setenv("EVIDPORT_OCR_COMMAND", json.dumps(engine(tmp_path, SENTENCE)))
    assert run_baseline(store, source["source_id"])["blocks"][0]["independent_reading"]["engine"] == "fakeocr"
    with pytest.raises(ValueError):
        RegionReader([])


def test_mcp_check_quote_follows_the_project_admission_setting(tmp_path):
    store, conversion = converted(tmp_path, SENTENCE)
    citation = conversion["citations"][0]
    strict = EvidenceTools(store.root, tmp_path / "review")
    assert strict.check_quote(citation, "17.25 units")["admitted"] is False
    enabled = EvidenceTools(store.root, tmp_path / "review", accept_independent_reading=True)
    checked = enabled.check_quote(citation, "17.25 units")
    assert checked["admitted"] is True and checked["admission_route"] == "independent_reading"
    with pytest.raises(EvidenceError):
        EvidenceTools(store.root, tmp_path / "review", accept_independent_reading="yes")


def test_cli_check_flag_enables_the_route(tmp_path, capsys):
    store, conversion = converted(tmp_path, SENTENCE)
    arguments = ["--db", str(store.root), "check", "--citation", conversion["citations"][0],
                 "--quote", "17.25 units"]
    assert main(arguments) == 2
    assert main(arguments + ["--accept-independent-reading"]) == 0
    assert json.loads(capsys.readouterr().out.split("\n}\n")[-2] + "\n}")["admission_route"] == "independent_reading"


def project(tmp_path, **verification):
    title = "Synthetic project workflow verifies concentration measurements in marker samples"
    path = tmp_path / "paper.pdf"
    with pymupdf.open() as doc:
        doc.new_page().insert_textbox((30, 30, 550, 300), title + "\nDOI: 10.5555/verification\n" + SENTENCE)
        doc.save(path)
    value = {"schema": 1, "project_id": "synthetic-project", "store": "store", "queue": "queue",
             "inbox": "inbox", "runs": "runs",
             "papers": [{"metadata": {"doi": "10.5555/verification", "title": title,
                                      "document_id": "synthetic-paper"}, "file": "paper.pdf"}]}
    if verification:
        value["verification"] = verification
    config = tmp_path / "project.json"
    config.write_text(json.dumps(value), encoding="utf-8")
    return config


def test_project_setting_is_validated(tmp_path):
    assert load_project(project(tmp_path))["verification"] == {}
    loaded = load_project(project(tmp_path, admission="independent_reading", ocr_command=["tesseract"]))
    assert loaded["verification"] == {"admission": "independent_reading", "ocr_command": ["tesseract"]}
    for invalid in ({"admission": "automatic"}, {"ocr_command": "tesseract"}, {"ocr_command": []},
                    {"unknown": True}):
        with pytest.raises(EvidenceError):
            load_project(project(tmp_path, **invalid))


def test_sync_reextracts_once_when_a_reading_is_requested(tmp_path):
    first = sync_project(project(tmp_path))
    plain = first["papers"][0]["conversion_id"]
    store = Store(tmp_path / "store")
    assert store.conversion(plain)["settings"]["independent_reading"] is None
    config = project(tmp_path, admission="independent_reading", ocr_command=engine(tmp_path, SENTENCE))
    second = sync_project(config)
    read = second["papers"][0]["conversion_id"]
    assert read != plain and store.conversion(read)["settings"]["independent_reading"]["engine"] == "fakeocr"
    assert store.conversion(plain)["status"] == "completed"  # the earlier version is kept
    assert sync_project(config)["papers"][0]["conversion_id"] == read
    assert store.audit()["counts"]["reviews"] == 0


def test_letter_in_a_symbol_font_keeps_its_quote_with_a_person(tmp_path):
    """Publisher symbol fonts map other glyphs onto letters: a micro sign extracted as m reads as ml."""
    path = tmp_path / "symbol.pdf"
    parts = [("Samples of 500 ", "helv"), ("m", "cour"), ("l were added to the binding column.", "helv")]
    with pymupdf.open() as doc:
        page, x = doc.new_page(width=400, height=200), 30.0
        for text, font in parts:
            page.insert_text((x, 60), text, fontname=font, fontsize=11)
            x += pymupdf.get_text_length(text, fontname=font, fontsize=11)
        doc.save(path)
    sentence = "".join(text for text, _ in parts)
    store = Store(tmp_path / "store")
    source = store.ingest(path, "symbol-paper")
    conversion = run_baseline(store, source["source_id"], ocr_command=engine(tmp_path, sentence))
    block = next(b for b in conversion["blocks"] if "500" in b["text"])
    assert " ".join(block["text"].split()) == sentence
    assert [block["text"][start:end] for start, end in block["symbol_font_spans"]] == ["m"]
    citation = f"evidence:{conversion['conversion_id']}#{block['id']}"
    unit = store.check(citation, "500 ml were added", accept_independent_reading=True)
    assert unit["admitted"] is False and unit["machine_verification"]["blocking"] == ["symbol_font_letter"]
    assert store.check(citation, "added to the binding column", accept_independent_reading=True)["admitted"] is True
