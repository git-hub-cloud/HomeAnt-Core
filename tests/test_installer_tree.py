"""D22 — `--upgrade` inside a source tree redeploys that tree.

`--upgrade` originally meant "replace the tree you are standing in with the
published zip", so a source tree — the developer's own work — was refused.  But
the tree is also the *only* target the mode accepts, and a source tree is never
stamped (the packager stamps a copy), so every `--upgrade` run in a tree landed
on "installer is not stamped with a version" instead of on anything useful.

The resolution: the mode's meaning is "make the deployment match the tree you
are standing in".  A stamped tree fetches that version; an unstamped one has
nothing to fetch, so it redeploys in place — stop, remove the domains this tree
ships, start.  That is the same three steps the old refusal printed, except now
the installer runs them itself and touches only the domains it ships instead of
the whole custom_components directory.

What is pinned here, in **both** files (they are meant to be the same installer
in two languages, and they have drifted before):

* the refusal is gone — a stale copy carrying it would refuse again;
* the redeploy call sits *inside* the version guard, ahead of the generic
  "not stamped" message, and the generic message is still reachable;
* the domain scan exists once and is called from both paths, so the published
  install and the source-tree redeploy cannot delete different sets;
* inside the redeploy the order is stop -> remove -> start: `start` is
  idempotent, so with HA still running the removed domains would never come
  back until some later restart;
* the removal targets the domains this tree ships, never the whole directory —
  a hand-installed integration living there must survive.
"""

from __future__ import annotations

from gate_paths import REPO_ROOT

INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"

GENERIC = "installer is not stamped with a version"


def _sh() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def _ps1() -> str:
    return INSTALL_PS1.read_text(encoding="utf-8")


def _sh_fn_body(text: str, name: str) -> str:
    """Body of a POSIX shell function, delimited by a column-0 closing brace."""
    start = text.index(f"{name}() {{")
    return text[start:text.index("\n}\n", start)]


def _ps1_fn_body(text: str, name: str) -> str:
    """Body of a PowerShell function, same delimiter — nested braces are indented."""
    start = text.index(f"function {name}")
    return text[start:text.index("\n}\n", start)]


def test_neither_installer_refuses_upgrade_in_a_source_tree():
    """The old refusal is exactly the bug: it named a mode that could not work."""
    sh, ps1 = _sh(), _ps1()
    for name, text in (("install.sh", sh), ("install.ps1", ps1)):
        assert "cannot run in a source tree" not in text, f"{name} still refuses"
        assert "no version to upgrade to in a source tree" not in text, f"{name} still refuses"


def test_the_redeploy_call_sits_inside_the_version_guard():
    """Ordering is the guard's whole value: the branch only runs unstamped."""
    sh, ps1 = _sh(), _ps1()

    sh_guard = sh.index('if ! printf \'%s\\n\' "$HOMEANT_VERSION"')
    sh_call = sh.index("\n        _redeploy_source_tree\n")
    sh_generic = sh.index(f'err "{GENERIC}"')
    assert sh_guard < sh_call < sh_generic, (
        "install.sh: the redeploy branch is not between the guard and the generic message"
    )

    ps1_guard = ps1.index("if ($HOMEANT_VERSION -notmatch")
    ps1_call = ps1.index("\n            _RedeploySourceTree $here\n")
    ps1_generic = ps1.index(f'throw "{GENERIC}"')
    assert ps1_guard < ps1_call < ps1_generic, (
        "install.ps1: the redeploy branch is not between the guard and the generic message"
    )


def test_one_domain_scan_serves_both_paths():
    """Two copies of "which domains do we ship" would drift; there is one."""
    sh, ps1 = _sh(), _ps1()

    assert "_replace_shipped_components() {" in sh, "install.sh: scan function is gone"
    assert '_replace_shipped_components "$repo_root"' in sh, (
        "install.sh: the published install path no longer uses the shared scan"
    )
    sh_redeploy = _sh_fn_body(sh, "_redeploy_source_tree")
    assert '_replace_shipped_components "$_here"' in sh_redeploy, (
        "install.sh: the source-tree redeploy no longer uses the shared scan"
    )

    assert "function _ReplaceShippedComponents" in ps1, "install.ps1: scan function is gone"
    assert "_ReplaceShippedComponents $RepoRoot" in ps1, (
        "install.ps1: the published install path no longer uses the shared scan"
    )
    ps1_redeploy = _ps1_fn_body(ps1, "_RedeploySourceTree")
    assert "_ReplaceShippedComponents $Root" in ps1_redeploy, (
        "install.ps1: the source-tree redeploy no longer uses the shared scan"
    )


def test_redeploy_stops_before_it_removes_and_starts_after():
    """`start` is idempotent — removing a live integration loses it until restart."""
    sh, ps1 = _sh(), _ps1()

    sh_body = _sh_fn_body(sh, "_redeploy_source_tree")
    assert sh_body.index("./homeant stop") < sh_body.index("_replace_shipped_components") < \
        sh_body.index("./homeant start"), "install.sh: redeploy order is not stop -> remove -> start"

    ps1_body = _ps1_fn_body(ps1, "_RedeploySourceTree")
    assert ps1_body.index(".\\homeant stop") < ps1_body.index("_ReplaceShippedComponents") < \
        ps1_body.index(".\\homeant start"), "install.ps1: redeploy order is not stop -> remove -> start"


def test_removal_targets_shipped_domains_not_the_whole_directory():
    """A hand-installed integration in HA_ROOT must not be collateral damage."""
    sh, ps1 = _sh(), _ps1()

    assert 'rm -rf ~/.homeant/custom_components"' not in sh, (
        "install.sh: still removes the whole custom_components directory"
    )
    assert 'custom_components/$_sc_domain"' in sh, "install.sh: removal is not per-domain"

    assert 'Remove-Item -Recurse -Force "$env:USERPROFILE\\.homeant\\custom_components"' not in ps1, (
        "install.ps1: still removes the whole custom_components directory"
    )
    assert "$haCc $ccDir.Name" in ps1, "install.ps1: removal is not per-domain"


def test_domain_scan_removes_exactly_the_domains_this_tree_ships(tmp_path):
    """Static text assertions cannot prove the scan's behaviour — run the real body.

    Extract `_replace_shipped_components` verbatim from install.sh, point HOME
    at a throwaway HA_ROOT, and let it work on a directory holding all three
    shipped domains plus one a user installed by hand.  A hard-coded list would
    pass this test with two domains and still be wrong.
    """
    import subprocess

    text = _sh()
    start = text.index("_replace_shipped_components() {")
    fn = text[start:text.index("\n}\n", start) + 3]

    home = tmp_path / "home"
    cc = home / ".homeant" / "custom_components"
    cc.mkdir(parents=True)
    shipped = ("homeant_mdns", "homeant_voice", "xiaomi_home")
    for domain in (*shipped, "hand_installed"):
        (cc / domain).mkdir()
        (cc / domain / "keep.txt").write_text("x", encoding="utf-8")
    marker = home / ".homeant" / ".seed-state"
    marker.write_text("x", encoding="utf-8")
    # The previous release's key snapshot must survive: --reinstall drops the
    # seed marker so start redeploys, but needs this to tell a removed script
    # from one the user created in HA's UI.
    snapshot = home / ".homeant" / ".seed-keys.json"
    snapshot.write_text('{"scripts.yaml": ["old_script"]}', encoding="utf-8")

    # `$(pwd)` as the tree: no Windows/POSIX path translation in the glob.
    script = (
        "set -eu\n"
        f'HOME="{home.as_posix()}"; export HOME\n'
        'info() { printf "[install] %s\\n" "$*"; }\n'
        + fn
        + '\n_replace_shipped_components "$(pwd)"\n'
    )
    proc = subprocess.run(
        ["sh", "-c", script], cwd=REPO_ROOT,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"scan failed: {proc.stdout}\n{proc.stderr}"

    reported = {
        domain for domain in (*shipped, "hand_installed")
        if f"deleting custom_components/{domain}" in proc.stdout
    }
    assert reported == set(shipped), f"wrong domain set: {reported}\n{proc.stdout}"
    assert sorted(p.name for p in cc.iterdir()) == ["hand_installed"], (
        "a domain the tree does not ship was removed"
    )
    assert not marker.exists(), "the seed marker must be dropped so start redeploys"
    assert snapshot.read_text(encoding="utf-8") == '{"scripts.yaml": ["old_script"]}', (
        "the redeploy deleted the key snapshot — the next start would keep a "
        "script this release removed"
    )
    assert ".seed-keys.json" not in _ps1_fn_body(_ps1(), "_ReplaceShippedComponents"), (
        "install.ps1: the key snapshot must survive a source-tree --reinstall"
    )
