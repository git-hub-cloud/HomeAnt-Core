#!/bin/sh
# HomeAnt local gate — run this before calling any change done.
#
#   1. syntax-check every .py this repo owns (in memory: no __pycache__ residue)
#   2. run this directory's tests under pytest
#
# Lives in the repo's tests/ directory and is version-managed.  It is dev
# tooling, so it stays out of the release zip (dist-pack.py uses a white list).
# Run it from the repo root, or set HOMEANT_ROOT.
#
# Both steps are subcommands of verify.py, not shell here: the uv discovery and
# the `--with` list are one fact that check.sh and check.ps1 would otherwise
# each spell out, and two spellings of "how to run the tests" is the drift the
# gate itself exists to prevent.
#
# pytest is deliberately NOT a project dependency.  Putting it in
# pyproject.toml would ship a test runner inside the runtime; installing it into
# .venv by hand is the `uv pip install` drift uv.lock cannot see — it resolves
# the newest matching version instead of the locked one, which is exactly how
# multidict 7.2.1 ended up in an environment that pins 6.9.1.
#
# `uv run --no-project` builds a throwaway environment from uv's cache and
# leaves .venv (the HA runtime) untouched.  --with pyyaml / voluptuous are there
# because the seed merge and the voice component import them; --with bcrypt
# because the broker's auth plugin is tested against HA's real password
# encoding (base64 of a bcrypt hash), which a stub cannot verify.
set -eu

GATE_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_ROOT="${HOMEANT_ROOT:-$PWD}"

if [ ! -f "$REPO_ROOT/scripts/homeant.py" ]; then
    echo "check: no scripts/homeant.py under $REPO_ROOT - run me from the repo root, or set HOMEANT_ROOT" >&2
    exit 1
fi

cd "$REPO_ROOT" || exit 1

PYTHON="${PYTHON:-python}"

echo "== compile =="
"$PYTHON" "$GATE_DIR/verify.py" --compile

echo "== pytest =="
exec "$PYTHON" "$GATE_DIR/verify.py" --pytest
