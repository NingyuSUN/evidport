"""CLI lifecycle: metadata-only discovery, pending export, then local PDF intake."""
import json

import pymupdf

from evidence_database.cli import main
from evidence_database.store import Store


def invoke(capsys, root, *args):
    code = main(["--db", str(root / "store"), "papers", "--queue", str(root / "queue"), *args])
    return code, json.loads(capsys.readouterr().out)


def make_pdf(path, title, doi):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_textbox((30, 30, 560, 300), title + "\nDOI: " + doi + "\n" +
                            "This synthetic fixture tests file acquisition and makes no scientific claims. " * 3)
        doc.save(path)


def test_metadata_pending_then_inbox_conversion(tmp_path, capsys):
    title = "Synthetic evidence acquisition experiment measuring marker concentration"
    doi = "10.5555/synthetic-intake"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps([{"doi": doi, "title": title}]), encoding="utf-8")
    code, result = invoke(capsys, tmp_path, "register", "--manifest", str(manifest))
    assert code == 0
    identifier = result["papers"][0]["paper_id"]
    store = Store(tmp_path / "store")
    assert store.sources() == [] and store.search("marker") == []
    code, result = invoke(capsys, tmp_path, "pending", "--out", str(tmp_path / "tasks"))
    assert code == 0
    exported = "\n".join(p.read_text(encoding="utf-8-sig") for p in (tmp_path / "tasks").glob("*.tsv"))
    assert doi in exported and identifier in exported
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    original = inbox / (identifier + ".pdf")
    make_pdf(original, title, doi)
    before = original.read_bytes()
    code, result = invoke(capsys, tmp_path, "import-inbox", "--inbox", str(inbox), "--parse")
    assert code == 0 and result["papers"][0]["fulltext_searchable"]
    assert original.read_bytes() == before
    hits = store.search("marker concentration")
    assert hits and not store.check(hits[0]["citation"], "marker concentration")["admitted"]
    code, result = invoke(capsys, tmp_path, "audit")
    assert code == 0 and result["ok"]


def test_discovery_failure_saved_and_no_fulltext_created(tmp_path, capsys, monkeypatch):
    from evidence_database import literature

    def fail(*args, **kwargs):
        raise ValueError("synthetic provider timeout")
    monkeypatch.setattr(literature, "search_europe_pmc", fail)
    code, result = invoke(capsys, tmp_path, "discover", "--query", "synthetic discovery", "--provider", "europepmc")
    assert code == 2 and result["errors"]
    assert result["fulltext_search_performed"] is False
    assert list((tmp_path / "queue" / "batches").glob("*.json"))
    assert Store(tmp_path / "store").sources() == []


def test_unknown_inbox_file_is_retained_without_guessing_identity(tmp_path, capsys):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    original = inbox / "unknown.pdf"
    original.write_bytes(b"%PDF-fake")
    code, result = invoke(capsys, tmp_path, "import-inbox", "--inbox", str(inbox))
    assert code == 2 and result["unknown_files_untouched"] == ["unknown.pdf"]
    assert original.read_bytes() == b"%PDF-fake"
    assert Store(tmp_path / "store").sources() == []


def test_current_intake_failure_does_not_reparse_old_success(tmp_path, capsys):
    from evidence_database.acquisition import AcquisitionQueue

    store = Store(tmp_path / "store")
    queue = AcquisitionQueue(tmp_path / "queue", store)
    title, doi = "Synthetic experiment to verify retained successful document states", "10.5555/retain"
    state = queue.register([{"title": title, "doi": doi}], provenance={})["papers"][0]
    pdf = tmp_path / "good.pdf"
    make_pdf(pdf, title, doi)
    queue.attach(state["paper_id"], pdf)
    success = queue.convert(state["paper_id"])
    conversions = len(store.conversions())
    html = tmp_path / "bad.pdf"
    html.write_bytes(b"<html>Access denied</html>")
    code, result = invoke(capsys, tmp_path, "import-pdf", "--paper", state["paper_id"], "--pdf", str(html), "--parse")
    assert code == 2 and result["last_failure"]
    assert result["source_id"] == success["source_id"] and result["fulltext_searchable"]
    assert len(store.conversions()) == conversions


def test_provider_partial_failures_are_reported(tmp_path, capsys, monkeypatch):
    from evidence_database import literature

    monkeypatch.setattr(literature, "search_europe_pmc", lambda *a, **k: {
        "records": [], "provenance": {"errors": [{"message": "second page failed"}]}})
    code, result = invoke(capsys, tmp_path, "discover", "--query", "fixture")
    assert code == 2 and result["errors"]
    monkeypatch.setattr(literature, "resolve_doi", lambda doi: {
        "doi": doi, "title": None, "metadata_status": "unresolved", "errors": [{"message": "outage"}]})
    code, result = invoke(capsys, tmp_path, "resolve", "--doi", "10.5555/missing")
    assert code == 2 and result["errors"] and result["papers"][0]["availability"] == "metadata_only"
