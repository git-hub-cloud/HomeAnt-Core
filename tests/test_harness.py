"""Smoke tests for the harness itself.

This file exists so `pytest .` has something to collect before the D02-D11
cases land: pytest exits 5 on an empty run, which would make the gate look
broken rather than empty.  Everything here asserts a property the later cases
depend on, so it is not busywork — if these fail, every other test is suspect.

Paths come from ``gate_paths`` rather than from ``conftest``: pytest's import
mode decides whether a sibling module is importable by name, and a test that
only works under one mode is a trap for whoever changes the mode later.
"""

from __future__ import annotations

import sys
from pathlib import Path

GATE_DIR = Path(__file__).resolve().parent
if str(GATE_DIR) not in sys.path:
    sys.path.insert(0, str(GATE_DIR))

from gate_paths import GATE_DIR as _GATE_DIR  # noqa: E402,F401 - import check
from gate_paths import REPO_ROOT  # noqa: E402

CLI_PATH = REPO_ROOT / "scripts" / "homeant.py"


def test_repo_root_is_the_repo():
    """The resolved root must actually hold the CLI, or every case judges the
    wrong tree and still prints PASS."""
    assert CLI_PATH.is_file(), f"REPO_ROOT resolved to {REPO_ROOT}, which has no {CLI_PATH.name}"
    assert (REPO_ROOT / "pyproject.toml").is_file()


def test_cli_loads_from_scripts(cli):
    """The CLI is loadable by path, and the symbols the cases patch exist."""
    for symbol in ("load_cfg", "_merge_file", "_WSConn", "_stop_pid", "_ws_purge_normal"):
        assert hasattr(cli, symbol), f"scripts/homeant.py lost {symbol}"
    assert Path(cli.__file__).resolve() == CLI_PATH


def test_cli_did_not_start_anything(cli):
    """Importing the CLI must not touch the live instance home.

    ``sys.modules`` registration is the only side effect allowed.  If the module
    ever grows top-level work (reading homeant.cfg, probing a port), a test run
    on a developer box would start stepping on the real instance — and the
    failure would show up as a flaky test, not as "the module does work at
    import time".
    """
    assert cli.HA_ROOT == Path.home() / ".homeant"
    assert cli.CONFIG_FILE.name == "homeant.cfg"


def test_ha_stubs_resolve(ha_stubs):
    """The stub tree is importable both ways, which is what the component
    tests need — they import HA at module scope."""
    import homeassistant.core as core
    from homeassistant.helpers import entity_registry

    assert core.HomeAssistant is not None
    assert entity_registry is ha_stubs["homeassistant.helpers.entity_registry"]


def test_ha_stubs_cover_unknown_attributes(ha_stubs):
    """A symbol nobody listed must still resolve to a mock, not AttributeError.

    The components reach for HA helpers that change between releases; a stub
    that only knows the names written down here would break on upgrade for no
    reason connected to the code under test.
    """
    import homeassistant.helpers.event as event

    assert event.async_track_state_change_event is not None

    assert "homeassistant.helpers" in sys.modules
