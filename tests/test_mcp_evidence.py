"""MCP evidence boundaries using synthetic local papers, never real approvals."""
import hashlib
import json
import sqlite3
from contextlib import closing

import pytest

from evidence_database.mcp_evidence import MAX_TEXT, EvidenceTools
from evidence_database.store import EvidenceError, Store


@pytest.fixture
def archive(tmp_path):
    import pymupdf
    pdf = tmp_path / "synthetic.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=300, height=400)
        page.insert_text((20, 30), "Synthetic evidence value 15.5 mg.")
        doc.save(pdf)
    store = Store(tmp_path / "store")
    source = store.ingest(pdf, "synthetic-paper", metadata={"literature": {
        "title": "Synthetic fixture paper", "doi": "10.1234/fixture", "pmid": "123456",
        "provenance": {"secret_private_path": "C:/private/never-return.txt", "raw": "x" * 10000}}})
    conversion = store.add_conversion(source["source_id"], parser="fixture", parser_version="1",
        blocks=[{"id": "b:1", "kind": "paragraph", "text": "Synthetic evidence value 15.5 mg.",
                 "locator": {"page": 1}, "issues": ["synthetic_unreviewed"]}])
    tools = EvidenceTools(store.root, tmp_path / "review")
    return store, source, conversion, tools


def test_get_preserves_bound_fields_without_native_or_path_leak(archive):
    _, source, conversion, tools = archive
    value = tools.get(conversion["citations"][0])
    assert value["source_id"] == source["source_id"]
    assert value["source_sha256"] == value["source"]["sha256"] == source["sha256"]
    assert value["source"]["title"] == "Synthetic fixture paper"
    assert value["source"]["doi"] == "10.1234/fixture"
    assert value["text"] == "Synthetic evidence value 15.5 mg."
    assert value["locator"] == {"page": 1}
    assert value["block_issues"] == ["synthetic_unreviewed"]
    assert value["review_status"] == "pending"
    assert value["text_truncated"] is False and value["scientifically_verified"] is False
    encoded = json.dumps(value)
    assert str(source["path"]) not in encoded and "never-return" not in encoded and "provenance" not in encoded
    assert len(encoded) < 3000


def test_search_discovery_is_distinct_from_quote_admission(archive):
    _, _, conversion, tools = archive
    hit = tools.search("Synthetic")["items"][0]
    assert hit["citation"] == conversion["citations"][0]
    assert hit["excerpt_truncated"] is False and hit["version_selection"] == "only_available_version"
    true_quote = tools.check_quote(hit["citation"], "15.5 mg")
    false_quote = tools.check_quote(hit["citation"], "5.5 mg")
    assert true_quote["quote_matches"] is True and true_quote["admitted"] is False
    assert true_quote["reason"] == "pending_review"
    assert false_quote["quote_matches"] is False and false_quote["admitted"] is False
    assert false_quote["reason"] == "quote_not_found"
    assert not hasattr(tools, "review")
    with pytest.raises(TypeError):
        tools.check_quote(hit["citation"], "15.5 mg", require_review=False)


def test_explicit_selection_and_old_version_warning(archive, tmp_path):
    store, source, old, tools = archive
    new = store.add_conversion(source["source_id"], parser="fixture", parser_version="2", status="partial",
        settings={"parent_conversion_id": old["conversion_id"]}, issues=["table_alignment_unresolved"],
        blocks=[{"id": "b2", "kind": "text", "text": "Synthetic evidence revised 15.5 mg", "locator": {"page": 1}}])
    all_versions = tools.search("Synthetic")
    assert all_versions["total_matches"] == 2
    assert all(x["version_selection"] == "unselected_multiple_versions" for x in all_versions["items"])
    assert all_versions["version_warnings"][0]["available_version_count"] == 2
    selected = EvidenceTools(store.root, tmp_path / "review-selected", {"synthetic-paper": new["conversion_id"]})
    result = selected.search("Synthetic")
    assert result["total_matches"] == 1 and result["items"][0]["conversion_id"] == new["conversion_id"]
    old_result = selected.search("Synthetic", conversion_id=old["conversion_id"])["items"][0]
    assert old_result["version_selection"] == "explicit"
    assert "requested_version_differs_from_project_selection" in old_result["warnings"]
    assert selected.get(old["citations"][0])["warnings"] == ["requested_version_differs_from_project_selection"]
    versions = selected.versions("synthetic-paper")["items"]
    assert sum(v["selected"] for v in versions) == 1
    assert all(v["review_inherited"] is False for v in versions)
    assert next(v for v in versions if v["selected"])["parent_conversion_id"] == old["conversion_id"]


def test_selected_conversion_not_lost_behind_public_search_hit_cap(archive, tmp_path):
    store, source, _, _ = archive
    store.add_conversion(source["source_id"], parser="large-old", parser_version="1", blocks=[
        {"id": f"b{i:04}", "kind": "text", "text": "needle", "locator": {"page": 1}} for i in range(1001)])
    chosen = store.add_conversion(source["source_id"], parser="chosen", parser_version="1", blocks=[
        {"id": "chosen", "kind": "text", "text": "needle", "locator": {"page": 1}}])
    tools = EvidenceTools(store.root, tmp_path / "packets", {source["document_id"]: chosen["conversion_id"]})
    result = tools.search("needle")
    assert result["total_matches"] == 1
    assert result["items"][0]["citation"] == chosen["citations"][0]


def test_pagination_is_query_limit_and_snapshot_bound(archive):
    store, source, _, tools = archive
    store.add_conversion(source["source_id"], parser="more", parser_version="1", blocks=[
        {"id": f"b{i}", "kind": "text", "text": "Synthetic row " + str(i), "locator": {"page": 1}}
        for i in range(4)])
    first = tools.search("Synthetic", limit=2)
    second = tools.search("Synthetic", limit=2, cursor=first["next_cursor"])
    assert not ({i["citation"] for i in first["items"]} & {i["citation"] for i in second["items"]})
    assert first["total_matches"] == 5
    with pytest.raises(EvidenceError, match="Cursor"):
        tools.search("row", limit=2, cursor=first["next_cursor"])
    with pytest.raises(EvidenceError, match="Cursor"):
        tools.search("Synthetic", limit=3, cursor=first["next_cursor"])
    store.add_conversion(source["source_id"], parser="changed", parser_version="2", blocks=[
        {"id": "new", "kind": "text", "text": "Synthetic new", "locator": {"page": 1}}])
    with pytest.raises(EvidenceError, match="snapshot"):
        tools.search("Synthetic", limit=2, cursor=first["next_cursor"])


def test_review_change_invalidates_pagination_cursor(archive):
    store, source, conversion, tools = archive
    store.add_conversion(source["source_id"], parser="extra", parser_version="1", blocks=[
        {"id": "b2", "kind": "text", "text": "Synthetic more", "locator": {"page": 1}}])
    first = tools.search("Synthetic", limit=1)
    store.review(conversion["citations"][0], "Synthetic", decision="reject", reviewer="synthetic-agent",
                 reviewer_kind="ai", note="Synthetic rejection fixture")
    with pytest.raises(EvidenceError, match="snapshot"):
        tools.search("Synthetic", limit=1, cursor=first["next_cursor"])


def test_large_text_is_explicitly_chunked_and_never_silently_truncated(archive):
    store, source, _, tools = archive
    text = "needle " + "a" * (MAX_TEXT * 2 + 13)
    conversion = store.add_conversion(source["source_id"], parser="long", parser_version="1", blocks=[
        {"id": "long", "kind": "text", "text": text, "locator": {"page": 1}}])
    got = tools.get(conversion["citations"][0])
    assert len(got["text"]) == MAX_TEXT and got["text_truncated"] is True
    chunks, resource = [], got["block_resource_uri"]
    while resource:
        content = json.loads(tools.read_resource(resource)["text"])
        chunks.append(content["text"])
        resource = content["next_text_resource_uri"]
    assert "".join(chunks) == text
    hit = tools.search("needle")["items"][0]
    assert hit["excerpt_truncated"] is True
    assert hit["next_text_resource_uri"] == got["block_resource_uri"]
    with pytest.raises(EvidenceError):
        tools.read_resource(got["block_resource_uri"] + "/chunks/1")


def test_review_packet_and_original_resource_preserve_pending_until_explicit_synthetic_review(archive):
    store, source, conversion, tools = archive
    citation = conversion["citations"][0]
    packet = tools.prepare_review(citation)
    assert packet["approval_created"] is False and store.reviews(citation) == []
    resource = tools.read_resource(packet["packet_resource_uri"])
    metadata = json.loads(resource["text"])
    assert metadata["source_sha256"] == source["sha256"]
    assert "path" not in metadata and metadata["approval_created"] is False
    page = tools.read_resource(packet["page_resource_uris"][0])
    assert page["data"].startswith(b"\x89PNG")
    assert page["sha256"] == hashlib.sha256(page["data"]).hexdigest()
    assert page["source_sha256"] == source["sha256"] and page["physical_page"] == 1
    store.review(citation, "15.5 mg", decision="approve", reviewer="Synthetic fixture reviewer",
                 reviewer_kind="human", note="Only generated synthetic paper used", viewed_original=True,
                 packet_id=packet["packet_id"])
    assert tools.check_quote(citation, "15.5 mg")["admitted"] is True
    assert tools.check_quote(citation, "Synthetic")["admitted"] is False
    store.review(citation, "15.5 mg", decision="revoke", reviewer="Synthetic fixture reviewer",
                 reviewer_kind="human", note="Synthetic revocation fixture")
    assert tools.check_quote(citation, "15.5 mg")["admitted"] is False


def test_read_operations_do_not_change_archive_files(archive):
    store, _, conversion, tools = archive
    def snapshot():
        return {p.relative_to(store.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in store.root.rglob("*") if p.is_file()}
    before = snapshot()
    tools.search("Synthetic")
    tools.get(conversion["citations"][0])
    tools.check_quote(conversion["citations"][0], "15.5 mg")
    tools.versions("synthetic-paper")
    assert snapshot() == before


@pytest.mark.parametrize("uri", ["file:///etc/passwd", "evidence://sources/../../secret", "evidence://evil:bad/id",
    "evidence://packets/%2e%2e", "evidence://blocks/cv_x/%2fetc", "evidence://sources/a/pages/0",
    "evidence://sources/a/pages/1?file=secret", "evidence://sources/a/pages/1#secret"])
def test_resource_rejects_paths_and_noncanonical_identifiers(archive, uri):
    with pytest.raises(EvidenceError):
        archive[3].read_resource(uri)


@pytest.mark.parametrize("limit", [True, 0, -1, 21, 1.5, "2"])
def test_tool_limits_are_explicit(archive, limit):
    with pytest.raises(EvidenceError):
        archive[3].search("Synthetic", limit=limit)
    with pytest.raises(EvidenceError):
        archive[3].versions("synthetic-paper", limit=limit)


@pytest.mark.parametrize("cursor", ["!", "e30", "e30=", "a" * 2049])
def test_malformed_cursors_fail(archive, cursor):
    with pytest.raises(EvidenceError):
        archive[3].search("Synthetic", cursor=cursor)


def test_unknown_and_wrong_version_bindings_fail_closed(archive, tmp_path):
    store, source, conversion, tools = archive
    with pytest.raises(EvidenceError):
        tools.get(conversion["citations"][0] + ":missing")
    with pytest.raises(EvidenceError):
        tools.search("Synthetic", conversion_id="cv_" + "0" * 64)
    with pytest.raises(EvidenceError):
        tools.search("Synthetic", document_id="other", conversion_id=conversion["conversion_id"])
    with pytest.raises(EvidenceError):
        tools.versions("unknown-document")
    with pytest.raises(EvidenceError):
        tools.read_resource(f"evidence://sources/{source['source_id']}/pages/2")
    with pytest.raises(EvidenceError):
        EvidenceTools(store.root, tmp_path / "different-review", {"wrong-document": conversion["conversion_id"]})
    with pytest.raises(EvidenceError):
        EvidenceTools(store.root, store.root / "packets")


def test_index_tampering_is_detected_before_empty_search(archive):
    store, _, _, tools = archive
    with closing(sqlite3.connect(store.db_path)) as db, db:
        db.execute("DELETE FROM blocks_fts")
    with pytest.raises(EvidenceError, match="index"):
        tools.search("no-match")


def test_original_tampering_is_rejected(archive):
    _, source, conversion, tools = archive
    from pathlib import Path
    Path(source["path"]).write_bytes(b"tampered original")
    with pytest.raises(EvidenceError):
        tools.get(conversion["citations"][0])
    with pytest.raises(EvidenceError):
        tools.read_resource(f"evidence://sources/{source['source_id']}/pages/1")


def test_failed_conversion_is_not_searchable_and_is_reported_in_versions(archive):
    store, source, _, tools = archive
    failed = store.add_conversion(source["source_id"], parser="failed", parser_version="1", blocks=[],
                                  status="failed", issues=["parse_failed"])
    with pytest.raises(EvidenceError, match="Failed"):
        tools.search("Synthetic", conversion_id=failed["conversion_id"])
    assert any(v["status"] == "failed" for v in tools.versions("synthetic-paper")["items"])


def test_native_locator_paths_are_not_exported_but_full_locator_hash_is_bound(archive):
    store, source, _, tools = archive
    locator = {"page": 1, "native_path": "private-native.json",
               "regions": [{"page": 1, "bbox": [1, 1, 20, 20], "native_path": "private-native.xml"}]}
    conversion = store.add_conversion(source["source_id"], parser="native", parser_version="1", blocks=[
        {"id": "native", "kind": "text", "text": "Synthetic native locator", "locator": locator}])
    got = tools.get(conversion["citations"][0])
    assert got["locator"] == {"page": 1, "regions": [{"page": 1, "bbox": [1, 1, 20, 20]}]}
    expected = hashlib.sha256(json.dumps(locator, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert got["locator_sha256"] == expected
    checked = tools.check_quote(conversion["citations"][0], "Synthetic")
    assert checked["locator_sha256"] == expected and checked["locator"] == got["locator"]
    packet = tools.prepare_review(conversion["citations"][0])
    content = tools.read_resource(packet["packet_resource_uri"])["text"]
    assert "private" not in content and "native_path" not in content


@pytest.mark.parametrize("damage", ["original", "native", "record", "block_index"])
def test_metadata_inventory_never_allows_damaged_selected_evidence(archive, tmp_path, damage):
    from pathlib import Path
    store, source, _, _ = archive
    native = tmp_path / "native.json"
    native.write_text('{"synthetic":true}', encoding="utf8")
    selected = store.add_conversion(source["source_id"], parser="native-checked", parser_version="1",
        artifacts={"native.json": native}, blocks=[
            {"id": "chosen", "kind": "text", "text": "UniquelySelected synthetic result", "locator": {"page": 1}}])
    tools = EvidenceTools(store.root, tmp_path / "metadata-review", {source["document_id"]: selected["conversion_id"]})
    if damage == "original":
        Path(source["path"]).write_bytes(b"changed original")
    elif damage == "native":
        Path(selected["artifacts"]["native.json"]["path"]).write_bytes(b"changed native")
    elif damage == "record":
        store._record_path("conversions", selected["conversion_id"]).write_bytes(b"{}")
    else:
        with closing(sqlite3.connect(store.db_path)) as db, db:
            db.execute("UPDATE blocks SET text='changed index' WHERE conversion_id=?", (selected["conversion_id"],))
    if damage in {"original", "native"}:
        # A metadata list is honest about its narrower scope, not an artifact audit.
        rows = tools.versions(source["document_id"])["items"]
        assert all("metadata and index only" in row["verification_scope"] for row in rows)
    with pytest.raises(EvidenceError):
        tools.search("UniquelySelected")
    with pytest.raises(EvidenceError):
        tools.get(selected["citations"][0])
    with pytest.raises(EvidenceError):
        tools.check_quote(selected["citations"][0], "UniquelySelected")


@pytest.mark.parametrize("field,bad_value", [("document_id", "different"), ("sha256", "0" * 64), ("size", -1)])
def test_publication_metadata_preserves_receipt_source_binding(archive, field, bad_value):
    store, source, conversion, tools = archive
    receipt = store._load("ingestions", source["ingestion_id"])
    receipt[field] = bad_value
    store._save("ingestions", receipt)
    with pytest.raises(EvidenceError, match="receipt/source binding"):
        tools.get(conversion["citations"][0])
    with pytest.raises(EvidenceError, match="receipt/source binding"):
        tools.search("Synthetic")
