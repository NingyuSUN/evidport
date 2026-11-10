import hashlib
import json
import socket

import pytest

from evidence_database import literature as lit


def receipt(payload, url="https://example.org/metadata"):
    raw = json.dumps(payload)
    return {"final_url": url, "sha256": hashlib.sha256(raw.encode()).hexdigest(), "raw_response": raw}


@pytest.mark.parametrize("value", ["10.1038/ABC.42", " DOI:10.1038/ABC.42 ",
                                 "https://doi.org/10.1038%2FABC.42", "http://dx.doi.org/10.1038/ABC.42"])
def test_normalize_doi(value):
    assert lit.normalize_doi(value) == "10.1038/abc.42"


@pytest.mark.parametrize("value", ["", None, "not a DOI", "10.12/no", "10.1234/contains space", "10.1234/<tag>"])
def test_invalid_doi_is_not_guessed(value):
    with pytest.raises(ValueError):
        lit.normalize_doi(value)


def test_europe_free_pdf_is_separate_from_open_reuse_and_html():
    item = {"id": "42", "source": "MED", "doi": "10.1234/ABC", "title": "A <i>gene</i> &amp; study",
            "isOpenAccess": "N", "fullTextUrlList": {"fullTextUrl": [
                {"availabilityCode": "F", "documentStyle": "pdf", "url": "https://example.org/free.pdf"},
                {"availabilityCode": "F", "documentStyle": "html", "url": "https://example.org/free"},
                {"availabilityCode": "S", "documentStyle": "pdf", "url": "https://example.org/paid.pdf"},
                {"availabilityCode": "F", "documentStyle": "doi", "url": "https://doi.org/10.1234/abc"},
            ]}}
    record = lit._europe_record(item)
    assert record["pmid"] == "42"
    assert record["title"] == "A gene & study"
    assert len(record["oa_locations"]) == 1
    assert record["oa_locations"][0]["eligibility"] == "free_access"
    assert record["oa_locations"][0]["license"] is None


def test_crossref_link_and_license_are_not_a_free_pdf():
    record = lit._crossref_record({"DOI": "10.1234/abc", "title": ["A paper"],
                                  "link": [{"URL": "https://example.org/full.pdf", "content-type": "application/pdf"}],
                                  "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}]})
    assert record["oa_locations"] == []
    assert record["provenance"]["record"]["link"]


def test_europe_source_namespaces_prevent_non_doi_id_collision():
    med = lit._europe_record({"source": "MED", "id": "42"})
    agr = lit._europe_record({"source": "AGR", "id": "42"})
    ctx = lit._europe_record({"source": "CTX", "id": "42"})
    assert len({record["record_id"] for record in (med, agr, ctx)}) == 3
    assert med["record_id"] == "MED:42"
    assert med["pmid"] == "42"
    assert agr["pmid"] is None
    assert ctx["pmid"] is None


def test_europe_cursor_pagination_caps_and_preserves_exact_query(monkeypatch):
    calls = []

    def get_json(url, params):
        calls.append(params.copy())
        start = 0 if params["cursorMark"] == "*" else 100
        count = min(params["pageSize"], 150 - start)
        payload = {"hitCount": 150, "nextCursorMark": "next",
                   "resultList": {"result": [{"id": str(i), "source": "MED"} for i in range(start, start + count)]}}
        return payload, receipt(payload)

    monkeypatch.setattr(lit, "_get_json", get_json)
    result = lit.search_europe_pmc("  cancer AND 2020  ", max_results=120)
    assert len(result["records"]) == 120
    assert [call["pageSize"] for call in calls] == [100, 20]
    assert calls[1]["cursorMark"] == "next"
    assert result["provenance"]["query"] == "  cancer AND 2020  "
    assert result["provenance"]["total_hits"] == 150
    assert result["provenance"]["truncated"] is True
    assert len(result["provenance"]["requests"]) == 2


def test_crossref_search_complete_and_partial_failure(monkeypatch):
    calls = []

    def get_json(url, params):
        calls.append(params)
        if len(calls) > 1:
            raise lit.LiteratureError("HTTP 429")
        payload = {"message": {"total-results": 120, "next-cursor": "page2",
                               "items": [{"DOI": f"10.1234/{i}"} for i in range(100)]}}
        return payload, receipt(payload)

    monkeypatch.setattr(lit, "_get_json", get_json)
    result = lit.search_crossref("test", max_results=150)
    assert result["provenance"]["retrieved"] == 100
    assert result["provenance"]["total_hits"] == 120
    assert result["provenance"]["truncated"] is True
    assert "429" in result["provenance"]["errors"][0]["message"]


def test_search_exact_cap_equal_total_is_complete(monkeypatch):
    payload = {"hitCount": 1, "resultList": {"result": [{"id": "1", "source": "MED"}]}}
    monkeypatch.setattr(lit, "_get_json", lambda *args: (payload, receipt(payload)))
    assert lit.search_europe_pmc("test", max_results=1)["provenance"]["truncated"] is False


def test_duplicate_or_stuck_cursor_does_not_claim_complete(monkeypatch):
    payload = {"hitCount": 30, "nextCursorMark": "*",
               "resultList": {"result": [{"id": "1", "source": "MED"}]}}
    monkeypatch.setattr(lit, "_get_json", lambda *args: (payload, receipt(payload)))
    result = lit.search_europe_pmc("test")
    assert result["provenance"]["truncated"] is True
    assert result["provenance"]["errors"][0]["type"] == "pagination_incomplete"


def test_unavailable_search_has_unknown_denominator(monkeypatch):
    def fail(*args):
        raise lit.LiteratureError("HTTP 503")

    monkeypatch.setattr(lit, "_get_json", fail)
    result = lit.search_europe_pmc("test")
    assert result["records"] == []
    assert result["provenance"]["total_hits"] is None
    assert result["provenance"]["truncated"] is True


def test_resolve_exact_doi_preserves_failures_and_other_provider(monkeypatch):
    eu_record = lit._europe_record({"id": "42", "source": "MED", "doi": "10.1234/test", "title": "Title"})
    monkeypatch.setattr(lit, "search_europe_pmc", lambda *a, **kw: {
        "records": [eu_record], "provenance": {"provider": "europe_pmc", "errors": [], "requests": []}})

    def fail(*args):
        raise lit.LiteratureError("HTTP 404")

    monkeypatch.setattr(lit, "_get_json", fail)
    record = lit.resolve_doi("10.1234/TEST")
    assert record["title"] == "Title"
    assert record["doi"] == "10.1234/test"
    assert record["metadata_status"] == "resolved"
    assert record["errors"][0]["provider"] == "crossref"
    assert record["provenance"]["matched_records"]


def test_resolve_rejects_provider_identity_mismatch(monkeypatch):
    monkeypatch.setattr(lit, "search_europe_pmc", lambda *a, **kw: {
        "records": [], "provenance": {"errors": []}})
    payload = {"message": {"DOI": "10.1234/another", "title": ["Wrong title"]}}
    monkeypatch.setattr(lit, "_get_json", lambda *a: (payload, receipt(payload)))
    result = lit.resolve_doi("10.1234/wanted")
    assert result["title"] == ""
    assert result["metadata_status"] == "unresolved"
    assert result["errors"][0]["type"] == "identity_mismatch"


def test_pmc_cloud_uses_only_article_metadata_pdf_and_retains_version(monkeypatch):
    listing = b'''<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <IsTruncated>false</IsTruncated><Contents><Key>metadata/PMC42.2.json</Key></Contents>
    <Contents><Key>metadata/PMC420.1.json</Key></Contents></ListBucketResult>'''
    calls = []

    def request(url, **kwargs):
        calls.append(kwargs["params"])
        return {"data": listing, "sha256": "listing", "final_url": url}

    payload = {"doi": "10.1234/a", "pmcid": "PMC42", "version": 2, "is_manuscript": True,
               "license_code": "TDM", "is_retracted": False,
               "pdf_url": "s3://pmc-oa-opendata/PMC42.2/PMC42.2.pdf?md5=abc",
               "media_urls": ["s3://pmc-oa-opendata/PMC42.2/supplement.pdf"]}
    monkeypatch.setattr(lit, "_request_bytes", request)
    monkeypatch.setattr(lit, "_get_json", lambda *a: (payload, receipt(payload)))
    locations, provenance = lit._pmc_locations("PMC42", "10.1234/a")
    assert calls[0]["prefix"] == "metadata/PMC42."
    assert len(locations) == 1
    assert locations[0]["version"] == "PMC42.2"
    assert locations[0]["version_kind"] == "author_manuscript"
    assert locations[0]["license"] == "TDM"
    assert locations[0]["url"].endswith("PMC42.2.pdf?md5=abc")
    assert provenance["version_metadata_count"] == 1


def test_pmc_xml_only_is_not_pdf_candidate(monkeypatch):
    listing = b'''<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
    <IsTruncated>false</IsTruncated><Contents><Key>metadata/PMC42.1.json</Key></Contents></ListBucketResult>'''
    monkeypatch.setattr(lit, "_request_bytes", lambda *a, **kw: {"data": listing})
    payload = {"doi": "10.1234/a", "pmcid": "PMC42", "xml_url": "s3://pmc-oa-opendata/PMC42.1/a.xml"}
    monkeypatch.setattr(lit, "_get_json", lambda *a: (payload, receipt(payload)))
    assert lit._pmc_locations("PMC42", "10.1234/a")[0] == []


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fc00::1"])
def test_dns_private_address_rejected(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", (address, 443))])
    with pytest.raises(lit.LiteratureError, match="Non-public"):
        lit._public_url("https://example.org/a")


@pytest.mark.parametrize("url", ["file:///tmp/a", "ftp://example.org/a", "https://user:pass@example.org/a",
                                "https://example.org:8080/a", "https://example.org/a\r\nHeader:x"])
def test_unsafe_urls_rejected_without_network(url):
    with pytest.raises(lit.LiteratureError):
        lit._public_url(url)


class Response:
    def __init__(self, body=b"%PDF-1.7\nbody", status=200, headers=None):
        self.body, self.status_code = body, status
        self.headers = headers or {"Content-Type": "application/pdf"}
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.body[:8]
        yield self.body[8:]

    def close(self):
        self.closed = True


@pytest.fixture
def transport(monkeypatch):
    import requests

    class Session:
        def __init__(self):
            self.trust_env = True
            self.headers = {}
            self.cookies = set()

        def __enter__(self):
            sessions.append(self)
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return responses.pop(0)

        def mount(self, prefix, adapter):
            pass

    responses, sessions, calls = [], [], []
    monkeypatch.setattr(requests, "Session", Session)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(lit.time, "sleep", lambda seconds: None)
    return responses, sessions, calls


def test_pdf_download_hash_and_no_environment_credentials(transport):
    responses, sessions, calls = transport
    responses.append(Response())
    result = lit.download_pdf("https://example.org/a.pdf")
    assert result["sha256"] == hashlib.sha256(result["data"]).hexdigest()
    assert result["status_code"] == 200
    assert sessions[0].trust_env is False
    assert calls[0][1]["allow_redirects"] is False
    assert calls[0][1]["timeout"] == (10, 30)


def test_private_redirect_never_requested(transport, monkeypatch):
    responses, _sessions, calls = transport
    responses.append(Response(status=302, headers={"Location": "https://private.test/a.pdf"}))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **kw: [
        (2, 1, 6, "", ("10.0.0.1" if host == "private.test" else "93.184.216.34", 443))])
    with pytest.raises(lit.LiteratureError, match="Non-public"):
        lit.download_pdf("https://example.org/a.pdf")
    assert len(calls) == 1


@pytest.mark.parametrize("headers,body,limit,match", [
    ({"Content-Type": "text/html"}, b"%PDF-fake html", 1000, "HTML"),
    ({"Content-Type": "application/pdf"}, b"<html>login</html>", 1000, "header missing"),
    ({"Content-Length": "2000"}, b"%PDF-small", 1000, "Content-Length"),
    ({}, b"%PDF-" + b"x" * 50, 20, "while streaming"),
])
def test_invalid_or_oversized_download_fails(transport, headers, body, limit, match):
    transport[0].append(Response(body, headers=headers))
    with pytest.raises(lit.LiteratureError, match=match):
        lit.download_pdf("https://example.org/a.pdf", max_bytes=limit)


def test_http_status_is_not_hidden_and_retries_bounded(transport):
    responses, _sessions, calls = transport
    responses.extend([Response(status=429) for _ in range(3)])
    with pytest.raises(lit.LiteratureError, match="HTTP 429"):
        lit.download_pdf("https://example.org/a.pdf")
    assert len(calls) == 3


def test_challenge_is_not_retried(transport):
    responses, _sessions, calls = transport
    responses.append(Response(status=403))
    with pytest.raises(lit.LiteratureError, match="HTTP 403"):
        lit.download_pdf("https://example.org/a.pdf")
    assert len(calls) == 1


def test_raw_json_receipt_hash_matches_original_bytes(transport):
    raw = b'{ "hitCount": 0, "resultList": { "result": [] } }'
    transport[0].append(Response(raw, headers={"Content-Type": "application/json"}))
    payload, provenance = lit._get_json("https://example.org/search", {"query": "two words"})
    assert payload["hitCount"] == 0
    assert provenance["raw_response"].encode() == raw
    assert provenance["sha256"] == hashlib.sha256(raw).hexdigest()
    assert provenance["request_params"]["query"] == "two words"
    assert "query=two+words" in provenance["requested_url"]


def test_dns_rebinding_socket_is_closed_before_http_request():
    class Socket:
        closed = False

        def getpeername(self):
            return ("127.0.0.1", 80)

        def close(self):
            self.closed = True

    sock = Socket()
    with pytest.raises(lit.LiteratureError, match="non-public"):
        lit._checked_socket(sock)
    assert sock.closed


def test_premature_empty_page_keeps_incomplete_coverage(monkeypatch):
    payload = {"hitCount": 10, "resultList": {"result": []}}
    monkeypatch.setattr(lit, "_get_json", lambda *args: (payload, receipt(payload)))
    provenance = lit.search_europe_pmc("test")["provenance"]
    assert provenance["truncated"] is True
    assert provenance["errors"][0]["type"] == "pagination_incomplete"
