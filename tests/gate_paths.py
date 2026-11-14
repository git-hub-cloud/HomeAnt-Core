"""Where the repo is, for the gate that lives in ``tests/``.

The harness is dev tooling, not shipped code.  It now sits inside the tree it
checks, but it must not assume a fixed depth: ``Path(__file__).parents[1]``
breaks the day this directory moves.  This module is the single place that
answers "which repo?" for both verify.py and conftest.py — two copies of this
walk would disagree the day one of them moved.

Resolution order, first hit wins:

1. ``$HOMEANT_ROOT`` — explicit, so a second checkout or a copied tree works.
2. the nearest ancestor of the *current directory* holding
   ``scripts/homeant.py``.  The gate is meant to be run from the repo root.
3. the same walk up from this file's own directory (the gate's own position in
   the tree, so ``python tests/verify.py`` also works from anywhere).

There is no hard-coded default.  A gate that silently checks the wrong repo is
worse than one that stops: every case would still print PASS.
"""

from __future__ import annotations

import os
from pathlib import Path

GATE_DIR = Path(__file__).resolve().parent

# The file whose presence makes a directory "the repo".  Nothing else in the
# tree is as reliable: pyproject.toml exists in sub-projects, uv.lock moves.
MARKER = Path("scripts") / "homeant.py"


def _holds_repo(path: Path) -> bool:
    return (path / MARKER).is_file()


def _walk_up(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if _holds_repo(candidate):
            return candidate
    return None


def resolve() -> Path:
    env = os.environ.get("HOMEANT_ROOT", "").strip()
    if env:
        path = Path(env).expanduser().resolve()
        if not _holds_repo(path):
            raise SystemExit(
                f"gate: HOMEANT_ROOT={path} holds no {MARKER} - point it at the repo"
            )
        return path
    for start in (Path.cwd().resolve(), GATE_DIR):
        found = _walk_up(start)
        if found is not None:
            return found
    raise SystemExit(
        "gate: cannot find the repo - run this from the repo root, or set "
        f"HOMEANT_ROOT (no {MARKER} above {Path.cwd()} or {GATE_DIR})"
    )


REPO_ROOT = resolve()
