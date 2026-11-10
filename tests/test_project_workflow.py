import hashlib
import json
from pathlib import Path

import pymupdf
import pytest

from evidence_database.acquisition import AcquisitionQueue
from evidence_database.adapters import run_baseline
from evidence_database.cli import main
from evidence_database.store import EvidenceError, Store
from evidence_database.workflow import project_status, sync_project

TITLE = "Synthetic project workflow verifies concentration measurements in marker samples"
DOI = "10.5555/project-workflow"


def pdf(path, title=TITLE, doi=DOI):
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_textbox((30, 30, 550, 300), title + "\nDOI: " + doi + "\n" +
                            "The synthetic measured concentration was 17.25 units per litre. " * 3)
        doc.save(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def config(tmp_path, *, file=False, **extra):
    item = {"metadata": {"doi": DOI, "title": TITLE, "document_id": "synthetic-paper"}}
    if file:
        item.update(file="paper.pdf", sha256=pdf(tmp_path / "paper.pdf"))
    item.update(extra)
    value = {"schema": 1, "project_id": "synthetic-project", "store": "store", "queue": "queue",
             "inbox": "inbox", "runs": "runs", "papers": [item]}
    path = tmp_path / "project.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, value


def test_sync_local_file_then_repeat_preserves_versions(tmp_path):
    path, _ = config(tmp_path, file=True)
    first = sync_project(path)
    assert first["counts"]["searchable"] == 1 and not first["needs_attention"]
    store = Store(tmp_path / "store")
    before = store.audit()["counts"]
    second = sync_project(path)
    assert second["papers"][0]["conversion_id"] == first["papers"][0]["conversion_id"]
    assert store.audit()["counts"] == before
    assert first["run_id"] != second["run_id"]
    assert store.audit()["counts"]["reviews"] == 0


def test_metadata_only_then_drop_into_inbox(tmp_path):
    path, _ = config(tmp_path)
    first = sync_project(path)
    assert first["needs_attention"] and first["counts"]["available"] == 0
    assert first["pending"]["need_pdf"] == 1
    state = first["papers"][0]
    pdf(tmp_path / "inbox" / (state["paper_id"] + ".pdf"))
    result = sync_project(path)
    assert result["counts"]["searchable"] == 1 and result["pending"]["count"] == 0


def test_explicit_conversion_reused_and_wrong_source_rejected(tmp_path):
    path, value = config(tmp_path, file=True)
    store = Store(tmp_path / "store")
    source = store.ingest(tmp_path / "paper.pdf", "synthetic-paper", role="paper")
    selected = run_baseline(store, source["source_id"])
    value["papers"][0]["conversion_id"] = selected["conversion_id"]
    path.write_text(json.dumps(value), encoding="utf-8")
    result = sync_project(path)
    assert result["papers"][0]["conversion_id"] == selected["conversion_id"]
    assert len(store.conversions()) == 1
    pdf(tmp_path / "other.pdf", "Another synthetic long title demonstrating mismatched source identity checks", "10.5555/other")
    other = store.ingest(tmp_path / "other.pdf", "other")
    wrong = run_baseline(store, other["source_id"])
    queue = AcquisitionQueue(tmp_path / "queue", store)
    with pytest.raises(EvidenceError, match="another original"):
        queue.bind_conversion(result["papers"][0]["paper_id"], wrong["conversion_id"])
    assert queue.current(result["papers"][0]["paper_id"])["conversion_id"] == selected["conversion_id"]


def test_pinned_bytes_mismatch_not_ingested(tmp_path):
    path, value = config(tmp_path, file=True)
    value["papers"][0]["sha256"] = "a" * 64
    path.write_text(json.dumps(value), encoding="utf-8")
    result = sync_project(path)
    assert result["errors"] and result["needs_attention"]
    assert Store(tmp_path / "store").sources() == []


def test_fetch_is_explicit_and_scoped_pending_excludes_other_projects(tmp_path, monkeypatch):
    from evidence_database import literature
    path, _ = config(tmp_path)
    calls = []
    monkeypatch.setattr(literature, "resolve_doi", lambda doi: calls.append(doi) or {"doi": doi, "title": TITLE})
    sync_project(path)
    assert calls == []
    queue = AcquisitionQueue(tmp_path / "queue", Store(tmp_path / "store"))
    queue.register([{"doi": "10.5555/unrelated", "title": "Unrelated project paper"}], provenance={})
    result = sync_project(path, fetch=True)
    assert calls == [DOI] and result["pending"]["count"] == 1
    pending = json.loads((Path(result["pending"]["directory"]) / "pending.json").read_text())
    assert pending[0]["doi"] == DOI


def test_unknown_inbox_is_not_modified(tmp_path):
    path, _ = config(tmp_path)
    (tmp_path / "inbox").mkdir()
    unknown = tmp_path / "inbox" / "unmatched.pdf"
    unknown.write_bytes(b"not PDF")
    result = sync_project(path)
    assert result["unknown_inbox_files_untouched"] == ["unmatched.pdf"]
    assert unknown.read_bytes() == b"not PDF"


def test_status_before_sync_has_no_filesystem_side_effects(tmp_path, capsys, monkeypatch):
    path, _ = config(tmp_path)
    before = set(tmp_path.rglob("*"))
    monkeypatch.chdir(tmp_path)
    assert main(["project", "--config", str(path), "status"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["papers"][0]["availability"] == "not_registered"
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("kind", ["overlap", "duplicate", "unknown_field", "bad_sha"])
def test_invalid_config_fails_before_creating_managed_directories(tmp_path, kind):
    path, value = config(tmp_path)
    if kind == "overlap":
        value["inbox"] = "store/inbox"
    elif kind == "duplicate":
        value["papers"].append(value["papers"][0])
    elif kind == "unknown_field":
        value["command"] = "arbitrary executable"
    else:
        value["papers"][0]["sha256"] = "a" * 64
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(EvidenceError):
        sync_project(path)
    assert not (tmp_path / "store").exists() and not (tmp_path / "runs").exists()


def test_status_after_sync_reports_only_configured_papers(tmp_path):
    path, _ = config(tmp_path, file=True)
    expected = sync_project(path)
    result = project_status(path)
    assert result["papers"] == expected["papers"]


def test_status_cannot_repair_missing_queue_directory(tmp_path):
    path, _ = config(tmp_path)
    sync_project(path)
    (tmp_path / "queue" / "objects").rmdir()
    with pytest.raises(EvidenceError, match="existing complete"):
        project_status(path)
    assert not (tmp_path / "queue" / "objects").exists()


def test_readonly_queue_rejects_writes_before_side_effects(tmp_path):
    path, _ = config(tmp_path)
    sync_project(path)
    queue = AcquisitionQueue(tmp_path / "queue", Store(tmp_path / "store", read_only=True), read_only=True)
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(EvidenceError, match="read-only"):
        queue.register([{"doi": "10.5555/extra"}], provenance={})
    with pytest.raises(EvidenceError, match="read-only"):
        queue.fetch(queue.list()[0]["paper_id"])
    with pytest.raises(EvidenceError, match="read-only"):
        queue.export_pending(tmp_path / "new-pending")
    after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_repeat_sync_preserves_failed_reparse_until_explicit_resolution(tmp_path, monkeypatch):
    from evidence_database import adapters
    path, value = config(tmp_path, file=True)
    initial = sync_project(path)["papers"][0]
    store = Store(tmp_path / "store")
    queue = AcquisitionQueue(tmp_path / "queue", store)
    monkeypatch.setattr(adapters, "run_baseline", lambda store, source: store.add_conversion(
        source, parser="synthetic-failed-reparse", parser_version="1", blocks=[], status="failed"))
    failed = queue.convert(initial["paper_id"])
    assert failed["last_failure"] == "conversion_failed" and failed["fulltext_searchable"]
    result = sync_project(path)
    assert result["needs_attention"] and result["papers"][0]["last_failure"] == "conversion_failed"
    assert result["papers"][0]["conversion_id"] == initial["conversion_id"]
    value["papers"][0]["conversion_id"] = initial["conversion_id"]
    path.write_text(json.dumps(value), encoding="utf-8")
    resolved = sync_project(path)
    assert not resolved["needs_attention"] and not resolved["papers"][0]["last_failure"]
