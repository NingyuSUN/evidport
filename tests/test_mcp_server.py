"""Real local MCP calls exercise the acquisition-to-citation boundary."""
import asyncio
import base64
import json
import sys
import threading

import pymupdf
import pytest

pytest.importorskip("mcp", reason="Install the mcp optional dependency to exercise the wire protocol")
from mcp import Client, StdioServerParameters

from evidence_database.mcp_server import create_server
from evidence_database.store import Store

TITLE = "Synthetic local MCP evidence lifecycle and factor activity study"
DOI = "10.1234/mcp.wire.fixture"


def setup_project(tmp_path):
    (tmp_path / "inbox").mkdir()
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_textbox((30, 30, 560, 780), TITLE + "\nhttps://doi.org/" + DOI
                            + "\nSynthetic factor activity was 1.25 mg.", fontsize=12)
        pdf.save(tmp_path / "inbox" / "paper.pdf")
    config = tmp_path / "project.json"
    config.write_text(json.dumps({"schema": 1, "project_id": "synthetic-wire", "store": "store",
        "queue": "queue", "inbox": "inbox", "runs": "runs", "papers": [{"metadata": {
            "doi": DOI, "title": TITLE, "provider": "synthetic", "record_id": "fixture",
            "oa_locations": []}}]}), encoding="utf8")
    Store(tmp_path / "store")
    return config


async def tool(client, name, arguments=None):
    result = await client.call_tool(name, arguments or {})
    assert not result.is_error, result.content
    assert isinstance(result.structured_content, dict)
    return result.structured_content


async def finish(client, result):
    async with asyncio.timeout(40):
        while result["status"] in {"queued", "running"}:
            await asyncio.sleep(.02)
            result = await tool(client, "evidport_job_status", {"job_id": result["job_id"]})
    assert result["status"] == "completed", result
    return result["result"]


@pytest.mark.parametrize("transport", ["memory", "stdio"])
def test_real_mcp_pending_import_search_original_and_quote_gate(tmp_path, transport):
    config = setup_project(tmp_path)

    async def scenario():
        server = create_server(config) if transport == "memory" else StdioServerParameters(
            command=sys.executable, args=["-m", "evidence_database.mcp_server", "--project-config", str(config)],
            env={"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        async with Client(server, read_timeout_seconds=45) as client:
            names = {item.name for item in (await client.list_tools()).tools}
            assert "evidport_evidence_check_quote" in names
            assert not any("approve" in name or "shell" in name for name in names)
            discovered = await finish(client, await tool(client, "evidport_papers_discover", {"dois": [DOI]}))
            paper_id = discovered["papers"][0]["paper_id"]
            pending = await tool(client, "evidport_papers_pending")
            assert DOI in json.dumps(pending)
            assert "acquisition_required" in json.dumps(pending)
            assert '"citation"' not in json.dumps(pending)
            await finish(client, await tool(client, "evidport_papers_import",
                {"paper_id": paper_id, "filename": "paper.pdf"}))
            await finish(client, await tool(client, "evidport_papers_convert", {"paper_id": paper_id}))
            found = await tool(client, "evidport_evidence_search", {"query": "factor"})
            hits = found.get("hits", found.get("results", found.get("items")))
            assert hits, found
            citation = hits[0]["citation"]
            item = await tool(client, "evidport_evidence_get", {"citation": citation})
            assert item["source"]["sha256"] and item["locator"]
            quote = "Synthetic factor activity was 1.25 mg."
            exact = await tool(client, "evidport_evidence_check_quote", {"citation": citation, "quote": quote})
            assert exact["quote_matches"] and not exact["admitted"]
            assert exact["scientifically_verified"] is False
            bad = await tool(client, "evidport_evidence_check_quote",
                             {"citation": citation, "quote": "Synthetic factor activity was 999 mg."})
            assert not bad["quote_matches"] and not bad["admitted"]
            page = await client.read_resource(item["page_resource_uris"][0])
            assert base64.b64decode(page.contents[0].blob).startswith(b"\x89PNG")
            block = await client.read_resource(item["block_resource_uri"])
            assert "factor" in block.contents[0].text
            packet = await finish(client, await tool(client, "evidport_evidence_prepare_review", {"citation": citation}))
            assert "packet_id" in packet
            after = await tool(client, "evidport_evidence_check_quote", {"citation": citation, "quote": quote})
            assert not after["admitted"]  # Packet preparation is not human review.
            invalid = await client.call_tool("evidport_evidence_get", {"citation": "../../outside"})
            assert invalid.is_error
            too_many = await client.call_tool("evidport_evidence_search", {"query": "factor", "limit": 21})
            assert too_many.is_error
            override = await client.call_tool("evidport_evidence_check_quote",
                {"citation": citation, "quote": quote, "require_review": False})
            assert override.is_error or override.structured_content["admitted"] is False
        assert Store(tmp_path / "store", read_only=True).audit()["counts"]["reviews"] == 0

    asyncio.run(scenario())


def test_startup_network_is_explicit_and_cli_does_not_start_server_for_describe(tmp_path, capsys):
    from evidence_database.mcp_server import main
    config = setup_project(tmp_path)
    assert main(["--project-config", str(config), "--describe"]) == 0
    description = json.loads(capsys.readouterr().out)
    assert description["network_enabled"] is False and description["human_approval_tools"] is False
    assert not (tmp_path / "runs").exists()


def test_invalid_project_does_not_create_service(tmp_path, capsys):
    from evidence_database.mcp_server import main
    assert main(["--project-config", str(tmp_path / "missing")]) == 2
    assert capsys.readouterr().out == ""


def test_receipt_lock_wait_does_not_block_the_mcp_event_loop(tmp_path, monkeypatch):
    from evidence_database.mcp_jobs import LocalJobs
    started, release = threading.Event(), threading.Event()

    def waiting_get(_self, _job_id):
        started.set()
        if not release.wait(10):
            raise RuntimeError("MCP event loop could not release a waiting receipt reader")
        return {"status": "completed"}

    monkeypatch.setattr(LocalJobs, "get", waiting_get)

    async def scenario():
        async with Client(create_server(setup_project(tmp_path)), read_timeout_seconds=15) as client:
            pending = asyncio.create_task(tool(client, "evidport_job_status", {"job_id": "fixture"}))
            try:
                assert await asyncio.to_thread(started.wait, 5)
                # A second protocol request must finish while the receipt waits.
                async with asyncio.timeout(5):
                    assert (await client.list_tools()).tools
                assert not pending.done()
            finally:
                release.set()
                await pending

    asyncio.run(scenario())


@pytest.mark.parametrize("uri", ["evidence://blocks/cv_test/b1", "evidence://blocks/cv_test/b1/chunks/20000",
                                 "evidence://sources/src_test/pages/1", "evidence://packets/pkt_test"])
def test_resource_busy_reason_survives_the_protocol(tmp_path, monkeypatch, uri):
    from evidence_database.mcp_evidence import EvidenceTools
    from evidence_database.store import EvidenceError

    def busy(_self, _uri):
        raise EvidenceError("Project operation is busy in another process; retry after it finishes")

    monkeypatch.setattr(EvidenceTools, "read_resource", busy)

    async def scenario():
        async with Client(create_server(setup_project(tmp_path))) as client:
            with pytest.raises(Exception, match="Project operation is busy in another process"):
                await client.read_resource(uri)

    asyncio.run(scenario())
