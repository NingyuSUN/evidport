"""Local paper MCP boundaries use synthetic PDFs, not scientific validation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from evidence_database import literature, mcp_papers
from evidence_database.acquisition import AcquisitionQueue
from evidence_database.mcp_papers import PaperTools
from evidence_database.store import EvidenceError, Store

TITLE = "Synthetic evidence study of canine factor activity and expression"
DOI = "10.1234/local.mcp.synthetic"


def metadata(doi=DOI, title=TITLE):
    return {"doi": doi, "title": title, "document_id": "synthetic-" + doi.rsplit("/", 1)[-1],
            "provider": "synthetic", "record_id": doi, "oa_locations": [],
            "provenance": {"private_response": "NEVER_RETURN_PRIVATE_PROVIDER_RESPONSE"}}


def config_file(root, records=None):
    path = root / "project.json"
    path.write_text(json.dumps({"schema": 1, "project_id": "synthetic-mcp", "store": "store",
        "queue": "queue", "inbox": "inbox", "runs": "runs",
        "papers": [{"metadata": item} for item in (records or [metadata()])]}), encoding="utf8")
    (root / "inbox").mkdir(exist_ok=True)
    return path


def pdf(path, *, doi=DOI, title=TITLE, scanned=False):
    with pymupdf.open() as document:
        page = document.new_page()
        if scanned:
            page.draw_rect((40, 40, 250, 300), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
        else:
            page.insert_textbox((30, 30, 560, 780), title + "\nhttps://doi.org/" + doi
                                + "\nSynthetic evidence: factor activity is 1.25 mg.", fontsize=12)
        document.save(path)
    return path


def register(tools, doi=DOI):
    return tools.discover(dois=[doi])["papers"][0]["paper_id"]


def raw_queue(tools):
    return AcquisitionQueue(tools.config["queue"], Store(tools.config["store"]))


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def setup(tmp_path):
    path = config_file(tmp_path)
    return PaperTools(path), tmp_path


def test_status_and_pending_are_read_only_and_do_not_import_declared_files(tmp_path):
    path = config_file(tmp_path)
    original = pdf(tmp_path / "outside-inbox.pdf")
    config = json.loads(path.read_text())
    config["papers"][0]["file"] = str(original)
    path.write_text(json.dumps(config), encoding="utf8")
    before = snapshot(tmp_path)
    tools = PaperTools(path)
    result = tools.status()
    assert result["counts"] == {"candidates": 1, "available": 0, "searchable": 0, "pending": 1}
    assert tools.pending()["papers"][0]["status"] == "acquisition_required"
    assert tools.selected_versions() == {}
    assert snapshot(tmp_path) == before
    assert not (tmp_path / "store").exists()


def test_cached_metadata_discovery_never_calls_network_or_leaks_raw_records(setup, monkeypatch):
    tools, root = setup
    monkeypatch.setattr(literature, "resolve_doi", lambda *_: pytest.fail("unexpected network"))
    monkeypatch.setattr(literature, "search_europe_pmc", lambda *_a, **_k: pytest.fail("unexpected network"))
    result = tools.discover(query="canine factor")
    assert result["provider"] == "local_catalog" and result["network_used"] is False
    assert result["papers"][0]["doi"] == DOI
    assert "NEVER_RETURN" not in json.dumps(result)
    assert str(root) not in json.dumps(result)
    assert "no online query" in result["scope"]
    empty = tools.discover(query="not-in-local-catalog")
    assert empty["papers"] == [] and "do not mean no literature" in empty["scope"]


def test_doi_pending_registration_is_idempotent_and_never_fabricates_fulltext(setup):
    tools, _ = setup
    first = tools.discover(dois=["https://doi.org/" + DOI])
    state = first["papers"][0]
    counts = raw_queue(tools).audit()["counts"]
    assert tools.discover(dois=[DOI])["papers"][0] == state
    assert raw_queue(tools).audit()["counts"] == counts
    unknown = tools.discover(dois=["10.1234/unknown.synthetic"])["papers"][0]
    assert unknown["title"] == "" and unknown["status"] == "acquisition_required"
    assert unknown["free_pdf_candidates"] == 0
    assert all(key not in unknown for key in ["excerpt", "page", "citation"])
    assert unknown["conversion_id"] is None and unknown["source_id"] is None
    assert unknown["doi_url"] == "https://doi.org/10.1234/unknown.synthetic"


def test_manual_import_and_conversion_round_trip_retains_identity_and_does_not_review(setup):
    tools, root = setup
    paper_id = register(tools)
    original = pdf(root / "inbox" / "manual.pdf")
    imported = tools.import_file(paper_id, original.name)
    assert imported["operation_succeeded"] is True
    assert imported["status"] == "conversion_required"
    converted = tools.convert(paper_id)
    assert converted["fulltext_searchable"] is True
    assert converted["human_approved"] is False and converted["scientifically_verified"] is False
    store = Store(root / "store", read_only=True)
    citation = store.search("factor")[0]["citation"]
    assert not store.check(citation, "1.25 mg", require_review=True)["admitted"]
    assert tools.selected_versions() == {metadata()["document_id"]: converted["conversion_id"]}
    counts = raw_queue(tools).audit()["counts"]
    assert tools.import_file(paper_id, original.name)["operation"] == "reuse_original"
    assert tools.convert(paper_id)["operation"] == "reuse_conversion"
    assert raw_queue(tools).audit()["counts"] == counts
    origin = store.ingestion_receipts(imported["source_id"])[0]["metadata"]["acquisition"]
    assert origin["method"] == "manual_file" and origin["filename"] == original.name


def test_wrong_pdf_is_rejected_and_scanned_identity_remains_pending(setup):
    tools, root = setup
    paper_id = register(tools)
    pdf(root / "inbox" / "wrong.pdf", doi="10.1234/wrong")
    result = tools.import_file(paper_id, "wrong.pdf")
    assert not result["operation_succeeded"] and result["last_failure"] == "front_page_doi_mismatch"
    assert result["source_id"] is None and result["status"] == "acquisition_required"
    pdf(root / "inbox" / "scan.pdf", scanned=True)
    result = tools.import_file(paper_id, "scan.pdf")
    assert not result["operation_succeeded"] and result["status"] == "identity_review_required"
    with pytest.raises(EvidenceError, match="resolve file identity"):
        tools.convert(paper_id)
    assert tools.selected_versions() == {}


def test_bad_replacement_is_not_reported_as_success_when_prior_fulltext_exists(setup):
    tools, root = setup
    paper_id = register(tools)
    pdf(root / "inbox" / "good.pdf")
    tools.import_file(paper_id, "good.pdf")
    selected = tools.convert(paper_id)
    pdf(root / "inbox" / "wrong.pdf", doi="10.1234/other")
    result = tools.import_file(paper_id, "wrong.pdf")
    assert not result["operation_succeeded"]
    assert result["conversion_id"] == selected["conversion_id"]
    assert result["needs_attention"] is True
    assert tools.pending()["papers"][0]["last_failure"] == "front_page_doi_mismatch"
    assert tools.status()["counts"]["pending"] == 1


@pytest.mark.parametrize("filename", ["../outside.pdf", "nested/file.pdf", r"nested\file.pdf",
                                      r"C:\outside.pdf", "file.pdf:stream", "CON.pdf", "x.txt", "x.pdf\n"])
def test_import_rejects_paths_and_unsafe_filenames_without_creating_queue(setup, filename):
    tools, root = setup
    paper_id = tools.pending()["papers"][0]["paper_id"]
    with pytest.raises(EvidenceError, match="direct PDF filename"):
        tools.import_file(paper_id, filename)
    assert not (root / "queue").exists()


def test_inbox_reparse_point_is_rejected_before_open(setup, monkeypatch):
    tools, root = setup
    path = pdf(root / "inbox" / "link.pdf")
    original_lstat = Path.lstat

    def reparse(target):
        result = original_lstat(target)
        if target == path:
            return SimpleNamespace(st_file_attributes=0x400, st_mode=result.st_mode)
        return result

    monkeypatch.setattr(Path, "lstat", reparse)
    monkeypatch.setattr(os, "open", lambda *_a, **_k: pytest.fail("must not open reparse input"))
    with pytest.raises(EvidenceError, match="link or reparse"):
        tools.import_file("paper_" + "0" * 64, path.name)


def test_file_swap_during_open_is_rejected_and_descriptor_closed(setup, monkeypatch):
    tools, root = setup
    inside = pdf(root / "inbox" / "file.pdf")
    outside = pdf(root / "outside.pdf", doi="10.1234/outside")
    original_open, descriptors = os.open, []

    def swapped(_path, flags):
        descriptor = original_open(outside, flags)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", swapped)
    with pytest.raises(EvidenceError, match="changed while opening"):
        tools.import_file("paper_" + "0" * 64, inside.name)
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert not (root / "queue").exists()


def test_import_size_limit_is_checked_before_intake(setup, monkeypatch):
    tools, root = setup
    path = pdf(root / "inbox" / "large.pdf")
    monkeypatch.setattr(mcp_papers, "MAX_FILE_BYTES", 100)
    with pytest.raises(EvidenceError, match="50 MiB"):
        tools.import_file("paper_" + "0" * 64, path.name)
    assert not (root / "queue").exists()


def test_network_is_disabled_by_default_and_no_free_location_is_not_called_paywall(setup, monkeypatch):
    tools, _ = setup
    paper_id = register(tools)
    monkeypatch.setattr(literature, "resolve_doi", lambda *_: metadata())
    with pytest.raises(EvidenceError, match="Network is disabled"):
        tools.acquire(paper_id)
    online = PaperTools(tools.config["config_path"], allow_network=True)
    result = online.acquire(paper_id)
    assert result["status"] == "acquisition_required"
    assert result["last_failure"] == "no_verified_free_pdf_location_found"
    assert not result["operation_succeeded"]
    assert "does not establish" in online.pending()["access_note"]


def test_provider_discovery_retains_scope_and_errors_without_raw_response(setup, monkeypatch):
    tools, _ = setup
    online = PaperTools(tools.config["config_path"], allow_network=True)
    record = metadata("10.1234/discovered")
    monkeypatch.setattr(literature, "search_crossref", lambda query, max_results: {
        "records": [record], "provenance": {"provider": "crossref", "query": query,
        "total_hits": 99, "truncated": True, "errors": [{"type": "pagination_incomplete"}],
        "requests": [{"raw_response": "PRIVATE_PROVIDER_BODY"}]}})
    result = online.discover(query="canine", provider="crossref", limit=1)
    assert result["network_used"] and result["total_hits"] == 99 and result["truncated"]
    assert result["provider_errors"] == ["pagination_incomplete"]
    assert "PRIVATE_PROVIDER_BODY" not in json.dumps(result)
    assert result["papers"][0]["status"] == "acquisition_required"


def test_free_download_uses_existing_identity_checks(setup, monkeypatch):
    tools, root = setup
    online = PaperTools(tools.config["config_path"], allow_network=True)
    record = metadata()
    record["oa_locations"] = [{"url": "https://example.org/synthetic.pdf", "eligibility": "free_access"}]
    queue = AcquisitionQueue(root / "queue", Store(root / "store"))
    paper_id = queue.register([record], provenance={"synthetic": True})["papers"][0]["paper_id"]
    content = pdf(root / "download.pdf").read_bytes()
    monkeypatch.setattr(literature, "download_pdf", lambda _url: {"data": content, "status_code": 200})
    result = online.acquire(paper_id)
    assert result["operation_succeeded"] and result["status"] == "conversion_required"
    assert not result["human_approved"]
    assert online.acquire(paper_id)["network_used"] is False


def test_pending_pagination_is_bounded_and_rejects_stale_or_forged_cursor(tmp_path):
    records = [metadata("10.1234/" + str(index)) for index in range(3)]
    tools = PaperTools(config_file(tmp_path, records))
    first = tools.pending(limit=1)
    second = tools.pending(limit=1, cursor=first["next_cursor"])
    assert first["total"] == 3 and first["papers"][0]["paper_id"] != second["papers"][0]["paper_id"]
    tools.discover(dois=["10.1234/new"])
    with pytest.raises(EvidenceError, match="stale pending cursor"):
        tools.pending(cursor=first["next_cursor"])
    with pytest.raises(EvidenceError, match="Invalid or stale"):
        tools.pending(cursor="{not-a-cursor}")


def test_selected_versions_reuses_explicit_pin_and_rejects_wrong_source(setup):
    tools, root = setup
    paper_id = register(tools)
    pdf(root / "inbox" / "good.pdf")
    tools.import_file(paper_id, "good.pdf")
    selected = tools.convert(paper_id)
    config = json.loads(tools.config["config_path"].read_text())
    config["papers"][0]["conversion_id"] = selected["conversion_id"]
    tools.config["config_path"].write_text(json.dumps(config), encoding="utf8")
    pinned = PaperTools(tools.config["config_path"])
    assert pinned.convert(paper_id)["operation"] == "reuse_pinned_conversion"
    assert pinned.selected_versions() == {metadata()["document_id"]: selected["conversion_id"]}
    store = Store(root / "store")
    other = pdf(root / "different.pdf", doi="10.1234/elsewhere")
    source = store.ingest(other, "different", role="paper")
    conversion = store.add_conversion(source["source_id"], parser="synthetic", parser_version="1",
                                     blocks=[{"id": "b1", "kind": "text", "text": "Synthetic other text",
                                              "locator": {"page": 1}}])
    config["papers"][0]["conversion_id"] = conversion["conversion_id"]
    tools.config["config_path"].write_text(json.dumps(config), encoding="utf8")
    with pytest.raises(EvidenceError, match="belongs to another original"):
        PaperTools(tools.config["config_path"]).selected_versions()


def test_selected_versions_reads_metadata_without_rehashing_pdf_or_parser_artifacts(setup, monkeypatch):
    tools, root = setup
    paper_id = register(tools)
    pdf(root / "inbox" / "good.pdf")
    tools.import_file(paper_id, "good.pdf")
    selected = tools.convert(paper_id)
    with monkeypatch.context() as patch:
        patch.setattr(AcquisitionQueue, "list", lambda *_: pytest.fail("must not replay full source reads"))
        patch.setattr(Store, "_blob", lambda *_: pytest.fail("selection must not read stored artifact bytes"))
        assert tools.selected_versions() == {metadata()["document_id"]: selected["conversion_id"]}


@pytest.mark.parametrize("damage", ["record", "index", "event"])
def test_selected_versions_still_rejects_changed_immutable_metadata(setup, damage):
    tools, root = setup
    paper_id = register(tools)
    pdf(root / "inbox" / "good.pdf")
    tools.import_file(paper_id, "good.pdf")
    selected = tools.convert(paper_id)
    store = Store(root / "store")
    if damage == "record":
        path = store._record_path("conversions", selected["conversion_id"])
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "index":
        with store._db() as database:
            database.execute("DELETE FROM conversions WHERE conversion_id=?", (selected["conversion_id"],))
    else:
        path = next((root / "queue" / "events").glob("*.json"))
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(EvidenceError, match="integrity|membership"):
        tools.selected_versions()


def test_metadata_selection_does_not_allow_corrupted_native_artifact_to_be_returned(setup):
    tools, root = setup
    paper_id = register(tools)
    pdf(root / "inbox" / "good.pdf")
    imported = tools.import_file(paper_id, "good.pdf")
    native = root / "native.json"
    native.write_text('{"synthetic": true}', encoding="utf8")
    queue = raw_queue(tools)
    conversion = queue.store.add_conversion(imported["source_id"], parser="synthetic", parser_version="1",
        artifacts={"native.json": native}, blocks=[
            {"id": "b1", "kind": "text", "text": "Synthetic factor activity", "locator": {"page": 1}}])
    queue.bind_conversion(paper_id, conversion["conversion_id"])
    Path(conversion["artifacts"]["native.json"]["path"]).write_bytes(b"changed native artifact")
    assert tools.selected_versions() == {metadata()["document_id"]: conversion["conversion_id"]}
    with pytest.raises(EvidenceError, match="integrity"):
        queue.store.get(conversion["citations"][0])


@pytest.mark.parametrize("limit", [0, 21, True, 1.5])
def test_output_limit_rejects_unbounded_requests(setup, limit):
    tools, _ = setup
    with pytest.raises(EvidenceError, match="integer from 1 to 20"):
        tools.discover(limit=limit)
    with pytest.raises(EvidenceError, match="integer from 1 to 20"):
        tools.pending(limit=limit)


@pytest.mark.parametrize("parse_status", ["failed", "partial"])
def test_failed_and_partial_conversions_remain_pending_with_correct_selection(setup, monkeypatch, parse_status):
    from evidence_database import adapters
    tools, root = setup
    paper_id = register(tools)
    pdf(root / "inbox" / "paper.pdf")
    tools.import_file(paper_id, "paper.pdf")

    def parsed(store, source_id):
        blocks = [] if parse_status == "failed" else [
            {"id": "b1", "kind": "text", "text": "Synthetic partial text", "locator": {"page": 1}}]
        return store.add_conversion(source_id, parser="synthetic-status", parser_version="1", blocks=blocks,
                                    status=parse_status, issues=["synthetic conversion uncertainty"])

    monkeypatch.setattr(adapters, "run_baseline", parsed)
    result = tools.convert(paper_id)
    assert result["needs_attention"] and tools.status()["counts"]["pending"] == 1
    assert tools.pending()["total"] == 1
    if parse_status == "failed":
        assert not result["operation_succeeded"] and tools.selected_versions() == {}
        assert result["next_action"] == "inspect_acquisition_failure"
    else:
        assert result["operation_succeeded"]
        assert tools.selected_versions() == {metadata()["document_id"]: result["conversion_id"]}
        assert result["next_action"] == "inspect_partial_conversion"


@pytest.mark.parametrize("managed_root", ["store", "queue"])
def test_missing_existing_index_is_an_error_not_an_empty_catalog(setup, managed_root):
    tools, root = setup
    (root / managed_root).mkdir()
    (root / managed_root / "historical-record.json").write_text("{}", encoding="utf8")
    before = snapshot(root)
    with pytest.raises(EvidenceError, match="incomplete"):
        tools.status()
    with pytest.raises(EvidenceError, match="incomplete"):
        tools.discover(dois=[DOI])
    assert snapshot(root) == before
