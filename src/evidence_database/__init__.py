"""Backward-compatible imports for the evidport package."""
import sys as _sys
from importlib import import_module as _import_module

from evidport import __version__

_SUBMODULES = (
    "acquisition", "adapters", "cli", "literature", "mcp_evidence", "mcp_jobs",
    "mcp_locks", "mcp_papers", "mcp_server", "memory", "packets", "paper_cli",
    "quoting", "reconciliation", "store", "triage", "verification", "workflow",
)
__all__ = ["__version__"]
__all__.extend(_SUBMODULES)


def __getattr__(name):
    if name not in _SUBMODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = _import_module(f"evidport.{name}")
    _sys.modules[f"{__name__}.{name}"] = module
    globals()[name] = module
    return module
