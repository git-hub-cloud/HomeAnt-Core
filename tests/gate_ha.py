"""Load ``config/custom_components/*`` without Home Assistant installed.

The two components this project ships import HA at module scope (``from
homeassistant.core import callback``), so something has to answer those imports
before the module is executed — patching afterwards is too late.  This module
puts MagicMock modules into ``sys.modules`` and then loads the component by
path, the same way ``gate_cli`` loads the CLI.

Why stubs instead of the real HA: the project venv holds a pinned HA (uv.lock),
and importing it costs seconds and drags in a global event loop, a config dir
and a storage backend — none of which a criterion like "does the reap regex
match a foreign process" needs.  A stub makes the import cost microseconds and
keeps the gate independent of the HA version, which changes under us.

What is *not* stubbed: ``voluptuous``, which both components use to build a real
schema at import time (the voice one defines ``CONFIG_SCHEMA``).  Faking it
would mean the module's own setup code never runs, and a broken schema would
then only fail on a user's box.  ``check.sh`` therefore asks uv for it.

Nothing here touches ``~/.homeant``, ``homeant.cfg`` or a socket.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

from gate_paths import REPO_ROOT

COMPONENTS_DIR = REPO_ROOT / "config" / "custom_components"

# Every dotted name the components import at module scope, plus the parents the
# import machinery has to find on its way there.  A stub module has
# ``__path__ = []``, so ``import a.b.c`` needs an explicit entry for each level:
# the finder never asks a module's ``__getattr__`` for a submodule.
STUB_NAMES: tuple[str, ...] = (
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.event",
    "homeassistant.helpers.storage",
    "homeassistant.helpers.typing",
    "homeassistant.util",
    "homeassistant.util.dt",
    "homeassistant.setup",
    "homeassistant.components",
    "homeassistant.components.network",
    "homeassistant.components.network.const",
    "homeassistant.components.zeroconf",
    "homeassistant.components.homeassistant",
    "homeassistant.components.homeassistant.exposed_entities",
    # Imported by homeant_mdns on every platform that is not darwin.  Stubbed
    # even on darwin: a stub nobody imports is free, and the alternative is a
    # test that only runs on one OS.
    "zeroconf",
    "zeroconf.asyncio",
)

_components: dict[str, types.ModuleType] = {}


def _identity(fn):
    """``homeassistant.core.callback``: a marker that returns the function."""
    return fn


class _NameConflict(Exception):
    """Stand-in for ``zeroconf.NonUniqueNameException`` under the stubs.

    A stub answers every attribute with a MagicMock, and a MagicMock in an
    ``except`` clause raises TypeError instead of catching anything — so the
    component's conflict branch would be untestable (and unexercisable) without
    a real class here.
    """


# Symbols that must NOT be mocks.  A decorator is the dangerous shape: replacing
# ``@callback`` with a MagicMock replaces every decorated function with a mock,
# so a test that calls one would exercise nothing and still go green.
REAL_SYMBOLS: dict[str, dict[str, object]] = {
    "homeassistant.core": {"callback": _identity},
    "zeroconf": {"NonUniqueNameException": _NameConflict},
}


def make_stub(fullname: str) -> types.ModuleType:
    """A module whose every attribute is a MagicMock named after it."""
    from unittest.mock import MagicMock

    module = types.ModuleType(fullname)
    module.__path__ = []  # type: ignore[attr-defined]  - package marker

    def __getattr__(attr: str, _fullname: str = fullname) -> MagicMock:
        return MagicMock(name=f"{_fullname}.{attr}")

    module.__getattr__ = __getattr__  # type: ignore[attr-defined]
    return module


def install_stubs(*names: str) -> list[str]:
    """Install stubs for ``names`` (default: STUB_NAMES); return what was added.

    Idempotent and process-wide: a name already in ``sys.modules`` is left
    alone, so real HA wins if it happens to be importable, and two callers
    cannot end up with two different stub trees.  The parent attribute is
    pointed at the child, so ``homeassistant.core`` resolves both as an import
    and as an attribute.
    """
    added: list[str] = []
    for fullname in names or STUB_NAMES:
        if fullname in sys.modules:
            continue
        stub = make_stub(fullname)
        for attr, value in REAL_SYMBOLS.get(fullname, {}).items():
            setattr(stub, attr, value)
        sys.modules[fullname] = stub
        parent_name, _, leaf = fullname.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None:
            setattr(parent, leaf, stub)
        added.append(fullname)
    return added


def load_component(name: str) -> types.ModuleType:
    """Load ``config/custom_components/<name>/__init__.py`` as a module."""
    cached = _components.get(name)
    if cached is not None:
        return cached
    install_stubs()
    path = COMPONENTS_DIR / name / "__init__.py"
    if not path.is_file():
        raise ImportError(f"no component at {path}")
    module_name = f"homeant_comp_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - bad path
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    _components[name] = module
    return module


__all__ = [
    "COMPONENTS_DIR",
    "STUB_NAMES",
    "install_stubs",
    "load_component",
    "make_stub",
]
