"""Synthetic queue lifecycle tests; no network calls or real human approvals."""
import copy
import csv
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pymupdf
import pytest

from evidence_database import literature
from evidence_database.acquisition import AcquisitionQueue
from evidence_database.store import EvidenceError, Store

TITLE = "Evidence acquisition synthetic study of canine factor activity"
DOI = "10.1234/synthetic.test"


def make_pdf(path, *, title=TITLE, doi=DOI, extra="Synthetic experiment dose 1.25 mg", supplement=False,
             scanned=False, second_page=None):
    with pymupdf.open() as doc:
        page = doc.new_page()
        if not scanned:
            text = ("Supplementary Materials for\n" if supplement else "") + title
            text += "\n" + ("https://doi.org/" + doi if doi else "") + "\n" + extra
            page.insert_textbox((30, 30, 560, 780), text, fontsize=12)
        else:
            page.draw_rect((50, 50, 500, 500), color=(0, 0, 0), fill=(0.8, 0.8, 0.8))
        if second_page:
            page = doc.new_page()
            page.insert_textbox((30, 30, 560, 780), second_page, fontsize=12)
        doc.save(path)
    return path


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "store")
    queue = AcquisitionQueue(tmp_path / "queue", store)
    record = {"doi": DOI, "title": TITLE, "pmid": "12345678", "document_id": "PMID12345678",
              "provider": "synthetic", "record_id": "explicit-fixture", "oa_locations": []}
    result = queue.register([record], provenance={"scope": "synthetic fixture, not real literature"})
    paper_id = result["papers"][0]["paper_id"]
    return queue, store, record, paper_id, tmp_path


def track_connections(monkeypatch, *, fail_sql=None):
    actual_connect = sqlite3.connect
    opened = []

    class TrackedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=(), /):
            result = super().execute(sql, parameters)
            if fail_sql and sql.startswith(fail_sql):
                raise RuntimeError("synthetic failure after SQL execution")
            return result

    def connect(*args, **kwargs):
        connection = actual_connect(*args, **kwargs, factory=TrackedConnection)
        statements = []
        connection.set_trace_callback(statements.append)
        # Keep actual connections alive: garbage collection must not hide missing close().
        opened.append((connection, statements))
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    return opened


def assert_connections_closed(opened):
    assert opened
    for connection, _ in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("operation", ["initialize", "read", "register", "append", "read_only"])
def test_queue_closes_connections_and_preserves_successful_operations(setup, monkeypatch, operation):
    queue, store, record, paper_id, tmp_path = setup
    state = queue.current(paper_id)
    opened = track_connections(monkeypatch)
    expected_batches, expected_events = 1, 1
    if operation == "initialize":
        queue = AcquisitionQueue(tmp_path / "new-queue", store)
        expected_batches, expected_events = 0, 0
    elif operation == "read":
        assert queue.current(paper_id) == state
    elif operation == "register":
        queue.register([record], provenance={"scope": "second synthetic batch"})
        expected_batches, expected_events = 2, 2
    elif operation == "append":
        queue._append(state, "synthetic-event", {})
        expected_events = 2
    else:
        queue = AcquisitionQueue(queue.root, store, read_only=True)
        assert queue.current(paper_id) == state
    report = queue.audit()
    assert report["ok"]
    assert report["counts"]["batches"] == expected_batches
    assert report["counts"]["events"] == expected_events
    if operation in {"register", "append"}:
        assert any("COMMIT" in statements for _, statements in opened)
    assert_connections_closed(opened)


@pytest.mark.parametrize(("operation", "fail_sql"), [
    ("initialize", "CREATE TABLE IF NOT EXISTS events"),
    ("read", "SELECT id,body FROM events"),
    ("register", "INSERT OR IGNORE INTO batches"),
    ("append", "INSERT INTO events"),
])
def test_queue_closes_connections_after_errors_and_rolls_back_writes(setup, monkeypatch, operation, fail_sql):
    queue, store, record, paper_id, tmp_path = setup
    state = queue.current(paper_id)
    actual_connect = sqlite3.connect
    with closing(actual_connect(queue.db_path)) as db:
        before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in ("batches", "events")}
    opened = track_connections(monkeypatch, fail_sql=fail_sql)
    with pytest.raises(RuntimeError, match="synthetic failure after SQL execution"):
        if operation == "initialize":
            AcquisitionQueue(tmp_path / "new-queue", store)
        elif operation == "read":
            queue.current(paper_id)
        elif operation == "register":
            queue.register([record], provenance={"scope": "synthetic failed batch"})
        else:
            queue._append(state, "synthetic-failed-event", {})
    assert_connections_closed(opened)
    with closing(actual_connect(queue.db_path)) as db:
        after = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in ("batches", "events")}
    assert after == before
    if operation in {"register", "append"}:
        # Failure is injected after the actual INSERT, so rollback must undo a real write.
        assert any("ROLLBACK" in statements for _, statements in opened)
        assert not any("COMMIT" in statements for _, statements in opened)


def test_metadata_only_never_becomes_fulltext(setup):
    queue, store, _, paper_id, _ = setup
    state = queue.current(paper_id)
    assert state["availability"] == "metadata_only"
    assert state["source_id"] is None and state["fulltext_searchable"] is False
    assert store.sources() == [] and store.search("canine") == []
    with pytest.raises(EvidenceError, match="acquire the correct PDF"):
        queue.convert(paper_id)
    assert queue.audit()["ok"]


def test_empty_discovery_receipt_is_preserved(setup):
    queue, _, _, _, _ = setup
    result = queue.register([], provenance={"query": "no results", "total_hits": 0, "truncated": False})
    assert result["count"] == 0
    assert queue.audit()["counts"]["batches"] == 2
    saved = json.loads((queue.root / "batches" / (result["batch_id"] + ".json")).read_text(encoding="utf-8"))
    assert saved["provenance"]["query"] == "no results"


def test_normalized_identifier_dedup_preserves_original_metadata_and_new_provenance(setup):
    queue, _, record, paper_id, _ = setup
    newer = {**record, "doi": "https://doi.org/10.1234/SYNTHETIC.TEST", "provider": "other", "record_id": "2"}
    duplicate = queue.register([newer], provenance={"query": "second discovery"})
    assert duplicate["papers"][0]["paper_id"] == paper_id
    assert len(queue.list()) == 1
    assert queue.audit()["counts"]["events"] == 2


def test_pmid_record_can_gain_doi_without_changing_work_identity(setup):
    queue, _, _, _, _ = setup
    pmid_only = {"pmid": "33333333", "title": TITLE}
    one = queue.register([pmid_only], provenance={})["papers"][0]
    two = queue.register([{**pmid_only, "doi": "10.1234/pmid.enriched"}], provenance={})["papers"][0]
    assert one["paper_id"] == two["paper_id"]
    assert two["metadata"]["doi"] == "10.1234/pmid.enriched"


def test_doi_enrichment_cannot_invalidate_already_verified_pdf(setup):
    queue, store, _, _, tmp_path = setup
    paper = queue.register([{"pmid": "999999", "title": TITLE}], provenance={})["papers"][0]
    old = queue.attach(paper["paper_id"], make_pdf(tmp_path / "original.pdf"))
    with pytest.raises(EvidenceError, match="conflicts with verified PDF"):
        queue.register([{"pmid": "999999", "title": TITLE, "doi": "10.1234/incorrect"}], provenance={})
    assert "doi" not in queue.current(paper["paper_id"])["metadata"]
    assert len(store.sources()) == 1 and queue.current(paper["paper_id"])["source_id"] == old["source_id"]


def test_conflicting_doi_alias_is_not_merged(setup):
    queue, _, record, _, _ = setup
    with pytest.raises(EvidenceError, match="Conflicting doi"):
        queue.register([{**record, "doi": "10.1234/wrong"}], provenance={})
    assert len(queue.list()) == 1


def test_supplement_is_distinct_and_requires_attachment_identifier(setup):
    queue, _, record, paper_id, _ = setup
    with pytest.raises(EvidenceError, match="attachment_id"):
        queue.register([{**record, "role": "supplement"}], provenance={})
    other = queue.register([{**record, "role": "supplement", "attachment_id": "S1"}], provenance={})["papers"][0]
    assert other["paper_id"] != paper_id
    assert len(queue.list()) == 2


def test_no_doi_metadata_export_does_not_invent_identifier(setup):
    queue, _, _, _, tmp_path = setup
    paper = queue.register([{"title": TITLE, "provider": "index", "record_id": "no-doi"}], provenance={})["papers"][0]
    result = queue.export_pending(tmp_path / "export")
    rows = json.loads((Path(result["directory"]) / "pending.json").read_text(encoding="utf-8"))
    row = next(x for x in rows if x["paper_id"] == paper["paper_id"])
    assert row["doi"] == "" and row["doi_url"] == "" and row["record_id"] == "no-doi"


def test_no_free_location_creates_manual_download_task(setup, monkeypatch):
    queue, _, record, paper_id, tmp_path = setup
    monkeypatch.setattr(literature, "resolve_doi", lambda doi: record)
    state = queue.fetch(paper_id)
    assert state["availability"] == "waiting_user"
    assert state["last_failure"] == "no_verified_free_pdf_location_found"
    report = queue.export_pending(tmp_path / "pending")
    assert report["need_pdf"] == 1 and report["verify_downloaded_file"] == 0
    with (tmp_path / "pending/pending.csv").open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["doi_url"] == "https://doi.org/" + DOI
    assert rows[0]["suggested_filename"] == paper_id + ".pdf"
    with pytest.raises(EvidenceError, match="new directory"):
        queue.export_pending(tmp_path / "pending")


def test_provider_outage_is_recorded_without_paywall_claim(setup, monkeypatch):
    queue, _, _, paper_id, _ = setup
    def fail(doi):
        raise ValueError("HTTP 429 metadata provider unavailable")
    monkeypatch.setattr(literature, "resolve_doi", fail)
    state = queue.fetch(paper_id)
    assert state["availability"] == "waiting_user"
    assert any("HTTP 429" in error["reason"] for error in state["failures"])
    assert "paywall" not in state["last_failure"]


def test_free_download_then_explicit_conversion_and_unreviewed_gate(setup, monkeypatch):
    queue, store, record, paper_id, tmp_path = setup
    pdf = make_pdf(tmp_path / "public.pdf")
    resolved = {**record, "oa_locations": [{"url": "https://example.org/paper.pdf", "provider": "fixture",
                                           "eligibility": "free_access", "license": None}]}
    monkeypatch.setattr(literature, "resolve_doi", lambda doi: resolved)
    monkeypatch.setattr(literature, "download_pdf", lambda url: {"data": pdf.read_bytes(), "requested_url": url,
                                                               "final_url": url, "content_type": "application/pdf",
                                                               "status_code": 200})
    state = queue.fetch(paper_id)
    assert state["availability"] == "available" and state["identity_status"] == "verified"
    assert state["parsing_status"] == "not_started" and not state["fulltext_searchable"]
    assert store.source(state["source_id"])["document_id"] == "PMID12345678"
    state = queue.convert(paper_id)
    assert state["fulltext_searchable"] and state["parsing_status"] == "completed"
    hit = store.search("dose 1.25 mg")[0]
    assert store.check(hit["citation"], "dose 1.25 mg")["quote_matches"]
    assert not store.check(hit["citation"], "dose 1.25 mg")["admitted"]
    assert queue.audit()["ok"]


def test_unqualified_url_is_never_downloaded(setup, monkeypatch):
    queue, _, record, paper_id, _ = setup
    queue.register([{**record, "oa_locations": [{"url": "https://example.org/paywall.pdf"}]}], provenance={})
    monkeypatch.setattr(literature, "download_pdf", lambda url: pytest.fail("Unverified URL must not be fetched"))
    assert queue.fetch(paper_id)["availability"] == "waiting_user"


def test_bad_download_attempt_is_retained_and_next_free_location_can_succeed(setup, monkeypatch):
    queue, store, record, paper_id, tmp_path = setup
    pdf = make_pdf(tmp_path / "public.pdf")
    queue.register([{**record, "oa_locations": [
        {"url": "https://example.org/bad", "eligibility": "open_access"},
        {"url": "https://example.org/good", "eligibility": "open_access"}]}], provenance={})
    def download(url):
        if url.endswith("bad"):
            raise ValueError("HTTP 403")
        return {"data": pdf.read_bytes(), "final_url": url, "status_code": 200}
    monkeypatch.setattr(literature, "download_pdf", download)
    state = queue.fetch(paper_id)
    assert state["availability"] == "available" and len(store.sources()) == 1
    assert any("403" in f["reason"] for f in state["failures"])


def test_html_file_cannot_be_attached_as_pdf(setup):
    queue, store, _, paper_id, tmp_path = setup
    path = tmp_path / "looks-like-paper.pdf"
    path.write_bytes(b"<!doctype html><title>Subscribe to read</title>")
    state = queue.attach(paper_id, path)
    assert state["availability"] == "waiting_user" and store.sources() == []
    assert state["last_failure"] == "invalid_pdf_header"
    assert path.exists()


def test_wrong_paper_even_with_expected_doi_in_references_is_not_admitted(setup):
    queue, store, _, paper_id, tmp_path = setup
    path = make_pdf(tmp_path / "wrong.pdf", title="A distinct unrelated synthetic paper about completely different data",
                    doi="10.1234/wrong", second_page="References\n" + TITLE + "\n" + DOI)
    state = queue.attach(paper_id, path)
    assert state["availability"] != "available" and not store.sources()
    assert state["last_failure"] == "front_page_doi_mismatch"


def test_expected_doi_in_first_page_bibliography_does_not_identify_wrong_title(setup):
    queue, store, _, paper_id, tmp_path = setup
    path = make_pdf(tmp_path / "wrong-reference.pdf", title="Entirely different synthetic paper and unrelated scientific findings",
                    doi=None, extra="References\n" + TITLE + "\n" + DOI)
    state = queue.attach(paper_id, path)
    assert state["availability"] == "identity_review_required" and not store.sources()


def test_supplement_cannot_impersonate_primary_even_with_same_title_and_doi(setup):
    queue, store, _, paper_id, tmp_path = setup
    state = queue.attach(paper_id, make_pdf(tmp_path / "main.pdf", supplement=True))
    assert state["availability"] == "waiting_user" and not store.sources()
    assert state["last_failure"] == "supplement_cannot_replace_primary_paper"


def test_scanned_pdf_is_retained_for_identity_check_not_silently_converted(setup):
    queue, store, _, paper_id, tmp_path = setup
    state = queue.attach(paper_id, make_pdf(tmp_path / "scan.pdf", scanned=True))
    assert state["availability"] == "identity_review_required" and not store.sources()
    report = queue.export_pending(tmp_path / "pending")
    assert report["need_pdf"] == 0 and report["verify_downloaded_file"] == 1
    row = json.loads((tmp_path / "pending/pending.json").read_text(encoding="utf-8"))[0]
    assert Path(row["retained_file"]).is_file()
    assert queue.audit()["ok"]


def test_missing_title_requires_identity_review_even_when_doi_present(setup):
    queue, store, _, _, tmp_path = setup
    paper = queue.register([{"doi": "10.1234/no-title"}], provenance={})["papers"][0]
    state = queue.attach(paper["paper_id"], make_pdf(tmp_path / "no-title.pdf", doi="10.1234/no-title"))
    assert state["availability"] == "identity_review_required" and not store.sources()


def test_bad_later_attempt_cannot_erase_success_or_conversion(setup, monkeypatch):
    queue, store, _, paper_id, tmp_path = setup
    pdf = make_pdf(tmp_path / "correct.pdf")
    queue.attach(paper_id, pdf)
    old = queue.convert(paper_id)
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"HTML")
    state = queue.attach(paper_id, bad)
    assert state["availability"] == "available" and state["source_id"] == old["source_id"]
    assert state["fulltext_searchable"] and state["conversion_id"] == old["conversion_id"]
    monkeypatch.setattr(literature, "download_pdf", lambda url: pytest.fail("Already available must not download"))
    assert queue.fetch(paper_id)["source_id"] == old["source_id"]
    again = queue.attach(paper_id, pdf)
    assert again["source_id"] == old["source_id"] and again["conversion_id"] == old["conversion_id"]
    assert len(store.sources()) == 1


def test_new_pdf_bytes_create_new_unparsed_source_without_overwriting_old(setup):
    queue, store, _, paper_id, tmp_path = setup
    first = queue.attach(paper_id, make_pdf(tmp_path / "first.pdf"))
    converted = queue.convert(paper_id)
    old_bytes = Path(store.source(first["source_id"])["path"]).read_bytes()
    updated = queue.attach(paper_id, make_pdf(tmp_path / "revision.pdf", extra="Synthetic revised study dose 2.50 mg"))
    assert updated["source_id"] != first["source_id"]
    assert updated["parsing_status"] == "not_started" and updated["conversion_id"] is None
    assert not updated["fulltext_searchable"]
    assert Path(store.source(first["source_id"])["path"]).read_bytes() == old_bytes
    assert converted["conversion_id"] in updated["conversion_ids"]


def test_failed_reconversion_preserves_previous_searchable_conversion(setup, monkeypatch):
    from evidence_database import adapters
    queue, _, _, paper_id, tmp_path = setup
    queue.attach(paper_id, make_pdf(tmp_path / "correct.pdf"))
    good = queue.convert(paper_id)
    def failure(store, source_id):
        return store.add_conversion(source_id, parser="synthetic-failure", parser_version="1", blocks=[], status="failed")
    monkeypatch.setattr(adapters, "run_baseline", failure)
    failed = queue.convert(paper_id)
    assert failed["conversion_id"] == good["conversion_id"] and failed["fulltext_searchable"]
    assert failed["last_conversion_attempt"]["status"] == "failed"
    assert failed["last_failure"] == "conversion_failed" and queue.audit()["ok"]


def test_null_title_keeps_file_pending(setup):
    queue, store, _, _, tmp_path = setup
    paper = queue.register([{"doi": "10.1234/null-title", "title": None}], provenance={})["papers"][0]
    state = queue.attach(paper["paper_id"], make_pdf(tmp_path / "null.pdf", doi="10.1234/null-title"))
    assert state["availability"] == "identity_review_required" and not store.sources()


@pytest.mark.parametrize("header", ["Supporting Online Material", "Supplementary Data"])
def test_alternate_supplement_headers_cannot_impersonate_main(setup, header):
    queue, store, _, paper_id, tmp_path = setup
    state = queue.attach(paper_id, make_pdf(tmp_path / "supp.pdf", title=header + "\n" + TITLE))
    assert state["last_failure"] == "supplement_cannot_replace_primary_paper" and not store.sources()


def test_conflicting_doi_cannot_be_ignored_when_expected_doi_also_present(setup):
    queue, store, _, paper_id, tmp_path = setup
    path = make_pdf(tmp_path / "two-dois.pdf", extra="Another paper identifier: 10.1234/other.review")
    state = queue.attach(paper_id, path)
    assert state["last_failure"] == "front_page_doi_mismatch" and not store.sources()


def test_doi_prefix_is_not_exact_identity(setup):
    queue, store, _, paper_id, tmp_path = setup
    state = queue.attach(paper_id, make_pdf(tmp_path / "prefix.pdf", doi=DOI + ".extra"))
    assert state["last_failure"] == "front_page_doi_mismatch" and not store.sources()


def test_institutional_cover_allows_same_page_title_and_doi_on_page_two(setup):
    queue, store, _, paper_id, tmp_path = setup
    path = make_pdf(tmp_path / "cover.pdf", title="University public repository deposit cover page", doi=None,
                    second_page=TITLE + "\nhttps://doi.org/" + DOI)
    state = queue.attach(paper_id, path)
    assert state["availability"] == "available" and len(store.sources()) == 1
    assert state["attachments"][0]["identity"]["identity_page"] == 2


def test_institutional_research_database_cover_is_explicit_evidence(setup):
    queue, store, _, paper_id, tmp_path = setup
    path = make_pdf(tmp_path / "research-cover.pdf", title="University of Synthetic Research database", doi=None,
                    extra="Document version: Published article\nDownloaded from the research database.",
                    second_page=TITLE + "\nhttps://doi.org/" + DOI)
    state = queue.attach(paper_id, path)
    assert state["availability"] == "available" and len(store.sources()) == 1
    assert state["attachments"][0]["identity"]["inspected_frontmatter_pages"][0]["cover_evidence"]


def test_cover_does_not_allow_cross_page_title_doi_assembly(setup):
    queue, store, _, paper_id, tmp_path = setup
    path = make_pdf(tmp_path / "cross-page.pdf", title="University public repository deposit cover page", doi=DOI,
                    second_page=TITLE + "\nNo article identifier on this page")
    state = queue.attach(paper_id, path)
    assert state["availability"] == "identity_review_required" and not store.sources()


@pytest.mark.parametrize("heading", ["References and Notes", "7. References", "IV. Bibliography",
                                    "4 Literature Cited", "References & Notes"])
def test_reference_heading_variants_do_not_make_later_citations_identity(setup, heading):
    queue, store, _, paper_id, tmp_path = setup
    path = make_pdf(tmp_path / "review.pdf", title="An unrelated review of synthetic molecular observations", doi=None,
                    second_page=heading + "\n1. " + TITLE + "\nhttps://doi.org/" + DOI)
    state = queue.attach(paper_id, path)
    assert state["availability"] == "identity_review_required" and not store.sources()


def test_later_exact_title_doi_requires_explicit_cover_evidence(setup):
    queue, store, _, paper_id, tmp_path = setup
    path = make_pdf(tmp_path / "ordinary.pdf", title="An unrelated review of synthetic molecular observations", doi=None,
                    second_page=TITLE + "\nhttps://doi.org/" + DOI)
    state = queue.attach(paper_id, path)
    assert state["availability"] == "identity_review_required" and not store.sources()


def test_explicit_cover_does_not_bypass_reference_heading(setup):
    queue, store, _, paper_id, tmp_path = setup
    path = make_pdf(tmp_path / "cover-references.pdf", title="University public repository deposit cover page", doi=None,
                    second_page="References and Notes\n1. " + TITLE + "\nhttps://doi.org/" + DOI)
    state = queue.attach(paper_id, path)
    assert state["availability"] == "identity_review_required" and not store.sources()


def test_repository_wording_in_article_does_not_allow_later_identity(setup):
    queue, store, _, paper_id, tmp_path = setup
    path = make_pdf(tmp_path / "article.pdf", title="University public repository deposit cover page", doi=None,
                    extra="Abstract\nThis is an article discussing repository use.",
                    second_page=TITLE + "\nhttps://doi.org/" + DOI)
    state = queue.attach(paper_id, path)
    assert state["availability"] == "identity_review_required" and not store.sources()


@pytest.mark.parametrize("table", ["events", "batches"])
def test_immutable_record_tampering_is_detected(setup, table):
    queue, _, _, _, _ = setup
    path = next((queue.root / table).glob("*.json"))
    path.write_bytes(path.read_bytes() + b" ")
    assert not queue.audit()["ok"]


def test_event_sql_binding_tampering_is_detected(setup):
    queue, _, _, paper_id, _ = setup
    with closing(sqlite3.connect(queue.db_path)) as db, db:
        db.execute("UPDATE events SET paper_id='wrong'")
    with pytest.raises(EvidenceError, match="index binding"):
        queue.current(paper_id)
    assert not queue.audit()["ok"]


def test_missing_index_row_is_detected(setup):
    queue, _, _, _, _ = setup
    with closing(sqlite3.connect(queue.db_path)) as db, db:
        db.execute("DELETE FROM events")
    assert not queue.audit()["ok"]


def test_retained_file_tampering_is_detected(setup):
    queue, _, _, paper_id, tmp_path = setup
    state = queue.attach(paper_id, make_pdf(tmp_path / "scan.pdf", scanned=True))
    path = Path(state["attachments"][0]["object"]["path"])
    path.write_bytes(path.read_bytes() + b"changed")
    assert not queue.audit()["ok"]


def test_stale_snapshot_cannot_overwrite_new_state(setup):
    queue, _, record, paper_id, _ = setup
    stale = copy.deepcopy(queue.current(paper_id))
    queue.register([record], provenance={"new": True})
    with pytest.raises(EvidenceError, match="changed concurrently"):
        queue._append(stale, "synthetic-stale-write", {})
    assert queue.audit()["ok"]


def test_invalid_identifier_is_rejected(setup):
    queue, _, _, _, _ = setup
    with pytest.raises(EvidenceError, match="Invalid acquisition paper ID"):
        queue.current("../../secret")
