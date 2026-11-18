"""Quotations taken from stored text by position, and typed quotations resolved to stored text."""
import json

import pymupdf
import pytest

from evidence_database.cli import main
from evidence_database.mcp_evidence import EvidenceTools
from evidence_database.quoting import align, sentence_span, sentences
from evidence_database.store import EvidenceError, Store

# As a text layer stores it: a word broken at a line end, an equals sign and a minus sign stored as
# control characters, an en dash where a reader types a hyphen.
BLOCK = ("As described previously (25). Real-time PCR was performed on n \x01 3 samples in the dis-\n"
         "ease condition. Fold change was \x052.27 in tissue. First–strand cDNA was generated twice.")
READING = ("As described previously (25). Real-time PCR was performed on n = 3 samples in the dis-\n"
           "ease condition. Fold change was -2.27 in tissue. First-strand cDNA was generated twice.")


def stored(tmp_path, text=BLOCK, reading=READING):
    path = tmp_path / "paper.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=400, height=300).insert_textbox((30, 30, 370, 200), "Synthetic original.")
        doc.save(path)
    store = Store(tmp_path / "store")
    source = store.ingest(path, "synthetic-paper")
    block = {"id": "b1", "kind": "paragraph", "text": text, "locator": {"page": 1, "bbox": [30, 30, 370, 60]},
             "superscripts": [], "symbol_font_spans": [],
             "independent_reading": {"text": reading, "engine": "fakeocr", "engine_version": "9.9"}}
    conversion = store.add_conversion(source["source_id"], parser="fixture", parser_version="1", blocks=[block])
    return store, conversion["citations"][0]


def test_sentences_rebuild_the_collapsed_text_and_spans_are_stored_text():
    collapsed = " ".join(BLOCK.split())
    spans = sentences(BLOCK)
    assert len(spans) == 4
    assert " ".join(collapsed[start:end] for start, end in spans) == collapsed
    assert sentence_span(BLOCK, 4) == "First–strand cDNA was generated twice."
    assert sentence_span(BLOCK, 2, 3) == collapsed[spans[1][0]:spans[2][1]]
    assert sentences("  \n ") == []


@pytest.mark.parametrize(("first", "last"), [(0, 1), (3, 2), (1, 5), (True, 1), ("1", 2), (1.0, 2)])
def test_sentence_positions_are_checked(first, last):
    with pytest.raises(ValueError, match="Sentence positions"):
        sentence_span(BLOCK, first, last)


def test_typed_quotation_resolves_to_the_stored_text_and_says_what_differed():
    rows = align(BLOCK, "Real-time PCR was performed on n = 3 samples in the disease condition")
    assert [row["quote"] for row in rows] == [
        "Real-time PCR was performed on n \x01 3 samples in the dis- ease condition"]
    assert rows[0]["differences"] == ["line_end_hyphenation", "symbol_not_stored_by_text_layer"]
    rows = align(BLOCK, "First-strand cDNA was generated twice")
    assert rows[0]["quote"] == "First–strand cDNA was generated twice"
    assert rows[0]["differences"] == ["dash_quote_or_ligature_style"]
    rows = align(BLOCK, "Fold change was −2.27 in tissue...")
    assert rows[0]["quote"] == "Fold change was \x052.27 in tissue"
    assert "ellipsis_removed" in rows[0]["differences"]


@pytest.mark.parametrize("quote", [
    "Real-time PCR was performed on n = 4 samples",   # a digit is never substituted
    "Fold change was x2.27 in tissue",               # nor a letter for a lost symbol
    "Real-time PCR was done on n = 3 samples",       # reworded
    "Invented result absent from the block",
])
def test_changed_letters_digits_or_wording_resolve_to_nothing(quote):
    assert align(BLOCK, quote) == []


def test_ambiguous_place_resolves_to_nothing():
    twice = "The value rose in group A. The value rose in group B."
    assert align(twice, "The value rose in group") == []
    assert len(align(twice, "The value rose in group B")) == 1


def test_stitched_quotation_becomes_its_parts_in_reading_order():
    rows = align(BLOCK, "Real-time PCR was performed ... First-strand cDNA was generated twice.")
    assert [row["quote"] for row in rows] == ["Real-time PCR was performed",
                                              "First–strand cDNA was generated twice."]
    assert all("stitched_with_ellipsis" in row["differences"] for row in rows)
    assert align(BLOCK, "First-strand cDNA was generated twice ... Real-time PCR was performed") == []
    assert align(BLOCK, "Real-time PCR was performed ... an invented second passage") == []


def test_check_offers_stored_text_without_admitting_the_typed_quotation(tmp_path):
    store, citation = stored(tmp_path)
    typed = store.check(citation, "First-strand cDNA was generated twice", accept_independent_reading=True)
    assert typed["quote_matches"] is False and typed["admitted"] is False and typed["reason"] == "quote_not_found"
    assert typed["suggested_quotes"] == [{"quote": "First–strand cDNA was generated twice",
                                          "differences": ["dash_quote_or_ligature_style"]}]
    again = store.check(citation, typed["suggested_quotes"][0]["quote"], accept_independent_reading=True)
    assert again["admitted"] is True and again["suggested_quotes"] == []
    # Stored text with a control character is found, and still goes to a person.
    typed = store.check(citation, "Fold change was -2.27 in tissue", accept_independent_reading=True)
    held = store.check(citation, typed["suggested_quotes"][0]["quote"], accept_independent_reading=True)
    assert held["quote_matches"] is True and held["admitted"] is False
    assert "control_character" in held["machine_verification"]["blocking"]
    assert store.check(citation, "Invented result absent from the block")["suggested_quotes"] == []


def test_suggestion_respects_number_boundaries(tmp_path):
    store, citation = stored(tmp_path, "The dose was 15.2 mg in group A.", "The dose was 15.2 mg in group A.")
    assert store.check(citation, "5.2 mg in group A")["suggested_quotes"] == []


def test_quotation_by_position_is_stored_text(tmp_path):
    store, citation = stored(tmp_path)
    numbered = store.sentences(citation)
    assert [row["n"] for row in numbered] == [1, 2, 3, 4]
    quote = store.sentence_quote(citation, 4)
    assert quote == numbered[3]["text"] and store.check(citation, quote, accept_independent_reading=True)["admitted"]
    assert store.sentence_quote(citation, 1, 2) == numbered[0]["text"] + " " + numbered[1]["text"]
    with pytest.raises(EvidenceError, match="Sentence positions"):
        store.sentence_quote(citation, 5)


def test_mcp_tools_quote_by_position(tmp_path):
    store, citation = stored(tmp_path)
    tools = EvidenceTools(store.root, tmp_path / "review", accept_independent_reading=True)
    assert "sentences" not in tools.get(citation)
    numbered = tools.get(citation, sentences=True)["sentences"]
    checked = tools.check_quote(citation, first_sentence=4)
    assert checked["quote"] == numbered[3]["text"] and checked["admitted"] is True
    assert tools.check_quote(citation, first_sentence=1, last_sentence=2)["quote_matches"] is True
    typed = tools.check_quote(citation, "First-strand cDNA was generated twice")
    assert typed["admitted"] is False and typed["suggested_quotes"][0]["quote"].startswith("First–strand")
    for arguments in ({"quote": "x", "first_sentence": 1}, {"last_sentence": 2}, {}, {"first_sentence": 9}):
        with pytest.raises(EvidenceError):
            tools.check_quote(citation, **arguments)
    with pytest.raises(EvidenceError):
        tools.get(citation, sentences="yes")


def test_cli_quotes_by_position(tmp_path, capsys):
    store, citation = stored(tmp_path)
    base = ["--db", str(store.root)]
    assert main(base + ["show", "--citation", citation, "--sentences"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert len(shown["sentences"]) == 4
    assert main(base + ["check", "--citation", citation, "--sentences", "4", "--accept-independent-reading"]) == 0
    assert json.loads(capsys.readouterr().out)["quote"] == shown["sentences"][3]["text"]
    assert main(base + ["check", "--citation", citation, "--sentences", "1-2", "--inspect"]) == 0
    capsys.readouterr()
    assert main(base + ["check", "--citation", citation, "--sentences", "two"]) != 0
    with pytest.raises(SystemExit):
        main(base + ["check", "--citation", citation])
