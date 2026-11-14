"""Load ``scripts/homeant.py`` by path — once, for verify.py and conftest.py.

The CLI is a script, not a package, so there is no import name to write down.
Two callers need it (the acceptance cases here, the pytest fixtures in
conftest), and two copies of this loader would drift the day one of them
changed — the same failure mode D16 exists to prevent.

Nothing here touches ``~/.homeant`` or ``homeant.cfg``: loading the module only
executes constants plus function and class definitions, and every side effect
sits behind ``if __name__`` or inside a function.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

from gate_paths import REPO_ROOT

CLI_NAME = "homeant_cli"
CLI_PATH = REPO_ROOT / "scripts" / "homeant.py"

# Session cache: re-executing the CLI per test is correct but wasteful, and
# every caller that patches a global is expected to restore it.
_loaded: dict[str, types.ModuleType] = {}


def load_by_path(name: str, path: Path) -> types.ModuleType:
    """Import a single .py file under ``name``, registering it in sys.modules.

    Registering matters: a module loaded by hand is not visible to
    ``import <name>`` or to pickling unless it is in sys.modules.
    """
    cached = _loaded.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - bad path
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    _loaded[name] = module
    return module


def cli() -> types.ModuleType:
    """``scripts/homeant.py`` as a module (``homeant_cli``)."""
    return load_by_path(CLI_NAME, CLI_PATH)
