"""Backward-compatible package alias forwarding directly to evidport."""
from __future__ import annotations

import importlib as _importlib
import sys as _sys

_target = _importlib.import_module("evidport")
_sys.modules[__name__] = _target
