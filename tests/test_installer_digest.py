"""The stamped digest and the two runtime helpers that read it.

TODO D15-2.  The digest rides *inside* the installers now: `dist-pack.py`
computes the zip's sha256 and rewrites the placeholder literal before the file
is uploaded, and each installer compares it against the file it downloaded.

Two halves, because the failure modes are on opposite sides of that hand-over:

* the packer has to leave **no** placeholder behind and put the digest exactly
  where the runtime helper looks for it — while the source tree keeps its
  placeholder, since that is what the next release stamps;
* each helper has to answer the three-valued contract: 0 verified, 1 mismatch,
  2 could not check.  The 2 is load-bearing — an installer that cannot verify
  must say so and keep going, and it must never fold 2 into either 0 or 1.

The helpers are exercised as they ship: the function text is lifted out of the
real installer and run by the real shell, so this cannot drift from the file a
user downloads.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from gate_cli import load_by_path
from gate_paths import REPO_ROOT

INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"

# A digest that is definitely not any file's: 64 hex characters, all the same,
# so a mistaken `count()` or a substring match cannot pass by accident.
DIGEST = "ab" * 32


def extract_function(text: str, header: str) -> str:
    """One function's own text: from ``header`` to the first ``}`` at column 0.

    Both installers close every top-level function on a line of its own, which
    is the property that makes this safe — a brace inside a string or a nested
    block is indented.
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(header)]
    assert len(starts) == 1, f"{header!r}: found {len(starts)} definitions, expected 1"
    for end in range(starts[0] + 1, len(lines)):
        if lines[end] == "}":
            return "\n".join(lines[starts[0]:end + 1])
    raise AssertionError(f"{header!r}: no closing brace at column 0")


@pytest.fixture()
def zip_file(tmp_path: Path) -> Path:
    """Some bytes with a real sha256 — the helpers only ever hash a file."""
    path = tmp_path / "HomeAnt-0.1.1.zip"
    path.write_bytes(b"PK\x03\x04 not really a zip, but it hashes\n")
    return path


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── pack side ───────────────────────────────────────────────────────────────


def test_stamp_fills_the_placeholders_and_leaves_the_tree_alone(tmp_path: Path) -> None:
    pack = load_by_path("dist_pack_under_test", REPO_ROOT / "scripts" / "dist-pack.py")
    version = pack.read_version(REPO_ROOT / "pyproject.toml")
    fixed, pinned = pack.stamp_install_scripts(REPO_ROOT, version, tmp_path, DIGEST)
    assert sorted(fixed) == ["install.ps1", "install.sh"], fixed
    # The immutable copies land beside them, same bytes (one stamped string).
    assert len(pinned) == len(fixed), pinned

    for name in fixed + pinned:
        out = (tmp_path / name).read_text(encoding="utf-8")
        assert pack.ZIP_SHA256_PLACEHOLDER not in out, (
            f"{name}: placeholder survived the stamp — the uploaded installer "
            "would answer 'could not check' for every user"
        )
        assert out.count(DIGEST) == 1, (
            f"{name}: digest appears {out.count(DIGEST)} times, expected exactly 1"
        )
        assert version in out, f"{name}: no version stamp"

    for name in fixed:
        src = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert pack.ZIP_SHA256_PLACEHOLDER in src, (
            f"scripts/{name} was stamped in place — the tree must keep the "
            "placeholder, dist/ is the stamped copy"
        )


# ── runtime: the shell helper ───────────────────────────────────────────────

SH_SCRIPT = """
set -u
HOMEANT_ZIP_SHA256={want}
{body}
verify_zip_digest "$1"
echo "rc=$?"
"""


def run_sh_helper(tmp_path: Path, zip_path: Path, want: str) -> int:
    body = "\n\n".join([
        extract_function(INSTALL_SH.read_text(encoding="utf-8"), "sha256_of("),
        extract_function(INSTALL_SH.read_text(encoding="utf-8"), "verify_zip_digest("),
    ])
    script = tmp_path / "digest.sh"
    script.write_text(SH_SCRIPT.format(want=f"'{want}'", body=body),
                      encoding="utf-8", newline="\n")
    # as_posix(), not str(): GNU sha256sum runs in "escaped" mode when the file
    # name contains a backslash and then prints the digest prefixed with `\`,
    # so a Windows path makes the helper look broken while it is only being fed
    # a name no unix installer would ever hand it.  (Measured: `\<hex>`.)
    proc = subprocess.run(["sh", str(script), zip_path.as_posix()],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"helper script died: {proc.stderr}"
    last = [ln for ln in proc.stdout.splitlines() if ln.startswith("rc=")][-1]
    return int(last.split("=", 1)[1])


@pytest.mark.parametrize("case,want,expected", [
    ("matches", "MATCH", 0),
    ("upper case", "UPPER", 0),
    ("one byte changed", "MISMATCH", 1),
    ("placeholder", "__HOMEANT_ZIP_SHA256_PLACEHOLDER__", 2),
    ("empty", "", 2),
])
def test_sh_verify_zip_digest(tmp_path: Path, zip_file: Path,
                              case: str, want: str, expected: int) -> None:
    real = digest_of(zip_file)
    want = {"MATCH": real, "UPPER": real.upper(),
            "MISMATCH": ("0" if real[0] != "0" else "1") + real[1:]}.get(want, want)
    rc = run_sh_helper(tmp_path, zip_file, want)
    assert rc == expected, f"{case}: verify_zip_digest returned {rc}, expected {expected}"


# ── runtime: the PowerShell helper ─────────────────────────────────────────

PS_SCRIPT = """
$ErrorActionPreference = 'Stop'
$HOMEANT_ZIP_SHA256 = "{want}"
{body}
exit (_zip_digest_ok "{zip_path}")
"""


def run_ps1_helper(tmp_path: Path, zip_path: Path, want: str) -> int:
    if shutil.which("powershell") is None:
        pytest.skip("no powershell on PATH")
    body = extract_function(INSTALL_PS1.read_text(encoding="utf-8"),
                            "function _zip_digest_ok")
    script = tmp_path / "digest.ps1"
    script.write_text(PS_SCRIPT.format(want=want, body=body, zip_path=zip_path),
                      encoding="utf-8-sig")
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, timeout=180,
    )
    return proc.returncode


@pytest.mark.parametrize("case,want,expected", [
    ("matches", "MATCH", 0),
    ("upper case", "UPPER", 0),
    ("one byte changed", "MISMATCH", 1),
    ("placeholder", "__HOMEANT_ZIP_SHA256_PLACEHOLDER__", 2),
    ("empty", "", 2),
])
def test_ps1_zip_digest_ok(tmp_path: Path, zip_file: Path,
                           case: str, want: str, expected: int) -> None:
    real = digest_of(zip_file)
    want = {"MATCH": real, "UPPER": real.upper(),
            "MISMATCH": ("0" if real[0] != "0" else "1") + real[1:]}.get(want, want)
    rc = run_ps1_helper(tmp_path, zip_file, want)
    assert rc == expected, f"{case}: _zip_digest_ok returned {rc}, expected {expected}"
