"""D17 — the service environment must carry working dirs, not just USERPROFILE.

The defect this pins down is invisible from the outside and unrepairable by the
user it hits: HA installs component requirements at run time with
``uv pip install``; with uv's default cache under SYSTEM's own
``%LOCALAPPDATA%`` the files it moves into site-packages keep SYSTEM's ACEs
instead of inheriting the venv's.  Measured 11-14 on this machine: 16995 of
26805 files unreadable to the owner of the install, ``/`` answering 500, and
``Get-Acl`` / ``icacls /reset`` both denied because that user is not their
owner.  Pointing ``UV_CACHE_DIR`` / ``TMP`` / ``TEMP`` inside HA_ROOT fixed it
(912 freshly installed files, 0 unreadable).

Two separate rules therefore get tested here:

* what ``_win_service_env()`` contains, and that ``_win_service_home_write()``
  really writes all of it — a write that silently drops the dirs would restore
  the damage on the next re-registration;
* that ``_win_service_home_ok()`` still means "USERPROFILE only", because a
  pre-11-14 install reaches the start hot path non-elevated and must not be
  turned into a machine that refuses to start.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


def _only_profile_registry(monkeypatch, cli, entries):
    """Make ``_win_reg_value("Environment")`` answer with `entries`."""
    value = "|".join(entries)

    def fake(name: str):
        return value if name == "Environment" else None

    monkeypatch.setattr(cli, "_win_reg_value", fake)


def test_service_env_covers_profile_and_working_dirs(cli, tmp_path, monkeypatch):
    root = tmp_path / ".homeant"
    monkeypatch.setattr(cli, "HA_ROOT", root)

    entries = cli._win_service_env()

    assert len(entries) == 4, f"expected 4 environment entries, got {entries}"
    got = dict(entry.split("=", 1) for entry in entries)
    assert set(got) == {"USERPROFILE", "TMP", "TEMP", "UV_CACHE_DIR"}
    assert got["USERPROFILE"] == str(Path.home())
    # The whole point: nothing the service downloads or unpacks may land
    # outside the data root, because that is the only place its ACLs inherit
    # from a directory the human user can read.
    for key in ("TMP", "TEMP", "UV_CACHE_DIR"):
        assert Path(got[key]).is_relative_to(root), (
            f"{key}={got[key]} is outside HA_ROOT {root} — files created there "
            "carry ACLs the installing user cannot repair"
        )
    assert got["UV_CACHE_DIR"] == str(root / "cache"), (
        "uv must not fall back to SYSTEM's %LOCALAPPDATA% cache"
    )


def test_home_ok_still_means_profile_only(cli, tmp_path, monkeypatch):
    """A pre-11-14 install must keep taking the non-elevated hot path."""
    monkeypatch.setattr(cli, "HA_ROOT", tmp_path / ".homeant")
    _only_profile_registry(monkeypatch, cli, [f"USERPROFILE={Path.home()}"])

    assert cli._win_service_home_ok() is True, (
        "USERPROFILE is what HA_ROOT depends on; widening this check to the "
        "working dirs would make an old install refuse to start as a plain user"
    )
    assert cli._win_service_dirs_ok() is False, (
        "an environment with only USERPROFILE has no working dirs"
    )


def test_dirs_ok_accepts_a_complete_environment(cli, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "HA_ROOT", tmp_path / ".homeant")
    _only_profile_registry(monkeypatch, cli, cli._win_service_env())

    assert cli._win_service_dirs_ok() is True
    assert cli._win_service_home_ok() is True


def test_write_persists_every_entry(cli, tmp_path, monkeypatch):
    """The registry write must contain the dirs, not just the profile.

    This is the regression that matters: ``_win_service_home_write`` used to
    hand ``SetValueEx`` a one-element list, so every re-registration (moving
    the tree, reinstalling) silently put the service back on SYSTEM's private
    cache — the damage returns with no visible cause.
    """
    root = tmp_path / ".homeant"
    monkeypatch.setattr(cli, "HA_ROOT", root)

    written = {}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake = types.ModuleType("winreg")
    fake.HKEY_LOCAL_MACHINE = object()
    fake.KEY_SET_VALUE = 0
    fake.REG_MULTI_SZ = 7
    fake.CreateKeyEx = lambda *a, **k: _Key()

    def set_value(key, name, reserved, kind, value):
        written[name] = (kind, list(value))

    fake.SetValueEx = set_value
    monkeypatch.setitem(sys.modules, "winreg", fake)

    assert cli._win_service_home_write() is True

    kind, value = written["Environment"]
    assert kind == 7, "the value has to stay REG_MULTI_SZ"
    assert value == cli._win_service_env(), (
        "the write dropped entries — the service would run without "
        f"TMP/TEMP/UV_CACHE_DIR again: {value}"
    )
    assert (root / "tmp").is_dir() and (root / "cache").is_dir(), (
        "the dirs the environment points at must exist before the service "
        "starts, or uv recreates them with whatever ACLs it likes"
    )


def test_hot_path_repairs_the_dirs(cli):
    """Structural: an old install only ever reaches the hot path."""
    src = Path(cli.__file__).read_text(encoding="utf-8")
    body = src[src.index("def _win_service_ensure"):]
    body = body[:body.index("\ndef _win_open_service")]
    hot = body[:body.index("return True")]

    assert "_win_service_dirs_ok()" in hot, (
        "the hot path never looks at the working dirs, so an install "
        "registered before 11-14 never gets them"
    )
    assert "_win_service_home_write(quiet=True)" in hot, (
        "the repair must be quiet: a plain user cannot write HKLM, and that "
        "is the expected outcome there, not a FAIL"
    )
