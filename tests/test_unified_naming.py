"""Both public names must reach the same code, in Python and on the command line."""
from __future__ import annotations

import importlib
import runpy
import shutil
import subprocess
import sys
import sysconfig
import warnings
from unittest.mock import Mock

import pytest

import evidence_database
import evidport

SUBMODULES = (
    "acquisition",
    "adapters",
    "cli",
    "literature",
    "mcp_evidence",
    "mcp_jobs",
    "mcp_locks",
    "mcp_papers",
    "mcp_server",
    "memory",
    "packets",
    "paper_cli",
    "quoting",
    "reconciliation",
    "store",
    "triage",
    "verification",
    "workflow",
)


def test_top_level_package_and_store_type_identity():
    from evidport.store import EvidenceError, Store

    assert evidence_database is evidport
    assert sys.modules["evidence_database"] is sys.modules["evidport"] is evidport
    assert evidence_database.__version__ == evidport.__version__
    assert evidence_database.store.Store is Store
    assert evidence_database.store.EvidenceError is EvidenceError


@pytest.mark.parametrize(
    "import_stmt",
    [
        "import evidence_database, evidport",
        "import evidport, evidence_database",
    ],
)
def test_top_level_identity_in_both_import_orders(import_stmt):
    code = (
        f"{import_stmt}; assert evidence_database is evidport; "
        "from evidence_database import cli; import runpy, warnings; "
        "warnings.simplefilter('ignore', RuntimeWarning); "
        "ns = runpy.run_module('evidence_database.cli', run_name='__not_main__'); "
        "assert callable(ns['main'])"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize("name", SUBMODULES)
def test_submodule_identity(name):
    canonical = importlib.import_module(f"evidport.{name}")
    assert importlib.import_module(f"evidence_database.{name}") is canonical
    assert getattr(evidence_database, name) is getattr(evidport, name) is canonical
    assert sys.modules[f"evidence_database.{name}"] is canonical


def test_runpy_cli_without_alter_sys_after_import():
    from evidence_database import cli, mcp_server

    assert cli is evidport.cli and mcp_server is evidport.mcp_server
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        cli_ns = runpy.run_module("evidence_database.cli", run_name="__not_main__")
        mcp_ns = runpy.run_module("evidence_database.mcp_server", run_name="__not_main__")
    assert callable(cli_ns["main"]) and callable(mcp_ns["main"])


@pytest.mark.parametrize("name", SUBMODULES)
def test_runpy_run_module_works_after_import(name):
    imported = getattr(evidence_database, name)
    assert imported is getattr(evidport, name)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            alias_ns = runpy.run_module(
                f"evidence_database.{name}", run_name="__not_main__", alter_sys=True
            )
            canon_ns = runpy.run_module(
                f"evidport.{name}", run_name="__not_main__", alter_sys=True
            )
    finally:
        sys.modules.pop("__not_main__", None)
    assert alias_ns["__name__"] == "__not_main__"
    assert canon_ns["__name__"] == "__not_main__"
    if hasattr(imported, "main"):
        assert callable(alias_ns["main"]) and callable(canon_ns["main"])


@pytest.mark.parametrize(
    ("module_name", "option"),
    [
        ("evidence_database.cli", "--db"),
        ("evidence_database.mcp_server", "--project-config"),
        ("evidence_database", "--db"),
    ],
)
def test_runpy_main_execution_before_and_after_import(module_name, option, monkeypatch, capsys):
    # 1. Fresh interpreter via runpy without prior submodule import (strict -W error).
    code = (
        "import runpy, sys; "
        f"sys.argv = [{module_name!r}, '--help']; "
        f"runpy.run_module({module_name!r}, run_name='__main__', alter_sys=True)"
    )
    out = subprocess.check_output([sys.executable, "-W", "error", "-c", code], text=True)
    assert option in out

    # 2. In-process after importing via evidence_database.
    importlib.import_module(module_name)
    monkeypatch.setattr(sys, "argv", [module_name, "--help"])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with pytest.raises(SystemExit) as exc:
            runpy.run_module(module_name, run_name="__main__", alter_sys=True)
    assert exc.value.code == 0
    assert option in capsys.readouterr().out


def test_lazy_package_attribute_in_fresh_interpreter():
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
import evidence_database
import evidport
assert evidence_database is evidport
assert "evidence_database.store" not in sys.modules
store = evidence_database.store
import evidport.store
assert sys.modules["evidence_database.store"] is store is evidport.store
from evidence_database import *
assert acquisition is sys.modules["evidport.acquisition"]
""",
        ],
        check=True,
    )


@pytest.mark.parametrize(
    ("patched", "consumer"),
    [
        ("evidence_database", "evidport"),
        ("evidport", "evidence_database"),
    ],
)
def test_patching_doi_resolver_reaches_acquisition(patched, consumer, monkeypatch, tmp_path):
    literature = importlib.import_module(f"{patched}.literature")
    acquisition = importlib.import_module(f"{consumer}.acquisition")
    doi = "10.9999/synthetic.alias"
    resolver = Mock(return_value={"doi": doi, "title": "Synthetic alias test"})
    monkeypatch.setattr(literature, "resolve_doi", resolver)
    assert importlib.import_module(f"{consumer}.literature").resolve_doi is resolver

    queue = acquisition.AcquisitionQueue(tmp_path / "queue", evidport.store.Store(tmp_path / "store"))
    (paper,) = queue.register([{"doi": doi}], provenance={})["papers"]
    state = queue.fetch(paper["paper_id"])
    resolver.assert_called_once_with(doi)
    assert state["metadata"]["title"] == "Synthetic alias test"


@pytest.mark.parametrize(
    ("name", "option"),
    [
        ("evidport", "--db"),
        ("evidence-db", "--db"),
        ("evidport-mcp", "--project-config"),
    ],
)
def test_installed_script_help(name, option):
    script = shutil.which(name, path=sysconfig.get_path("scripts"))
    assert script is not None, f"{name} is missing from the Python environment"
    help_text = subprocess.check_output([script, "--help"], text=True)
    assert option in help_text


@pytest.mark.parametrize(
    ("module", "option"),
    [
        ("evidport", "--db"),
        ("evidence_database", "--db"),
        ("evidport.cli", "--db"),
        ("evidence_database.cli", "--db"),
        ("evidport.mcp_server", "--project-config"),
        ("evidence_database.mcp_server", "--project-config"),
    ],
)
def test_python_module_help(module, option):
    help_text = subprocess.check_output([sys.executable, "-m", module, "--help"], text=True)
    assert option in help_text
