"""Read-only consumers must not initialize, repair, or mutate the evidence store."""

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from evidence_database.store import EvidenceError, Store


def tree_state(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


@pytest.fixture
def existing_store(tmp_path):
    import pymupdf as fitz

    original = tmp_path / "synthetic.pdf"
    with fitz.open() as document:
        page = document.new_page(width=300, height=400)
        page.insert_text((30, 30), "Synthetic efficacy value 15.5.")
        document.save(original)
    native = tmp_path / "native.txt"
    native.write_text("Synthetic efficacy value 15.5.", encoding="utf-8")
    writer = Store(tmp_path / "existing store #1")
    source = writer.ingest(original, "synthetic-readonly-test")
    conversion = writer.add_conversion(source["source_id"], parser="synthetic", parser_version="1",
        blocks=[{"id": "b1", "kind": "paragraph", "text": native.read_text(), "locator": {"page": 1}}],
        artifacts={"native.txt": native})
    citation = conversion["citations"][0]
    item = writer.get(citation)
    image = tmp_path / "synthetic-page.png"
    with fitz.open(original) as document:
        image.write_bytes(document[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False, annots=True).tobytes("png"))
    packet = writer.register_packet(citation, artifacts={"page.png": image}, metadata={
        "citation": citation, "source_id": source["source_id"], "source_sha256": source["sha256"],
        "locator": item["block"]["locator"], "renderer": "pymupdf", "renderer_version": fitz.VersionBind,
        "scale": 2, "rendered_pages": [{"page": 1, "artifact": "page.png"}]})
    writer.review(citation, "Synthetic efficacy", decision="approve", reviewer="Synthetic test fixture",
                  reviewer_kind="human", note="Synthetic regression test, not a real human review",
                  viewed_original=True, packet_id=packet["packet_id"])
    return writer, source, conversion, citation


def test_nonexistent_store_fails_without_creating_anything(tmp_path):
    missing = tmp_path / "missing-parent" / "store"
    before = tree_state(tmp_path)
    with pytest.raises(EvidenceError, match="missing required directory"):
        Store(missing, read_only=True)
    assert not missing.exists()
    assert tree_state(tmp_path) == before


@pytest.mark.parametrize("invalid", [None, 0, 1, "true"])
def test_read_only_flag_requires_boolean_without_creating_store(tmp_path, invalid):
    missing = tmp_path / "missing"
    with pytest.raises(EvidenceError, match="read_only must be a boolean"):
        Store(missing, read_only=invalid)
    assert not missing.exists()


def test_all_read_methods_preserve_results_and_file_hashes(existing_store):
    writer, source, conversion, citation = existing_store
    before = tree_state(writer.root)
    reader = Store(writer.root, read_only=True)
    assert reader.read_only is True
    assert reader.source(source["source_id"]) == writer.source(source["source_id"])
    assert reader.sources() == writer.sources()
    assert reader.ingestion_receipts() == writer.ingestion_receipts()
    assert reader.ingestion_receipts(source["source_id"]) == writer.ingestion_receipts(source["source_id"])
    assert reader.conversion(conversion["conversion_id"]) == writer.conversion(conversion["conversion_id"])
    assert reader.conversions() == writer.conversions()
    assert reader.conversions("synthetic-readonly-test") == writer.conversions("synthetic-readonly-test")
    assert reader.get(citation) == writer.get(citation)
    assert reader.search("efficacy") == writer.search("efficacy")
    assert reader.reviews(citation) == writer.reviews(citation)
    assert reader.check(citation, "Synthetic efficacy") == writer.check(citation, "Synthetic efficacy")
    assert reader.check(citation, "Synthetic efficacy")["admitted"] is True
    assert reader.check(citation, "fabricated result")["quote_matches"] is False
    assert reader.audit() == writer.audit()
    assert reader.audit()["ok"] is True
    assert tree_state(writer.root) == before


def test_initialization_uses_read_only_uri_query_only_and_no_ddl(existing_store, monkeypatch):
    writer, *_ = existing_store
    actual_connect = sqlite3.connect
    connections, statements = [], []

    def connect(database, **kwargs):
        connections.append((database, kwargs))
        connection = actual_connect(database, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    before = tree_state(writer.root)
    reader = Store(writer.root, read_only=True)
    with reader._db() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
    assert connections
    assert all(name == writer.db_path.as_uri() + "?mode=ro" and options["uri"] for name, options in connections)
    assert not any(sql.lstrip().upper().startswith(("CREATE ", "ALTER ", "INSERT ", "UPDATE ", "DELETE "))
                   for sql in statements)
    assert tree_state(writer.root) == before


@pytest.mark.parametrize("operation", ["ingest", "add_conversion", "register_packet", "approve", "reject", "revoke",
                                       "_write_once", "_snapshot", "_record_data", "_save", "_artifacts"])
def test_every_mutating_entry_rejects_before_reading_or_writing(existing_store, operation):
    writer, *_ = existing_store
    reader = Store(writer.root, read_only=True)
    before = tree_state(writer.root)
    missing = writer.root / "must-not-be-created" / "artifact.txt"
    # Missing inputs and invalid references prove the read-only gate runs before
    # copying artifacts, inspecting a PDF, or resolving the requested record.
    actions = {
        "ingest": lambda: reader.ingest(missing, "new-document"),
        "add_conversion": lambda: reader.add_conversion("missing-source", parser="test", parser_version="1",
                                                        blocks=[], artifacts={"new.txt": missing}),
        "register_packet": lambda: reader.register_packet("missing-citation", artifacts={"new.png": missing},
                                                           metadata={}),
        "approve": lambda: reader.review("missing-citation", "quote", decision="approve", reviewer="test",
                                          reviewer_kind="human", note="synthetic"),
        "reject": lambda: reader.review("missing-citation", "quote", decision="reject", reviewer="test",
                                         reviewer_kind="ai", note="synthetic"),
        "revoke": lambda: reader.review("missing-citation", "quote", decision="revoke", reviewer="test",
                                         reviewer_kind="human", note="synthetic"),
        "_write_once": lambda: reader._write_once(missing, b"new bytes"),
        "_snapshot": lambda: reader._snapshot(missing),
        "_record_data": lambda: reader._record_data("sources", {"new": "record"}),
        "_save": lambda: reader._save("sources", {"new": "record"}),
        "_artifacts": lambda: reader._artifacts({"new.txt": missing}),
    }
    with pytest.raises(EvidenceError, match="read-only; mutations are not permitted"):
        actions[operation]()
    assert tree_state(writer.root) == before
    assert reader.audit()["ok"] is True


@pytest.mark.parametrize("statement", ["CREATE TABLE forbidden (value TEXT)",
                                       "UPDATE blocks SET text='tampered'", "PRAGMA query_only=OFF"])
def test_sql_connection_rejects_writes_and_disabling_query_only(existing_store, statement):
    writer, *_ = existing_store
    reader = Store(writer.root, read_only=True)
    before = tree_state(writer.root)
    with pytest.raises(EvidenceError), reader._db() as connection:
        connection.execute(statement)
    assert tree_state(writer.root) == before


def test_sql_attach_cannot_create_another_database(existing_store):
    writer, *_ = existing_store
    reader = Store(writer.root, read_only=True)
    before = tree_state(writer.root)
    attached = writer.root / "forbidden.sqlite3"
    with pytest.raises(EvidenceError), reader._db() as connection:
        connection.execute("ATTACH DATABASE ? AS forbidden", (str(attached),))
    assert not attached.exists()
    assert tree_state(writer.root) == before


@pytest.mark.parametrize("damage", ["missing_index", "missing_objects", "missing_records", "corrupt_index",
                                    "missing_table", "missing_column", "fake_fts"])
def test_read_only_validates_structure_without_repair(tmp_path, damage):
    writer = Store(tmp_path / "db")
    if damage == "missing_index":
        writer.db_path.unlink()
    elif damage == "missing_objects":
        (writer.root / "objects").rmdir()
    elif damage == "missing_records":
        (writer.root / "records" / "reviews").rmdir()
    elif damage == "corrupt_index":
        writer.db_path.write_bytes(b"not a SQLite database")
    else:
        with closing(sqlite3.connect(writer.db_path)) as connection, connection:
            if damage == "missing_table":
                connection.execute("DROP TABLE reviews")
            elif damage == "missing_column":
                connection.execute("ALTER TABLE reviews DROP COLUMN record_json")
            else:
                connection.execute("DROP TABLE blocks_fts")
                connection.execute("CREATE TABLE blocks_fts (citation TEXT, text TEXT)")
    before = tree_state(writer.root)
    with pytest.raises(EvidenceError):
        Store(writer.root, read_only=True)
    assert tree_state(writer.root) == before


def test_missing_index_after_initialization_is_not_recreated(existing_store):
    writer, source, _, _ = existing_store
    reader = Store(writer.root, read_only=True)
    writer.db_path.unlink()
    before = tree_state(writer.root)
    with pytest.raises(EvidenceError, match="existing regular index.sqlite3"):
        reader.source(source["source_id"])
    assert not writer.db_path.exists()
    assert tree_state(writer.root) == before


def test_read_only_still_detects_corrupt_evidence(existing_store):
    writer, source, _, citation = existing_store
    reader = Store(writer.root, read_only=True)
    Path(source["path"]).write_bytes(b"tampered")
    before = tree_state(writer.root)
    with pytest.raises(EvidenceError, match="integrity failure"):
        reader.get(citation)
    assert reader.audit()["ok"] is False
    assert tree_state(writer.root) == before


def test_wal_store_rejected_before_sqlite_creates_sidecars(tmp_path, monkeypatch):
    writer = Store(tmp_path / "db")
    with closing(sqlite3.connect(writer.db_path)) as connection, connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    before = tree_state(writer.root)
    assert not (writer.root / "index.sqlite3-wal").exists()
    assert not (writer.root / "index.sqlite3-shm").exists()

    def connect(*args, **kwargs):
        pytest.fail("WAL must be rejected before sqlite3.connect can create sidecars")

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(EvidenceError, match="does not support WAL"):
        Store(writer.root, read_only=True)
    assert tree_state(writer.root) == before


def test_default_store_remains_writable(tmp_path):
    writer = Store(tmp_path / "new-store")
    original = tmp_path / "original.txt"
    original.write_text("Synthetic evidence", encoding="utf-8")
    assert writer.read_only is False
    source = writer.ingest(original, "default-writable")
    assert source["document_id"] == "default-writable"
    assert writer.audit()["ok"] is True
