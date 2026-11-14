"""The zip must be a function of the tree, not of the machine that packed it.

Four independent things used to leak the packer's environment into the archive,
and each one changes the digest — hence the file name — for reasons nobody could
act on:

* the per-entry timestamp, taken from each file's mtime (so the same commit
  cloned twice produced two different zips),
* the entry order, taken from the filesystem walk (NTFS and ext4 disagree),
* the mode bits, taken from the on-disk file (umask 022 vs 002),
* zipfile's SOURCE_DATE_EPOCH support, which uses localtime and therefore only
  reproduces on machines in the same timezone.

These cases pin all four.  They are deliberately about the bytes, not about the
code: "packs twice into the same digest" is the property the release depends on,
and it is the only thing that would notice a regression.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

from gate_cli import load_by_path
from gate_paths import REPO_ROOT

FLOOR = 315532800  # 1980-01-01T00:00:00Z


@pytest.fixture(scope="session")
def packer():
    """``scripts/dist-pack.py`` as a module (``dist_pack``)."""
    return load_by_path("dist_pack", REPO_ROOT / "scripts" / "dist-pack.py")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(root: Path, files: dict[str, bytes]) -> list[tuple[Path, str]]:
    """Create ``{arcname: content}`` under ``root`` and return a file_list."""
    out: list[tuple[Path, str]] = []
    for arc, data in files.items():
        target = root / arc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        out.append((target, arc))
    return out


# ── the timestamp ───────────────────────────────────────────────────────────

def test_without_source_date_epoch_the_last_commit_dates_the_build(packer, monkeypatch):
    """The default has to reproduce on its own: no wrapper script, no exported
    variable, no memory of a convention."""
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    epoch, note = packer.entry_epoch(REPO_ROOT)
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%ct"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert epoch == max(int(head), FLOOR)
    assert "last commit" in note


def test_a_tree_with_no_commit_is_refused(packer, monkeypatch, tmp_path):
    """Packing from an unpacked copy used to work; it now stops and says how to
    proceed, instead of silently dating the build by a constant."""
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    monkeypatch.setattr(packer, "_git_stamp", lambda root: None)
    with pytest.raises(SystemExit) as err:
        packer.entry_epoch(tmp_path)
    assert "SOURCE_DATE_EPOCH" in str(err.value)


def test_a_named_epoch_is_used_verbatim_in_utc(packer, monkeypatch):
    """`git log -1 --format=%ct | gmtime` — no timezone can move these numbers."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1620147723")
    epoch, note = packer.entry_epoch(REPO_ROOT)
    assert epoch == 1620147723
    assert time.gmtime(epoch)[:6] == (2021, 5, 4, 17, 2, 3)
    assert "1620147723" in note


def test_a_value_below_1980_is_clamped_not_rejected(packer, monkeypatch):
    """cpython's own support turns SOURCE_DATE_EPOCH=0 into a struct.error;
    a build knob should not be able to do that."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "0")
    epoch, note = packer.entry_epoch(REPO_ROOT)
    assert epoch == FLOOR
    assert "clamped" in note


def test_a_malformed_value_is_refused(packer, monkeypatch):
    """Named but unreadable is not the same as unset: falling through to the
    commit would pack with a date the caller did not ask for."""
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "yesterday")
    with pytest.raises(SystemExit) as err:
        packer.entry_epoch(REPO_ROOT)
    assert "SOURCE_DATE_EPOCH" in str(err.value)


# ── the bytes ───────────────────────────────────────────────────────────────

def test_the_same_tree_packs_to_the_same_bytes(packer, tmp_path):
    files = _tree(tmp_path / "src", {"a.txt": b"a\n", "d/b.txt": b"b\n"})
    stamp = (1980, 1, 1, 0, 0, 0)
    packer.make_zip(files, tmp_path / "1.zip", stamp)
    packer.make_zip(files, tmp_path / "2.zip", stamp)
    assert _digest(tmp_path / "1.zip") == _digest(tmp_path / "2.zip")


def test_touching_a_file_does_not_change_the_bytes(packer, tmp_path):
    """The failure this exists for: `git clone` writes "now" as every mtime, so
    the same commit checked out tomorrow used to pack into a different zip."""
    files = _tree(tmp_path / "src", {"a.txt": b"a\n"})
    stamp = (1980, 1, 1, 0, 0, 0)
    packer.make_zip(files, tmp_path / "1.zip", stamp)

    later = time.time() + 86400
    os.utime(files[0][0], (later, later))
    packer.make_zip(files, tmp_path / "2.zip", stamp)

    assert _digest(tmp_path / "1.zip") == _digest(tmp_path / "2.zip")


def test_the_epoch_reaches_the_archive(packer, tmp_path, monkeypatch):
    """Guard against the determinism tests passing for the wrong reason: if the
    stamp were ignored entirely they would still be green.

    The stored second is 02, not 03: the DOS field keeps seconds / 2, so the
    archive cannot express odd seconds at all.  That is a property of the
    format, not a bug — and it is why a one-second difference between two
    builds is invisible here.
    """
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1620147723")
    stamp = time.gmtime(packer.entry_epoch(REPO_ROOT)[0])[:6]
    files = _tree(tmp_path / "src", {"a.txt": b"a\n", "d/b.txt": b"b\n"})
    packer.make_zip(files, tmp_path / "out.zip", stamp)
    with zipfile.ZipFile(tmp_path / "out.zip") as zf:
        assert {info.date_time for info in zf.infolist()} == {(2021, 5, 4, 17, 2, 2)}


def test_a_different_epoch_produces_different_bytes(packer, tmp_path):
    files = _tree(tmp_path / "src", {"a.txt": b"a\n"})
    packer.make_zip(files, tmp_path / "1.zip", (1980, 1, 1, 0, 0, 0))
    packer.make_zip(files, tmp_path / "2.zip", (2021, 5, 4, 17, 2, 3))
    assert _digest(tmp_path / "1.zip") != _digest(tmp_path / "2.zip")


# ── the two fields that are not the timestamp ───────────────────────────────

def test_entries_are_sorted_by_arcname(packer, tmp_path):
    root = tmp_path / "tree"
    _tree(root, {"config/z.txt": b"z\n", "config/a.txt": b"a\n",
                 "scripts/homeant.py": b"#\n"})
    arcs = [arc for _src, arc in packer.collect_files(root)]
    assert arcs == sorted(arcs)
    assert arcs == ["config/a.txt", "config/z.txt", "scripts/homeant.py"]


def test_mode_bits_are_written_not_copied(packer, tmp_path):
    """`zf.write()` copies the on-disk mode, so the archive depended on the
    packer's umask — and on Windows the launcher has no +x to copy at all."""
    files = _tree(tmp_path / "src", {"homeant": b"#!/bin/sh\n", "a.txt": b"a\n"})
    packer.make_zip(files, tmp_path / "out.zip", (1980, 1, 1, 0, 0, 0))
    with zipfile.ZipFile(tmp_path / "out.zip") as zf:
        modes = {i.filename: i.external_attr >> 16 for i in zf.infolist()}
    assert modes == {"homeant": 0o755, "a.txt": 0o644}


# ── the installer copies ────────────────────────────────────────────────────

def _unstamped_scripts(root: Path, packer) -> None:
    """`scripts/` as it sits in the repo: two installers, both unstamped."""
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "install.sh").write_text(
        f'VERSION="{packer.VERSION_PLACEHOLDER}"\n'
        f'SHA="{packer.ZIP_SHA256_PLACEHOLDER}"\n',
        encoding="utf-8")
    (root / "scripts" / "install.ps1").write_text(
        f'$Version = "{packer.VERSION_PLACEHOLDER}"\n'
        f'$Sha = "{packer.ZIP_SHA256_PLACEHOLDER}"\n',
        encoding="utf-8")


def test_each_installer_is_written_twice_with_the_same_bytes(packer, tmp_path):
    """Fixed name for `curl | sh`, versioned name for the record.

    The two must be byte-identical: the versioned copy exists so a release can
    be audited after the fixed name has moved on, which is worthless if the
    copies differ.  They cannot, by construction — one stamped string, two
    paths — and this is the assertion that keeps it that way.
    """
    repo = tmp_path / "repo"
    _unstamped_scripts(repo, packer)
    out = tmp_path / "dist"
    out.mkdir()

    fixed, pinned = packer.stamp_install_scripts(
        repo, "9.9.9", out, "588ba06a" + "0" * 56)

    assert fixed == ["install.ps1", "install.sh"]
    assert pinned == ["install-9.9.9-588ba06a.ps1", "install-9.9.9-588ba06a.sh"]
    for plain, alias in zip(fixed, pinned):
        assert (out / alias).read_bytes() == (out / plain).read_bytes()
        text = (out / alias).read_text(encoding="utf-8")
        assert packer.VERSION_PLACEHOLDER not in text
        assert packer.ZIP_SHA256_PLACEHOLDER not in text


def test_the_pinned_installer_carries_the_same_tag_as_the_zip(packer):
    """One `digest[:TAG_LEN]` feeds both names, so an installer and the archive
    it installs pair up by eye — and a half-updated release is visible without
    opening either file."""
    digest = hashlib.sha256(b"whatever").hexdigest()
    tag = digest[: packer.TAG_LEN]
    assert packer.arc_name("0.1.1", digest) == f"HomeAnt-0.1.1-{tag}.zip"
    assert packer.versioned_name("0.1.1", digest, ".sh") == \
        f"install-0.1.1-{tag}.sh"
    assert packer.versioned_name("0.1.1", digest, ".ps1") == \
        f"install-0.1.1-{tag}.ps1"
