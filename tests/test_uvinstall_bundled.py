"""The uv bootstrap ships inside the release zip, not on the CDN.

D26. Before this, an install with no local uv made three homeant.cn requests:
the installer, the source zip, and the bootstrap script (uv/<version>/
uvinstall.{sh,ps1}). Now the two bootstrap files are checked in at
scripts/uvinstall.{sh,ps1}, collected into the zip, and both installers read
them from the unpacked tree -- the third request is gone.

The file names are FIXED: no version in the path, and neither installer holds a
version variable. The uv version and its wheel URLs live only in each file's
own embedded UVLOCK, so a bump replaces the file contents and touches nothing
else. Four independent links, each pinned here so a partial change is caught:

* the two files exist at the fixed path and the old uv/ tree is gone;
* dist-pack collects both by name and check_drift() requires them -- a missing
  bootstrap fails the pack, with no version to compare;
* install.sh makes no remote bootstrap request and reads the local copy with
  a missing/empty guard, keeping the same "uv install failed:" wording;
* install.ps1 does the same through a scriptblock (scope isolation intact)
  and reads the file as UTF-8.

No external file carries a uv version for these scripts to agree on: that is
the point of the fixed name. pyproject.toml's `uv==` is a separate fact -- the
HA runtime dependency recorded in uv.lock -- and is deliberately not a link
in this chain.
"""

from __future__ import annotations

import re

import pytest

from gate_cli import load_by_path
from gate_paths import REPO_ROOT

INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"
DIST_PACK = REPO_ROOT / "scripts" / "dist-pack.py"


@pytest.fixture(scope="module")
def packer():
    return load_by_path("dist_pack_uv", DIST_PACK)


# ── step 1: fixed names, nothing left at the old path ───────────────────────

def test_bootstrap_scripts_live_at_fixed_names():
    for name in ("uvinstall.sh", "uvinstall.ps1"):
        path = REPO_ROOT / "scripts" / name
        assert path.is_file(), f"scripts/{name} missing — the installer would find no bootstrap in the zip"
    assert not (REPO_ROOT / "uv").exists(), "old CDN-staging uv/ tree still present"
    assert not (REPO_ROOT / "scripts" / "uv").exists(), "old version-named scripts/uv/ tree still present"


def test_no_external_uv_version_reference():
    """Neither installer, nor the packer, pins a uv version any more.

    The version lives only in the bootstrap files themselves. A stray
    `UV_VERSION` / `$UvVersion` here would be a second source of truth that a
    bump has to keep in sync -- exactly the drift this change removes.
    """
    for path in (INSTALL_SH, INSTALL_PS1, DIST_PACK):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"\bUV_VERSION\b|\$UvVersion\b", text), (
            f"{path.name}: a uv version variable survived"
        )
        assert not re.search(r"scripts[/\\]uv[/\\]", text), (
            f"{path.name}: a version-named scripts/uv/ path survived"
        )


# ── step 2: packing is by fixed filename, check_drift requires them ────────

def test_pack_list_collects_the_two_bootstrap_files(packer, tmp_path):
    for name in ("uvinstall.sh", "uvinstall.ps1"):
        assert f"scripts/{name}" in packer.INCLUDE_PATHS, (
            f"INCLUDE_PATHS must list scripts/{name} explicitly"
        )
    (tmp_path / "scripts").mkdir()
    for name in ("uvinstall.sh", "uvinstall.ps1"):
        (tmp_path / "scripts" / name).write_text("# bootstrap\n", encoding="utf-8")
    arcs = [arc for _src, arc in packer.collect_files(tmp_path)]
    assert "scripts/uvinstall.sh" in arcs and "scripts/uvinstall.ps1" in arcs, arcs


def test_drift_check_requires_the_bootstrap_files(packer, tmp_path):
    """check_drift must fail on a missing bootstrap and pass when both exist.

    Copy the files check_drift reads; with no bootstrap present the only
    uv-related drift reported must name both scripts. Adding the real files
    back makes the tree clean. Real files, not stubs: they carry the embedded
    UVLOCK version that check 7 now compares to pyproject's pin.
    """
    import shutil

    for rel in ("pyproject.toml", "scripts/install.sh", "scripts/install.ps1",
                "scripts/homeant.py"):
        src = REPO_ROOT / rel
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    version = packer.read_version(tmp_path / "pyproject.toml")
    problems = packer.check_drift(tmp_path, version)
    assert any("scripts/uvinstall.sh" in p for p in problems), problems
    assert any("scripts/uvinstall.ps1" in p for p in problems), problems

    for name in ("uvinstall.sh", "uvinstall.ps1"):
        shutil.copy2(REPO_ROOT / "scripts" / name, tmp_path / "scripts" / name)
    assert packer.check_drift(tmp_path, version) == [], (
        "with both bootstrap files present, no drift should remain"
    )


def test_drift_check_rejects_uv_version_disagreement(packer, tmp_path):
    """pyproject's uv pin and each bootstrap's UVLOCK version must agree.

    Three independent injections: bump the pin, bump one bootstrap, remove a
    version block. Each must be caught before packing; an untouched copy must
    stay clean. This is the guard that stops a half-applied uv bump from
    shipping a zip whose installer disagrees with HA's lock.
    """
    import shutil

    files = ("pyproject.toml", "scripts/install.sh", "scripts/install.ps1",
             "scripts/homeant.py", "scripts/uvinstall.sh", "scripts/uvinstall.ps1")

    def load_clean():
        for rel in files:
            dst = tmp_path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / rel, dst)

    def inject(rel, pattern, repl):
        p = tmp_path / rel
        text = p.read_text(encoding="utf-8")
        new, hits = re.subn(pattern, repl, text, count=1)
        assert hits == 1, f"injection missed in {rel}"
        p.write_text(new, encoding="utf-8")

    load_clean()
    version = packer.read_version(tmp_path / "pyproject.toml")
    assert packer.check_drift(tmp_path, version) == [], "untouched copy must be clean"

    injections = (
        ("pyproject.toml", r'"uv==0\.13\.6"', '"uv==9.9.9"'),
        ("scripts/uvinstall.sh",
         r'(name\s*=\s*"uv"\s*\r?\n\s*version\s*=\s*")0\.13\.6(")',
         r"\g<1>9.9.9\g<2>"),
        ("scripts/uvinstall.ps1",
         r'(name\s*=\s*"uv"\s*\r?\n\s*version\s*=\s*")0\.13\.6(")',
         r"\g<1>9.9.9\g<2>"),
        ("scripts/uvinstall.sh",
         r'name\s*=\s*"uv"\s*\r?\n\s*version\s*=\s*"[^"]+"',
         'name = "not-uv"'),
    )
    for rel, pattern, repl in injections:
        load_clean()
        inject(rel, pattern, repl)
        problems = packer.check_drift(tmp_path, version)
        assert any("uv version differs" in p or "no embedded uv version" in p
                   for p in problems), f"{rel}: drift not caught: {problems}"


# ── step 3: install.sh reads locally, no remote bootstrap request ───────────

def test_install_sh_has_no_remote_bootstrap_request():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "UV_INSTALL_URL" not in text, "install.sh still builds a bootstrap URL"
    assert not re.search(r"https://\$\{HOMEANT_SERVER\}/uv/", text), (
        "install.sh still requests the bootstrap from homeant.cn"
    )
    assert "fetch_script" not in text, "dead remote-fetch helper survived"


def test_install_sh_reads_local_bootstrap_with_a_guard():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert '"$repo_root/scripts/uvinstall.sh"' in text, (
        "install.sh no longer points at the fixed in-zip bootstrap path"
    )
    assert '[ ! -s "$uv_installer" ]' in text, (
        "missing/empty bootstrap must not be piped into sh as a no-op"
    )
    assert 'uv install failed: missing bootstrap scripts/uvinstall.sh in this package' in text, (
        "missing-bootstrap failure lost the shared uv install failed: wording"
    )


# ── step 4: install.ps1 reads locally, scope isolation intact ───────────────

def test_install_ps1_has_no_remote_bootstrap_request():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "UvInstallUrl" not in text, "install.ps1 still builds a bootstrap URL"
    assert not re.search(r"https://\$HomeAntServer/uv/", text), (
        "install.ps1 still requests the bootstrap from homeant.cn"
    )
    assert "Invoke-WebRequest -UseBasicParsing -Uri $UvInstallUrl" not in text


def test_install_ps1_reads_local_bootstrap_in_its_own_scope():
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert 'Join-Path $RepoRoot "scripts\\uvinstall.ps1"' in text, (
        "install.ps1 no longer points at the fixed in-zip bootstrap path"
    )
    assert '[System.IO.File]::ReadAllText($uvBootstrap, [System.Text.Encoding]::UTF8)' in text, (
        "local bootstrap must be decoded as UTF-8 explicitly"
    )
    assert 'scriptblock]::Create($uvInstaller)' in text, (
        "scope isolation was lost -- installer variables would leak into the caller"
    )
    assert 'Test-Path $uvBootstrap -PathType Leaf' in text, (
        "missing bootstrap must fail before the scriptblock runs"
    )
    assert 'uv install failed: missing bootstrap scripts/uvinstall.ps1 in this package' in text
