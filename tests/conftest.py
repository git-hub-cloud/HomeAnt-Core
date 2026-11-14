"""Shared fixtures for the HomeAnt test suite.

Two jobs, both in service of one rule: a test must never start Home Assistant.

* ``cli`` — load ``scripts/homeant.py`` by path.  It is a script, not a package,
  so there is no import name to write down; the loader below is the only way a
  test can reach ``load_cfg``, ``_merge_file``, ``_WSConn`` and friends.
  Loading it is safe and cheap: its top level is constants plus function and
  class definitions, and every side effect sits behind ``if __name__`` or inside
  a function.

* ``ha_stubs`` — put fake ``homeassistant.*`` modules into ``sys.modules`` so the
  custom components under ``config/custom_components/`` can be imported without
  HA installed.  They import HA at module scope, which means the stub has to be
  in place *before* the import — patching afterwards is too late.

This directory lives in ``tests/`` inside the repo, but the depth is not a
contract: the repo root comes from ``gate_paths`` rather than ``parents[1]``.
The sys.path insert below is deliberate: pytest's import mode decides whether
a sibling module is importable by name, and the gate must not depend on that.

Nothing here touches ``~/.homeant``, ``homeant.cfg`` or a socket.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_GATE_DIR = Path(__file__).resolve().parent
if str(_GATE_DIR) not in sys.path:
    sys.path.insert(0, str(_GATE_DIR))

from gate_cli import CLI_PATH, cli as load_cli  # noqa: E402 - after the insert
from gate_ha import STUB_NAMES, load_component, make_stub  # noqa: E402
from gate_paths import REPO_ROOT  # noqa: E402


@pytest.fixture(scope="session")
def cli() -> types.ModuleType:
    """``scripts/homeant.py`` as a module (``homeant_cli``).

    The loader lives in gate_cli so the verify.py cases and these fixtures
    cannot drift into two different ideas of "load the CLI".
    """
    return load_cli()


# ── component loader ────────────────────────────────────────────────────────

# The two components live under config/custom_components and import HA at module
# scope, so the stubs have to be in place *before* the module is executed.
# ``gate_ha`` owns that (and owns the stub implementation, so a pytest test and
# a verify.py case cannot stub two different worlds).


@pytest.fixture(scope="session")
def component():
    """Load ``config/custom_components/<name>/__init__.py`` under HA stubs.

    Session-scoped and cached: re-executing the component per test would be
    correct but wasteful, and every test that patches a module global is
    expected to restore it (``monkeypatch`` does).

    ``gate_ha`` installs the stubs itself, process-wide, because the import has
    to see them *before* the module is executed — a monkeypatched-only set would
    be rolled back after the first test and the cached module would then be the
    only thing left standing.
    """

    def load(name: str) -> types.ModuleType:
        return load_component(name)

    return load


# ── homeassistant stubs ─────────────────────────────────────────────────────

# One list, in gate_ha: the components' import surface is the same thing the
# loader has to satisfy, and two lists would drift.
HA_DEFAULT_MODULES = STUB_NAMES


@pytest.fixture
def ha_stubber(monkeypatch):
    """Factory: install stubs for the given dotted names, cleaned up afterwards.

    ``monkeypatch`` restores ``sys.modules`` even when the test fails, so a
    half-installed stub tree cannot leak into the next test.
    """

    def install(*names: str) -> dict[str, types.ModuleType]:
        installed: dict[str, types.ModuleType] = {}
        for fullname in names:
            if fullname in sys.modules:
                continue  # real HA, or an earlier stub - leave it alone
            stub = make_stub(fullname)
            monkeypatch.setitem(sys.modules, fullname, stub)
            installed[fullname] = stub
            # Make the parent attribute point at the child, the way a real
            # package does, so `homeassistant.core` is reachable both ways.
            parent_name, _, leaf = fullname.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None:
                monkeypatch.setattr(parent, leaf, stub, raising=False)
        return installed

    return install


@pytest.fixture
def ha_stubs(ha_stubber) -> dict[str, types.ModuleType]:
    """The default ``homeassistant.*`` stub set."""
    return ha_stubber(*HA_DEFAULT_MODULES)
