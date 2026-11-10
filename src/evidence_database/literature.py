"""Public literature metadata and bounded, credential-free PDF retrieval.

Metadata is discovery material, never paragraph evidence. Free access is distinct
from an open reuse licence; a candidate URL is not proof of a successful download.
Provider responses and exact queries are retained for the acquisition receipt.
"""

from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import re
import socket
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
CROSSREF = "https://api.crossref.org/works"
PMC_CLOUD = "https://pmc-oa-opendata.s3.amazonaws.com/"
USER_AGENT = "local-evidence-database/0.1 (public literature acquisition)"
MAX_REDIRECTS = 5
MAX_ATTEMPTS = 3
MAX_METADATA_BYTES = 16 * 1024 * 1024


class LiteratureError(ValueError):
    """A provider, URL, or content check failed; no access claim is implied."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def normalize_doi(value: str) -> str:
    """Normalize a DOI or doi.org URL without guessing missing identifiers."""
    if not isinstance(value, str):
        raise ValueError("DOI must be a non-empty string")  # noqa: TRY004 - public validation contract
    value = value.strip()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^doi:\s*", "", value, flags=re.IGNORECASE)
    value = unquote(value).strip().lower()
    if not re.fullmatch(r"10\.\d{4,9}/[^\s\x00-\x1f<>\"]+", value):
        raise ValueError(f"Invalid DOI: {value!r}")
    return value


def _doi_or_none(value):
    try:
        return normalize_doi(value)
    except ValueError:
        return None


def _public_url(url: str) -> None:
    """Check every redirect before making a request, including DNS addresses."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise LiteratureError("Malformed public HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LiteratureError("Only public HTTP(S) URLs are permitted")
    if parsed.username is not None or parsed.password is not None:
        raise LiteratureError("Credentials in URLs are not permitted")
    if any(ord(char) < 33 for char in url) or "\\" in url:
        raise LiteratureError("Whitespace, control characters, or backslashes in URL")
    if port not in {None, 80, 443}:
        raise LiteratureError("Only standard public HTTP(S) ports are permitted")
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname, port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise LiteratureError(f"DNS lookup failed for {parsed.hostname}: {exc}") from exc
    if not addresses:
        raise LiteratureError("URL hostname has no resolved public address")
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0].split("%", 1)[0])
        if not ip.is_global:
            raise LiteratureError(f"Non-public address is not permitted: {parsed.hostname}")


def _checked_socket(sock):
    """Reject DNS rebinding before HTTP bytes or a TLS handshake are sent."""
    try:
        address = sock.getpeername()[0].split("%", 1)[0]
        if not ipaddress.ip_address(address).is_global:
            raise LiteratureError("Connection resolved to a non-public address")
    except Exception:
        sock.close()
        raise
    return sock


def _public_adapter():
    from requests.adapters import HTTPAdapter
    from urllib3.connection import HTTPConnection, HTTPSConnection
    from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool

    class PublicConnectionMixin:
        def _new_conn(self):
            return _checked_socket(super()._new_conn())

    class PublicHTTPConnection(PublicConnectionMixin, HTTPConnection):
        pass

    class PublicHTTPSConnection(PublicConnectionMixin, HTTPSConnection):
        pass

    class PublicHTTPPool(HTTPConnectionPool):
        ConnectionCls = PublicHTTPConnection

    class PublicHTTPSPool(HTTPSConnectionPool):
        ConnectionCls = PublicHTTPSConnection

    class PublicAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            super().init_poolmanager(*args, **kwargs)
            self.poolmanager.pool_classes_by_scheme = {"http": PublicHTTPPool, "https": PublicHTTPSPool}

    return PublicAdapter(max_retries=0)


def _request_bytes(url: str, *, params: dict | None = None, max_bytes: int) -> dict:
    """Bound size, time, retries and redirects; never use cookies or credentials."""
    try:
        import requests
    except ImportError as exc:
        raise LiteratureError("Install .[pdf] (includes requests) to access literature providers") from exc
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if params:
        parsed = urlsplit(url)
        query = parsed.query + ("&" if parsed.query else "") + urlencode(params)
        url = urlunsplit(parsed._replace(query=query))
    requested_url, started = url, _now()
    deadline = time.monotonic() + 120
    redirects = []
    with requests.Session() as session:
        # Disable environment proxies and .netrc authentication. Each hop starts
        # without cookies, including redirects to a different public hostname.
        session.trust_env = False
        session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
        session.mount("http://", _public_adapter())
        session.mount("https://", _public_adapter())
        for hop in range(MAX_REDIRECTS + 1):
            if time.monotonic() >= deadline:
                raise LiteratureError(f"Request exceeded 120 second budget: {requested_url}")
            _public_url(url)
            for attempt in range(MAX_ATTEMPTS):
                session.cookies.clear()
                try:
                    response = session.get(url, timeout=(10, 30), stream=True, allow_redirects=False)
                except requests.RequestException as exc:
                    if attempt + 1 == MAX_ATTEMPTS:
                        raise LiteratureError(f"Request failed for {url}: {exc}") from exc
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if response.status_code in {429, 500, 502, 503, 504} and attempt + 1 < MAX_ATTEMPTS:
                    retry_after = response.headers.get("Retry-After", "")
                    response.close()
                    if retry_after.isdigit() and int(retry_after) > 5:
                        raise LiteratureError(f"HTTP {response.status_code} for {url}; Retry-After {retry_after}")
                    time.sleep(float(retry_after) if retry_after.isdigit() else 0.5 * (attempt + 1))
                    continue
                break
            try:
                status = response.status_code
                if status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise LiteratureError(f"HTTP {status} has no redirect Location: {url}")
                    if hop == MAX_REDIRECTS:
                        raise LiteratureError(f"Redirect limit exceeded for {requested_url}")
                    next_url = urljoin(url, location)
                    redirects.append({"url": url, "status_code": status, "location": next_url})
                    url = next_url
                    continue
                if status != 200:
                    raise LiteratureError(f"HTTP {status} for {url}")
                length = response.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > max_bytes:
                    raise LiteratureError(f"Response exceeds {max_bytes} bytes (Content-Length={length})")
                chunks, received = [], 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if time.monotonic() >= deadline:
                        raise LiteratureError(f"Response exceeded 120 second budget: {requested_url}")
                    received += len(chunk)
                    if received > max_bytes:
                        raise LiteratureError(f"Response exceeds {max_bytes} bytes while streaming")
                    chunks.append(chunk)
                data = b"".join(chunks)
                return {
                    "data": data, "requested_url": requested_url, "final_url": url,
                    "content_type": response.headers.get("Content-Type", ""), "status_code": status,
                    "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data),
                    "started_at": started, "retrieved_at": _now(), "redirects": redirects,
                }
            except requests.RequestException as exc:
                raise LiteratureError(f"Response stream failed for {url}: {exc}") from exc
            finally:
                response.close()
    raise LiteratureError(f"No response obtained for {requested_url}")


def _get_json(url: str, params: dict | None = None) -> tuple[dict, dict]:
    response = _request_bytes(url, params=params, max_bytes=MAX_METADATA_BYTES)
    data = response.pop("data")
    try:
        raw_response = data.decode("utf-8")
        payload = json.loads(raw_response)
    except (UnicodeError, ValueError) as exc:
        raise LiteratureError(f"Provider did not return valid UTF-8 JSON: {url}") from exc
    if not isinstance(payload, dict):
        raise LiteratureError(f"Expected a JSON object from provider: {url}")
    response.update({"request_params": params or {}, "raw_response": raw_response})
    return payload, response


def download_pdf(url: str, *, max_bytes: int = 50 * 1024 * 1024) -> dict:
    """Download PDF bytes, not HTML; the caller must still verify paper identity."""
    response = _request_bytes(url, max_bytes=max_bytes)
    media_type = response["content_type"].split(";", 1)[0].strip().lower()
    if media_type in {"text/html", "application/xhtml+xml"}:
        raise LiteratureError(f"Expected PDF but received HTML from {response['final_url']}")
    if not response["data"].startswith(b"%PDF-"):
        raise LiteratureError(f"Response is not a PDF (%PDF- header missing): {response['final_url']}")
    return response


def _plain(value) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", "", str(value or "")))).strip()


def _europe_record(item: dict) -> dict:
    source, record_id = str(item.get("source", "")), str(item.get("id", ""))
    locations = []
    for link in item.get("fullTextUrlList", {}).get("fullTextUrl", []):
        # The record-wide isOpenAccess flag does not override a link's access
        # statement. F means freely accessible, not necessarily openly licensed.
        code = link.get("availabilityCode")
        if code not in {"F", "OA"} or str(link.get("documentStyle", "")).lower() != "pdf":
            continue
        if not link.get("url"):
            continue
        locations.append({
            "url": link["url"], "provider": "europe_pmc", "version": "unspecified",
            "license": None, "eligibility": "open_access" if code == "OA" else "free_access",
            "evidence": {
                "availability": link.get("availability"), "availabilityCode": code,
                "documentStyle": link.get("documentStyle"), "site": link.get("site"),
                "record_isOpenAccess": item.get("isOpenAccess"),
                "note": "Provider-listed PDF candidate; successful transfer and identity not yet verified",
            },
        })
    authors = [a.get("fullName") for a in item.get("authorList", {}).get("author", []) if a.get("fullName")]
    if not authors and item.get("authorString"):
        authors = [item["authorString"]]
    return {
        "doi": _doi_or_none(item.get("doi")),
        "pmid": str(item.get("pmid") or (record_id if source == "MED" else "")) or None,
        "pmcid": item.get("pmcid"), "title": _plain(item.get("title")),
        "year": item.get("pubYear"), "authors": authors,
        "provider": "europe_pmc", "source": source,
        "record_id": f"{source}:{record_id}" if source else record_id,
        "oa_locations": locations,
        "provenance": {"provider": "europe_pmc", "record": item},
    }


def _crossref_record(item: dict) -> dict:
    title = item.get("title", [])
    if isinstance(title, list):
        title = title[0] if title else ""
    year = None
    for field in ("published", "issued", "published-print", "published-online"):
        parts = item.get(field, {}).get("date-parts", [])
        if parts and parts[0]:
            year = parts[0][0]
            break
    doi = _doi_or_none(item.get("DOI"))
    authors = [
        _plain(" ".join(filter(None, [a.get("given"), a.get("family")]))) or _plain(a.get("name"))
        for a in item.get("author", [])
    ]
    return {
        "doi": doi, "pmid": None, "pmcid": None, "title": _plain(title),
        "year": year, "authors": [a for a in authors if a],
        "provider": "crossref", "record_id": doi or str(item.get("URL", "")),
        # Crossref links can require a subscription or TDM contract. A link and
        # licence field do not establish that this particular PDF is free.
        "oa_locations": [], "provenance": {"provider": "crossref", "record": item},
    }


def _search(query: str, max_results: int, provider: str) -> dict:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("A non-empty search query is required")
    if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= 10000:
        raise ValueError("max_results must be an integer from 1 to 10000")
    records, receipts, errors, seen = [], [], [], set()
    started, cursor, total, exhausted = _now(), "*", None, False
    seen_cursors = set()
    while len(records) < max_results:
        size = min(100, max_results - len(records))
        if provider == "europe_pmc":
            url = EUROPE_PMC
            params = {"query": query, "format": "json", "resultType": "core",
                      "pageSize": size, "cursorMark": cursor}
        else:
            url = CROSSREF
            params = {"query": query, "rows": size, "cursor": cursor}
        try:
            payload, receipt = _get_json(url, params)
            receipts.append(receipt)
            if provider == "europe_pmc":
                total = int(payload["hitCount"])
                items = payload.get("resultList", {}).get("result", [])
                next_cursor = payload.get("nextCursorMark")
                normalize = _europe_record
            else:
                message = payload["message"]
                total = int(message["total-results"])
                items = message.get("items", [])
                next_cursor = message.get("next-cursor")
                normalize = _crossref_record
            if not isinstance(items, list):
                raise TypeError("Provider records are not a list")
            before = len(records)
            for item in items:
                record = normalize(item)
                key = (record.get("source"), record["record_id"])
                if key not in seen:
                    seen.add(key)
                    records.append(record)
                    if len(records) == max_results:
                        break
            if len(records) >= total:
                exhausted = True
                break
            if not items:
                errors.append({"type": "pagination_incomplete", "message": "Provider returned no records before total hits"})
                break
            if len(records) >= max_results:
                break
            if not next_cursor or next_cursor == cursor or next_cursor in seen_cursors or len(records) == before:
                errors.append({"type": "pagination_incomplete", "message": "Provider cursor stopped before all hits"})
                break
            seen_cursors.add(cursor)
            cursor = next_cursor
        except (ValueError, KeyError, TypeError) as exc:
            errors.append({"type": type(exc).__name__, "message": str(exc), "request_params": params})
            break
    return {
        "records": records,
        "provenance": {
            "provider": provider, "query": query, "max_results": max_results,
            "started_at": started, "completed_at": _now(), "total_hits": total,
            "retrieved": len(records), "truncated": not exhausted,
            "errors": errors, "requests": receipts,
            "coverage_note": "Provider search matches only; neither relevance nor exhaustive literature coverage is guaranteed",
        },
    }


def search_europe_pmc(query: str, *, max_results: int = 100) -> dict:
    """Search Europe PMC core records, retaining exact-query pagination receipts."""
    return _search(query, max_results, "europe_pmc")


def search_crossref(query: str, *, max_results: int = 100) -> dict:
    """Search Crossref works; subscription/TDM links are not automatic candidates."""
    return _search(query, max_results, "crossref")


def _pmc_locations(pmcid: str, doi: str) -> tuple[list, dict]:
    """Inspect only this article's version metadata in the official public bucket.

    The legacy oa.fcgi service was retired in August 2026. A PMCID alone does
    not guarantee membership in this dataset or that it includes a PDF.
    """
    locations, requests, errors = [], [], []
    provenance = {"provider": "pmc_cloud", "pmcid": pmcid, "requests": requests, "errors": errors}
    if not re.fullmatch(r"PMC\d+", pmcid):
        raise ValueError("Invalid PMCID")
    params = {"list-type": 2, "prefix": f"metadata/{pmcid}.", "max-keys": 100}
    try:
        response = _request_bytes(PMC_CLOUD, params=params, max_bytes=MAX_METADATA_BYTES)
        data = response.pop("data")
        response.update({"request_params": params, "raw_response": data.decode("utf-8")})
        requests.append(response)
        root = ET.fromstring(data)
        namespace = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        provenance["truncated"] = root.findtext("s3:IsTruncated", namespaces=namespace) == "true"
        if provenance["truncated"]:
            errors.append({"type": "version_limit", "message": "More than 100 article versions; candidates incomplete"})
        keys = [entry.text or "" for entry in root.findall("s3:Contents/s3:Key", namespace)]
        keys = [key for key in keys if re.fullmatch(rf"metadata/{re.escape(pmcid)}\.\d+\.json", key)]
        provenance["version_metadata_count"] = len(keys)
        for key in keys:
            metadata, receipt = _get_json(PMC_CLOUD + quote(key, safe="/"))
            requests.append(receipt)
            if metadata.get("pmcid") != pmcid or _doi_or_none(metadata.get("doi")) != doi:
                errors.append({"type": "identity_mismatch", "metadata_key": key})
                continue
            candidate = metadata.get("pdf_url")
            if not candidate:
                continue
            parsed = urlsplit(candidate)
            if parsed.scheme != "s3" or parsed.netloc != "pmc-oa-opendata":
                errors.append({"type": "unexpected_pdf_url", "metadata_key": key})
                continue
            locations.append({
                "url": PMC_CLOUD + parsed.path.lstrip("/") + ("?" + parsed.query if parsed.query else ""),
                "provider": "pmc_cloud", "version": f"{pmcid}.{metadata.get('version')}",
                "version_kind": "author_manuscript" if metadata.get("is_manuscript") else "publisher_version",
                "license": metadata.get("license_code"),
                "eligibility": "open_access" if metadata.get("is_pmc_openaccess") else "free_access",
                "evidence": {"metadata_url": receipt["final_url"], "metadata_sha256": receipt["sha256"],
                             "is_retracted": metadata.get("is_retracted"), "metadata": metadata,
                             "note": "Official dataset PDF; processing version number does not prove newest publication"},
            })
    except (ValueError, KeyError, TypeError, ET.ParseError) as exc:
        errors.append({"type": type(exc).__name__, "message": str(exc)})
    return locations, provenance


def resolve_doi(doi: str) -> dict:
    """Resolve an exact DOI using independent metadata sources and public PDF leads.

    Failed providers remain explicit errors. An unresolved DOI remains a metadata
    placeholder for manual acquisition rather than being declared paywalled.
    """
    doi = normalize_doi(doi)
    resolved, provenance, errors = [], [], []
    eu = search_europe_pmc(f'DOI:"{doi}"', max_results=20)
    provenance.append(eu["provenance"])
    errors.extend({"provider": "europe_pmc", **error} for error in eu["provenance"]["errors"])
    resolved.extend(record for record in eu["records"] if record.get("doi") == doi)
    if eu["records"] and not resolved:
        errors.append({"provider": "europe_pmc", "type": "identity_mismatch", "message": "No exact DOI match"})
    try:
        payload, receipt = _get_json(CROSSREF + "/" + quote(doi, safe=""))
        provenance.append({"provider": "crossref", "requests": [receipt]})
        record = _crossref_record(payload["message"])
        if record["doi"] != doi:
            errors.append({"provider": "crossref", "type": "identity_mismatch", "message": "Returned DOI differs"})
        else:
            resolved.append(record)
    except (ValueError, KeyError, TypeError) as exc:
        errors.append({"provider": "crossref", "type": type(exc).__name__, "message": str(exc)})
    result = {"doi": doi, "pmid": None, "pmcid": None, "title": "", "year": None, "authors": [],
              "provider": "doi_resolution", "record_id": doi, "oa_locations": []}
    for record in resolved:
        for key in ("pmid", "pmcid", "title", "year", "authors"):
            if not result[key] and record.get(key):
                result[key] = record[key]
        result["oa_locations"].extend(record["oa_locations"])
    if result["pmcid"]:
        locations, cloud_provenance = _pmc_locations(result["pmcid"], doi)
        # Prefer the automated access service over a web download endpoint.
        result["oa_locations"] = locations + result["oa_locations"]
        provenance.append(cloud_provenance)
        errors.extend({"provider": "pmc_cloud", **error} for error in cloud_provenance["errors"])
    unique = {}
    for location in result["oa_locations"]:
        unique.setdefault(location["url"], location)
    result["oa_locations"] = list(unique.values())
    result["metadata_status"] = "resolved" if resolved else "unresolved"
    result["errors"] = errors
    result["provenance"] = {"resolved_at": _now(), "requested_doi": doi, "providers": provenance,
                            "matched_records": [r["provenance"] for r in resolved]}
    return result
