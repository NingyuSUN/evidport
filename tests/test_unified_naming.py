"""Both public names must reach the same code, in Python and on the command line."""
import importlib
import shutil
import subprocess
import sys
import sysconfig
from unittest.mock import Mock

import pytest

import evidence_database
import evidport


def test_version_and_store_types():
    from evidport.store import EvidenceError, Store

    assert evidence_database.__version__ == evidport.__version__
    assert evidence_database.store.Store is Store
    assert evidence_database.store.EvidenceError is EvidenceError


@pytest.mark.parametrize("name", [
    "acquisition", "adapters", "cli", "literature", "mcp_evidence", "mcp_jobs", "mcp_locks",
    "mcp_papers", "mcp_server", "memory", "packets", "paper_cli", "quoting", "reconciliation",
    "store", "triage", "verification", "workflow",
])
def test_submodule_identity(name):
    canonical = importlib.import_module(f"evidport.{name}")
    assert importlib.import_module(f"evidence_database.{name}") is canonical
    assert getattr(evidence_database, name) is getattr(evidport, name) is canonical
    assert sys.modules[f"evidence_database.{name}"] is canonical


def test_lazy_package_attribute_in_fresh_interpreter():
    subprocess.run([sys.executable, "-c", """
import sys
import evidence_database
assert "evidence_database.store" not in sys.modules
store = evidence_database.store
import evidport.store
assert sys.modules["evidence_database.store"] is store is evidport.store
from evidence_database import *
assert acquisition is sys.modules["evidport.acquisition"]
"""], check=True)


@pytest.mark.parametrize(("patched", "consumer"), [
    ("evidence_database", "evidport"), ("evidport", "evidence_database"),
])
def test_patching_doi_resolver_reaches_acquisition(patched, consumer, monkeypatch, tmp_path):
    literature = importlib.import_module(f"{patched}.literature")
    acquisition = importlib.import_module(f"{consumer}.acquisition")
    doi = "10.9999/synthetic.alias"
    resolver = Mock(return_value={"doi": doi, "title": "Synthetic alias test"})
    monkeypatch.setattr(literature, "resolve_doi", resolver)
    assert importlib.import_module(f"{consumer}.literature").resolve_doi is resolver

    queue = acquisition.AcquisitionQueue(tmp_path / "queue", evidport.store.Store(tmp_path / "store"))
    paper, = queue.register([{"doi": doi}], provenance={})["papers"]
    state = queue.fetch(paper["paper_id"])
    resolver.assert_called_once_with(doi)
    assert state["metadata"]["title"] == "Synthetic alias test"


@pytest.mark.parametrize(("name", "option"), [
    ("evidport", "--db"), ("evidence-db", "--db"), ("evidport-mcp", "--project-config"),
])
def test_installed_script_help(name, option):
    script = shutil.which(name, path=sysconfig.get_path("scripts"))
    assert script is not None, f"{name} is missing from the Python environment"
    help_text = subprocess.check_output([script, "--help"], text=True)
    assert option in help_text


@pytest.mark.parametrize(("module", "option"), [
    ("evidport", "--db"), ("evidence_database", "--db"),
    ("evidport.cli", "--db"), ("evidence_database.cli", "--db"),
    ("evidport.mcp_server", "--project-config"), ("evidence_database.mcp_server", "--project-config"),
])
def test_python_module_help(module, option):
    help_text = subprocess.check_output([sys.executable, "-m", module, "--help"], text=True)
    assert option in help_text
