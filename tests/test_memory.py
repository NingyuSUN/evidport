"""Unit and cross-component contract tests for EvidPort advisory pitfall memory."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest

from evidence_database import cli, mcp_server
from evidence_database.mcp_evidence import MAX_TEXT, EvidenceTools
from evidence_database.memory import MemoryLedger, compute_quote_hazards
from evidence_database.packets import create_packet
from evidence_database.store import EvidenceError, Store
from evidence_database.workflow import load_project

DOI = "10.9999/synthetic.memory.test"
TITLE = "Synthetic cohort study distinguishing primary GWAS and expansion cohorts"
B12_TEXT = "Primary genome-wide association included 39 cases and 39 controls on page 1."
B40_S1 = "In the expansion validation stage, 65 cases and 100 controls were genotyped."
B40_S2 = "Replication odds ratio remained consistent across the expansion cohort."
B40_S3 = "Assay quality control passed all call-rate thresholds."
B40_TEXT = f"{B40_S1} {B40_S2} {B40_S3}"


def dir_hashes(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def env(tmp_path: Path):
    pdf = tmp_path / "paper.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.insert_textbox((30, 30, 550, 780), f"{TITLE}\nhttps://doi.org/{DOI}\n{B12_TEXT}\n{B40_TEXT}")
        doc.save(pdf)
    store_dir = tmp_path / "store"
    store = Store(store_dir)
    source = store.ingest(
        pdf,
        "paper_mem_test",
        role="primary",
        metadata={"literature": {"doi": DOI, "pmid": "88880001", "title": TITLE}},
    )
    conv = store.add_conversion(
        source["source_id"],
        parser="synthetic_fixture",
        parser_version="1.0",
        blocks=[
            {
                "id": "b12",
                "kind": "paragraph",
                "text": B12_TEXT,
                "locator": {"page": 1, "bbox": [30, 60, 520, 100]},
            },
            {
                "id": "b40",
                "kind": "paragraph",
                "text": B40_TEXT,
                "locator": {"page": 1, "bbox": [30, 120, 520, 200]},
            },
        ],
    )
    return {
        "tmp": tmp_path,
        "pdf": pdf,
        "store": store,
        "store_dir": store_dir,
        "review_dir": tmp_path / "mcp-review",
        "memory_dir": tmp_path / "memory",
        "source": source,
        "conversion": conv,
        "b12": f"evidence:{conv['conversion_id']}#b12",
        "b40": f"evidence:{conv['conversion_id']}#b40",
    }


def test_ledger_chain_deduplication_resolve_and_tamper_audit(env):
    before_store = dir_hashes(env["store_dir"])
    ro_store = Store(env["store_dir"], read_only=True)
    ledger = MemoryLedger(env["memory_dir"], other_roots=(env["store_dir"],))

    added = ledger.add(
        code="numbers_denominators",
        note="b40 reports expansion validation (65/100); compare b12 for primary GWAS (39/39).",
        actor="curator_alice",
        actor_kind="human",
        citation=env["b40"],
        sentence_range=(1, 2),
        related_citations=[env["b12"]],
        store=ro_store,
    )
    assert added["deduplicated"] is False and added["sequence"] == 1
    assert added["document_id"] == "paper_mem_test" and added["role"] == "primary"
    assert len(added["sentence_sha256s"]) == 2

    dup = ledger.add(
        code="numbers_denominators",
        note="b40 reports expansion validation (65/100); compare b12 for primary GWAS (39/39).",
        actor="curator_bob",
        actor_kind="human",
        citation=env["b40"],
        sentence_range=(1, 2),
        related_citations=[env["b12"]],
        store=ro_store,
    )
    assert dup["deduplicated"] is True and dup["memory_id"] == added["memory_id"]
    assert ledger.audit() == {
        "ok": True,
        "errors": [],
        "head": {"sequence": 1, "memory_id": added["memory_id"]},
        "counts": {"events": 1, "active": 1},
        "scientifically_verified": False,
        "identity_assurance": "self_declared_local",
    }

    resolved = ledger.resolve(
        added["memory_id"],
        reason="Superseded by explicit protocol table check.",
        actor="curator_alice",
        actor_kind="human",
    )
    assert resolved["sequence"] == 2 and resolved["previous_memory_id"] == added["memory_id"]
    listed = ledger.list_notes(include_history=True)
    assert listed["active_count"] == 0 and len(listed["events"]) == 2

    with pytest.raises(EvidenceError, match="already resolved"):
        ledger.resolve(added["memory_id"], reason="Again", actor="curator_alice", actor_kind="human")

    assert dir_hashes(env["store_dir"]) == before_store

    orphan = env["memory_dir"] / "records" / "stray.json"
    orphan.write_text("{}", encoding="utf-8")
    bad_audit = ledger.audit()
    assert bad_audit["ok"] is False and bad_audit["errors"]


def test_cli_memory_dispatch_has_zero_side_effects_on_absent_store(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    absent_mem = tmp_path / "absent-memory"
    assert cli.main(["memory", "--memory-db", str(absent_mem), "list"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["active_count"] == 0 and out["head"] is None
    assert cli.main(["memory", "--memory-db", str(absent_mem), "audit"]) == 0
    capsys.readouterr()
    assert not (tmp_path / "data").exists()
    assert not absent_mem.exists()


def test_concurrent_multiprocess_writes_preserve_linear_chain(tmp_path: Path):
    mem_dir = tmp_path / "concurrent-mem"
    worker = (
        "import sys; from pathlib import Path; from evidence_database.memory import MemoryLedger; "
        "l = MemoryLedger(Path(sys.argv[1])); "
        "[l.add(code='causal_strength', note=f'Worker {sys.argv[2]} item {i}', "
        "actor='w', actor_kind='human', document_id=f'doc_{sys.argv[2]}_{i}') for i in range(5)]"
    )
    p1 = subprocess.Popen([sys.executable, "-c", worker, str(mem_dir), "A"])
    p2 = subprocess.Popen([sys.executable, "-c", worker, str(mem_dir), "B"])
    assert p1.wait(timeout=30) == 0 and p2.wait(timeout=30) == 0
    audit = MemoryLedger(mem_dir, read_only=True).audit()
    assert audit["ok"] is True
    assert audit["counts"] == {"events": 10, "active": 10}


def test_stateless_quote_hazards_and_long_block_safety():
    defective = {
        "id": "b_def",
        "kind": "paragraph",
        "text": "Effect was \x0533.33 in vari- ant carriers with p = 10-8.",
        "superscripts": [[49, 51]],
    }
    hazards = compute_quote_hazards(defective, text_truncated=False)
    assert hazards == {
        "codes": ["control_character", "line_end_hyphenation", "superscript_position_lost"],
        "prefer_sentence_positions": True,
    }
    long_block = {"id": "b_long", "kind": "paragraph", "text": "vari- ant " + ("x " * (MAX_TEXT // 2 + 50))}
    assert len(long_block["text"]) > MAX_TEXT
    long_hazards = compute_quote_hazards(long_block, text_truncated=len(long_block["text"]) > MAX_TEXT)
    assert long_hazards == {"codes": ["line_end_hyphenation"], "prefer_sentence_positions": False}
    clean = {"id": "b_ok", "kind": "paragraph", "text": "Clean ordinary sentence without typography defects."}
    assert compute_quote_hazards(clean, text_truncated=False) == {
        "codes": [],
        "prefer_sentence_positions": False,
    }


def test_sentence_intersection_cross_conversion_and_role_isolation(env):
    supp_src = env["store"].ingest(env["pdf"], "paper_mem_test", role="supplement")
    supp_conv = env["store"].add_conversion(
        supp_src["source_id"],
        parser="synthetic_fixture",
        parser_version="1.0",
        blocks=[{"id": "s1", "kind": "paragraph", "text": B40_TEXT, "locator": {"page": 1}}],
    )
    supp_cit = f"evidence:{supp_conv['conversion_id']}#s1"

    ro_store = Store(env["store_dir"], read_only=True)
    ledger = MemoryLedger(env["memory_dir"], other_roots=(env["store_dir"],))

    with pytest.raises(EvidenceError, match="conflicts with the citation"):
        ledger.add(
            code="numbers_denominators",
            note="Mismatched role test",
            actor="curator",
            actor_kind="human",
            citation=env["b40"],
            role="supplement",
            store=ro_store,
        )

    added = ledger.add(
        code="numbers_denominators",
        note="Check expansion 65/100 in b40 against primary GWAS 39/39 in b12.",
        actor="secret_local_username",
        actor_kind="human",
        citation=env["b40"],
        sentence_range=(1, 2),
        related_citations=[env["b12"]],
        store=ro_store,
    )

    tools = EvidenceTools(env["store_dir"], env["review_dir"], memory_root=env["memory_dir"])

    chk_hit = tools.check_quote(env["b40"], quote=B40_S2)
    assert chk_hit["quote_matches"] is True and chk_hit["admitted"] is False
    assert chk_hit["reason"] == "pending_review"
    assert len(chk_hit["memory"]["notes"]) == 1
    note = chk_hit["memory"]["notes"][0]
    assert note["memory_id"] == added["memory_id"]
    assert note["related_citations"] == [env["b12"]]
    assert note["citable"] is False and "actor" not in note

    chk_miss = tools.check_quote(env["b40"], quote=B40_S3)
    assert chk_miss["quote_matches"] is True and chk_miss["memory"]["notes"] == []

    supp_get = tools.get(supp_cit)
    assert supp_get["memory"]["notes"] == []

    conv_v2 = env["store"].add_conversion(
        env["source"]["source_id"],
        parser="synthetic_fixture",
        parser_version="2.0",
        blocks=[
            {
                "id": "b40",
                "kind": "paragraph",
                "text": f"Edited lead sentence. {B40_S2} {B40_S3}",
                "locator": {"page": 1},
            },
        ],
    )
    v2_b40 = f"evidence:{conv_v2['conversion_id']}#b40"
    v2_get = tools.get(v2_b40)
    assert len(v2_get["memory"]["notes"]) == 1
    assert v2_get["memory"]["notes"][0]["match_scope"] == "sentence_across_conversion"
    assert v2_get["memory"]["notes"][0]["recorded_citation"] == env["b40"]

    conv_v3 = env["store"].add_conversion(
        env["source"]["source_id"],
        parser="synthetic_fixture",
        parser_version="3.0",
        blocks=[
            {
                "id": "b40",
                "kind": "paragraph",
                "text": f"Completely revised paragraph. {B40_S3}",
                "locator": {"page": 1},
            }
        ],
    )
    assert tools.get(f"evidence:{conv_v3['conversion_id']}#b40")["memory"]["notes"] == []


def test_search_cursor_stability_and_corrupted_memory_graceful_degradation(env):
    conv = env["store"].add_conversion(
        env["source"]["source_id"],
        parser="synthetic_fixture",
        parser_version="search-v1",
        blocks=[
            {
                "id": "p1",
                "kind": "paragraph",
                "text": "Shared cohort marker alpha in first block.",
                "locator": {"page": 1},
            },
            {
                "id": "p2",
                "kind": "paragraph",
                "text": "Shared cohort marker beta in second block.",
                "locator": {"page": 1},
            },
        ],
    )
    c1, c2 = f"evidence:{conv['conversion_id']}#p1", f"evidence:{conv['conversion_id']}#p2"
    tools = EvidenceTools(
        env["store_dir"],
        env["review_dir"],
        selected_versions={"paper_mem_test": conv["conversion_id"]},
        memory_root=env["memory_dir"],
    )
    page1 = tools.search("Shared cohort marker", limit=1)
    assert page1["returned"] == 1 and page1["next_cursor"] is not None
    assert page1["memory_status"] == "ok" and page1["memory_notes"] == []

    ro_store = Store(env["store_dir"], read_only=True)
    ledger = MemoryLedger(env["memory_dir"], other_roots=(env["store_dir"],))
    note = ledger.add(
        code="direction_comparator",
        note="Verify direction comparator in p2.",
        actor="curator",
        actor_kind="human",
        citation=c2,
        store=ro_store,
    )
    page2 = tools.search("Shared cohort marker", limit=1, cursor=page1["next_cursor"])
    assert page2["returned"] == 1 and page2["items"][0]["citation"] == c2
    assert page2["items"][0]["memory_note_ids"] == [note["memory_id"]]
    assert [n["memory_id"] for n in page2["memory_notes"]] == [note["memory_id"]]

    record_file = next((env["memory_dir"] / "records").glob("mem_*.json"))
    record_file.write_text('{"corrupted": true}', encoding="utf-8")
    degraded = tools.get(c1)
    assert degraded["text"].startswith("Shared cohort marker")
    assert degraded["memory"]["status"] == "unavailable"
    assert "memory_ledger_unavailable" in degraded["warnings"]


def test_security_redaction_and_project_config_isolation(env):
    ledger = MemoryLedger(env["memory_dir"], other_roots=(env["store_dir"],))
    parts = ("audit_user", "secret.json")
    unix_path = "/" + "/".join(("home", *parts))
    win_path = "C:" + chr(92) + chr(92).join(("Users", *parts))
    for bad_note in (
        f"See local file {unix_path} for details.",
        f"Windows path {win_path} leaked.",
        "Control char \x07 inside note.",
    ):
        with pytest.raises(EvidenceError):
            ledger.add(
                code="scope_context", note=bad_note, actor="curator", actor_kind="human", document_id="doc1"
            )

    base_cfg = {
        "schema": 1,
        "project_id": "mem-config-test",
        "store": "store",
        "queue": "queue",
        "inbox": "inbox",
        "runs": "runs",
        "papers": [{"metadata": {"doi": DOI, "title": TITLE}}],
    }
    cfg_path = env["tmp"] / "project.json"
    cfg_path.write_text(json.dumps(base_cfg), encoding="utf-8")
    loaded_default = load_project(cfg_path)
    assert loaded_default["memory"] is None

    cfg_nested = {**base_cfg, "memory": "runs/memory"}
    cfg_path.write_text(json.dumps(cfg_nested), encoding="utf-8")
    with pytest.raises(EvidenceError, match="separate, non-nested"):
        load_project(cfg_path)

    cfg_valid = {**base_cfg, "memory": "memory"}
    cfg_path.write_text(json.dumps(cfg_valid), encoding="utf-8")
    loaded_valid = load_project(cfg_path)
    assert loaded_valid["memory"] == env["memory_dir"].resolve()
    assert mcp_server.main(["--project-config", str(cfg_path), "--describe"]) == 0


def test_check_quote_preserves_native_store_check_keys_for_validator_bridge(env):
    packet = create_packet(env["store"], env["b40"], env["tmp"] / "packet-0")
    env["store"].review(
        env["b40"],
        B40_S1,
        decision="approve",
        reviewer="SYNTHETIC TEST",
        reviewer_kind="human",
        note="Synthetic page review",
        viewed_original=True,
        packet_id=packet["packet_id"],
    )

    ro_store = Store(env["store_dir"], read_only=True)
    ledger = MemoryLedger(env["memory_dir"], other_roots=(env["store_dir"],))
    ledger.add(
        code="numbers_denominators",
        note="Verify expansion cohort denominator before generalizing.",
        actor="curator",
        actor_kind="human",
        citation=env["b40"],
        quote=B40_S1,
        store=ro_store,
    )

    before_hashes = dir_hashes(env["store_dir"])
    tools = EvidenceTools(env["store_dir"], env["review_dir"], memory_root=env["memory_dir"])
    local_check = ro_store.check(env["b40"], B40_S1, require_review=True)
    mcp_check = tools.check_quote(env["b40"], quote=B40_S1)

    # Every admission/comparison key inspected by Validator's MCP bridge remains identical.
    for key in ("quote", "quote_matches", "review_status", "review_id", "admitted", "reason",
                "admission_route", "scientifically_verified"):
        assert mcp_check[key] == local_check[key]
    assert mcp_check["admitted"] is True
    assert len(mcp_check["memory"]["notes"]) == 1
    assert "quote_hazards" in mcp_check
    assert dir_hashes(env["store_dir"]) == before_hashes


def test_cli_round_trip_review_link_resource_uri_and_symlink_guard(env, capsys):
    rv = env["store"].review(
        env["b40"],
        B40_S1,
        decision="reject",
        reviewer="SYNTHETIC CURATOR",
        reviewer_kind="human",
        note="Expansion cohort misattributed as primary GWAS",
    )
    before_store = dir_hashes(env["store_dir"])

    rc_bad = cli.main([
        "--db", str(env["store_dir"]),
        "memory", "--memory-db", str(env["memory_dir"]),
        "add", "--code", "numbers_denominators", "--note", "Should fail",
        "--actor", "curator", "--actor-kind", "human",
        "--citation", env["b40"], "--no-store-check",
    ])
    assert rc_bad == 1
    capsys.readouterr()

    rc_add = cli.main([
        "--db", str(env["store_dir"]),
        "memory", "--memory-db", str(env["memory_dir"]),
        "add",
        "--citation", env["b40"],
        "--sentences", "1-2",
        "--related-citation", env["b12"],
        "--review-id", rv["review_id"],
        "--code", "numbers_denominators",
        "--actor", "curator",
        "--actor-kind", "human",
        "--note", "Verify cohort stage against b12 before attributing 65/100 to GWAS.",
    ])
    assert rc_add == 0
    added = json.loads(capsys.readouterr().out)
    assert added["review_id"] == rv["review_id"]

    tools = EvidenceTools(env["store_dir"], env["review_dir"], memory_root=env["memory_dir"])
    uri = f"evidence://blocks/{env['conversion']['conversion_id']}/b40"
    res = json.loads(tools.read_resource(uri)["text"])
    assert res["memory"]["notes"][0]["memory_id"] == added["memory_id"]
    assert "quote_hazards" in res

    rc_list = cli.main([
        "memory", "--memory-db", str(env["memory_dir"]),
        "list", "--document-id", "paper_mem_test", "--code", "numbers_denominators",
    ])
    assert rc_list == 0
    assert json.loads(capsys.readouterr().out)["active_count"] == 1

    rc_res = cli.main([
        "memory", "--memory-db", str(env["memory_dir"]),
        "resolve", "--id", added["memory_id"],
        "--reason", "Resolved in updated protocol", "--actor", "curator", "--actor-kind", "human",
    ])
    assert rc_res == 0
    capsys.readouterr()

    rc_audit = cli.main(["memory", "--memory-db", str(env["memory_dir"]), "audit"])
    assert rc_audit == 0
    audit_out = json.loads(capsys.readouterr().out)
    assert audit_out["ok"] is True and audit_out["counts"] == {"events": 2, "active": 0}
    assert dir_hashes(env["store_dir"]) == before_store

    link = env["tmp"] / "mem-symlink"
    link.symlink_to(env["memory_dir"], target_is_directory=True)
    with pytest.raises(EvidenceError, match="symbolic link"):
        EvidenceTools(env["store_dir"], env["review_dir"], memory_root=link)


def test_scan_cli_pinpoints_ocr_disagreements_and_defective_sentences(env, capsys):
    s_clean = "Baseline demographics were balanced across study arms."
    s_typo = "Prior work reported similar kinetics12."
    s_ocr_bad = "Mean change was 33.33 units in the treated cohort."
    s_ctrl = "Control arm delta was \x0512.5 units at week 8."
    p_text = f"{s_clean} {s_typo} {s_ocr_bad} {s_ctrl}"
    sup_start = p_text.index("12.")
    ocr_text = (
        f"{s_clean} {s_typo} Mean change was -33.33 units in the treated cohort. "
        "Control arm delta was -12.5 units at week 8."
    )
    conv = env["store"].add_conversion(
        env["source"]["source_id"],
        parser="synthetic_ocr_fixture",
        parser_version="1.0",
        blocks=[
            {
                "id": "b_scan",
                "kind": "paragraph",
                "text": p_text,
                "locator": {"page": 1, "bbox": [30, 30, 500, 120]},
                "superscripts": [[sup_start, sup_start + 2]],
                "symbol_font_spans": [],
                "independent_reading": {"engine": "tesseract", "engine_version": "5.3", "text": ocr_text},
            }
        ],
    )
    before_store = dir_hashes(env["store_dir"])

    rc = cli.main(["--db", str(env["store_dir"]), "scan", "--conversion", conv["conversion_id"]])
    assert rc == 2
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["sentences_scanned"] == 4
    assert report["summary"]["ocr_agreed_sentences"] == 1
    assert report["summary"]["high_severity_sentences"] == 2
    assert report["summary"]["typography_only_sentences"] == 1
    assert [(r["sentence"], r["codes"]) for r in report["flagged_sentences"]] == [
        (3, ["independent_reading_differs"]),
        (4, ["control_character", "independent_reading_differs"]),
    ]

    rc_typo = cli.main([
        "--db", str(env["store_dir"]),
        "scan", "--conversion", conv["conversion_id"], "--include-typography",
    ])
    assert rc_typo == 2
    report_typo = json.loads(capsys.readouterr().out)
    assert len(report_typo["flagged_sentences"]) == 3
    assert report_typo["flagged_sentences"][-1]["sentence"] == 2
    assert report_typo["flagged_sentences"][-1]["severity"] == "medium"

    rc_clean = cli.main([
        "--db", str(env["store_dir"]), "scan", "--conversion", env["conversion"]["conversion_id"],
    ])
    assert rc_clean == 0
    capsys.readouterr()
    assert dir_hashes(env["store_dir"]) == before_store


def test_import_validator_cli_from_validate_report_and_raw_receipt(env, capsys):
    cv_id = env["conversion"]["conversion_id"]
    anchors = [
        {"source": "S1", "block_id": "b40", "quote": B40_S1},
        {"source": "S1", "block_id": "b12", "quote": B12_TEXT},
    ]
    facet = {
        "verdict": "contradicted",
        "claim_fact": "Primary GWAS had 65 cases and 100 controls.",
        "source_fact": "Primary GWAS had 39/39 (b12); 65/100 in b40 is expansion validation.",
        "population": "cases and controls",
        "comparator": "primary GWAS vs expansion",
        "reason": "Expansion cohort sample size attributed to primary GWAS.",
        "evidence": anchors,
    }
    na_facet = {
        "verdict": "not_applicable",
        "claim_fact": "N/A",
        "source_fact": "N/A",
        "population": "N/A",
        "comparator": "N/A",
        "reason": "Sample size claim only.",
        "evidence": [],
    }
    val_report = {
        "overall_status": "review_required",
        "semantic_review": {
            "contract_version": 1,
            "status": "review_required",
            "assessment_valid": True,
            "scientifically_verified": False,
            "checks": {
                "numbers_denominators": facet,
                "direction_comparator": na_facet,
                "causal_strength": na_facet,
            },
            "reviewed_quotes": [
                {"source": "S1", "block_id": "b40", "quote": B40_S1, "citation": env["b40"], "quote_matches": True},
                {"source": "S1", "block_id": "b12", "quote": B12_TEXT, "citation": env["b12"], "quote_matches": True},
            ],
        },
    }
    report_file = env["tmp"] / "val-report.json"
    report_file.write_text(json.dumps(val_report), encoding="utf-8")

    rc_dry = cli.main([
        "--db", str(env["store_dir"]),
        "memory", "--memory-db", str(env["memory_dir"]),
        "import-validator", str(report_file), "--dry-run",
    ])
    assert rc_dry == 0
    dry_out = json.loads(capsys.readouterr().out)
    assert dry_out["dry_run"] is True and dry_out["candidates_found"] == 1
    assert dry_out["preview"][0]["related_citations"] == [env["b12"]]
    assert not env["memory_dir"].exists()

    rc_imp = cli.main([
        "--db", str(env["store_dir"]),
        "memory", "--memory-db", str(env["memory_dir"]),
        "import-validator", str(report_file),
    ])
    assert rc_imp == 0
    imp_out = json.loads(capsys.readouterr().out)
    assert imp_out["added_count"] == 1 and imp_out["deduplicated_count"] == 0

    rc_imp2 = cli.main([
        "--db", str(env["store_dir"]),
        "memory", "--memory-db", str(env["memory_dir"]),
        "import-validator", str(report_file),
    ])
    assert rc_imp2 == 0
    imp_out2 = json.loads(capsys.readouterr().out)
    assert imp_out2["added_count"] == 0 and imp_out2["deduplicated_count"] == 1

    tools = EvidenceTools(env["store_dir"], env["review_dir"], memory_root=env["memory_dir"])
    chk = tools.check_quote(env["b40"], quote=B40_S1)
    assert len(chk["memory"]["notes"]) == 1
    assert chk["memory"]["notes"][0]["code"] == "numbers_denominators"
    assert chk["memory"]["notes"][0]["related_citations"] == [env["b12"]]

    req_file = env["tmp"] / "sem-req.json"
    rec_file = env["tmp"] / "sem-receipt.json"
    req_file.write_text(json.dumps({"sources": [{"id": "S1", "conversion_id": cv_id}]}), encoding="utf-8")
    rec_file.write_text(
        json.dumps({
            "contract_version": 1,
            "response": {"results": [{"id": "C-1", "checks": val_report["semantic_review"]["checks"]}]},
        }),
        encoding="utf-8",
    )
    mem_dir_2 = env["tmp"] / "memory-from-receipt"
    rc_rec = cli.main([
        "--db", str(env["store_dir"]),
        "memory", "--memory-db", str(mem_dir_2),
        "import-validator", str(rec_file), "--request", str(req_file),
    ])
    assert rc_rec == 0
    assert json.loads(capsys.readouterr().out)["added_count"] == 1
