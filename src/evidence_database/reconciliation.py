"""Backward-compatible alias module forwarding to evidport.reconciliation."""
from __future__ import annotations

import importlib as _importlib
import sys as _sys

_target = _importlib.import_module("evidport.reconciliation")
if __name__ == "__main__" and hasattr(_target, "main"):
    raise SystemExit(_target.main())
_sys.modules[__name__] = _target
if __package__ in _sys.modules:
    _sys.modules[__package__].reconciliation = _target
