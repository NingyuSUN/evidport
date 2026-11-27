"""Local, versioned evidence storage. Review is not scientific validation."""
from __future__ import annotations

import importlib as _importlib
import importlib.abc as _abc
import importlib.util as _util
import sys as _sys

__version__ = "0.1.0"

_SUBMODULES = (
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
__all__ = [
    "__version__",
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
]


def _canonical_name(fullname: str) -> str:
    if fullname == "evidence_database":
        return "evidport"
    if fullname.startswith("evidence_database."):
        return "evidport." + fullname[len("evidence_database."):]
    return fullname


def _alias_name(fullname: str) -> str | None:
    if fullname == "evidport":
        return "evidence_database"
    if fullname.startswith("evidport."):
        return "evidence_database." + fullname[len("evidport."):]
    return None


class _DualNameLoader:
    """Delegate loader operations while accepting both evidport.* and evidence_database.* names."""

    def __init__(self, loader):
        self._loader = loader

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        alias = _alias_name(module.__name__)
        if alias is not None:
            _sys.modules[alias] = module

    def get_code(self, fullname):
        return self._loader.get_code(_canonical_name(fullname))

    def get_source(self, fullname):
        return self._loader.get_source(_canonical_name(fullname))

    def get_filename(self, fullname):
        return self._loader.get_filename(_canonical_name(fullname))

    def is_package(self, fullname):
        return self._loader.is_package(_canonical_name(fullname))

    def __getattr__(self, attr):
        return getattr(self._loader, attr)


def _ensure_dual_loader(module):
    spec = getattr(module, "__spec__", None)
    if spec is not None and spec.loader is not None and not isinstance(spec.loader, _DualNameLoader):
        spec.loader = _DualNameLoader(spec.loader)
    return module


class _AliasShimLoader(_abc.Loader):
    def __init__(self, canonical: str, dual_loader: _DualNameLoader):
        self._canonical = canonical
        self._dual_loader = dual_loader

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        target = _ensure_dual_loader(_importlib.import_module(self._canonical))
        _sys.modules[module.__name__] = target
        if "." in module.__name__:
            parent_name, attr = module.__name__.rsplit(".", 1)
            parent = _sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, attr, target)

    def get_code(self, fullname):
        return self._dual_loader.get_code(fullname)

    def get_source(self, fullname):
        return self._dual_loader.get_source(fullname)

    def get_filename(self, fullname):
        return self._dual_loader.get_filename(fullname)

    def is_package(self, fullname):
        return self._dual_loader.is_package(fullname)


class _EvidPortCompatFinder(_abc.MetaPathFinder):
    def __init__(self) -> None:
        self._active: set[str] = set()

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self._active:
            return None
        if fullname == "evidence_database" or fullname.startswith("evidence_database."):
            canonical = _canonical_name(fullname)
            self._active.add(fullname)
            try:
                c_spec = _util.find_spec(canonical)
            except ModuleNotFoundError as exc:
                if exc.name == canonical or (exc.name and canonical.startswith(exc.name + ".")):
                    return None
                raise
            finally:
                self._active.discard(fullname)
            if c_spec is None or c_spec.loader is None:
                return None
            dual = (
                c_spec.loader
                if isinstance(c_spec.loader, _DualNameLoader)
                else _DualNameLoader(c_spec.loader)
            )
            is_pkg = c_spec.submodule_search_locations is not None
            spec = _util.spec_from_loader(
                fullname,
                _AliasShimLoader(canonical, dual),
                origin=c_spec.origin,
                is_package=is_pkg,
            )
            if is_pkg:
                spec.submodule_search_locations = list(c_spec.submodule_search_locations)
            return spec
        if fullname.startswith("evidport."):
            self._active.add(fullname)
            try:
                spec = _util.find_spec(fullname)
            finally:
                self._active.discard(fullname)
            if spec is not None and spec.loader is not None and not isinstance(spec.loader, _DualNameLoader):
                spec.loader = _DualNameLoader(spec.loader)
            return spec
        return None


def _install_compat() -> None:
    if __name__ != "evidport":
        return
    me = _ensure_dual_loader(_sys.modules["evidport"])
    _sys.modules["evidence_database"] = me
    if not any(type(f).__name__ == "_EvidPortCompatFinder" for f in _sys.meta_path):
        _sys.meta_path.insert(0, _EvidPortCompatFinder())


_install_compat()


def __getattr__(name: str):
    if name not in _SUBMODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = _ensure_dual_loader(_importlib.import_module(f"evidport.{name}"))
    _sys.modules[f"evidence_database.{name}"] = module
    globals()[name] = module
    return module
