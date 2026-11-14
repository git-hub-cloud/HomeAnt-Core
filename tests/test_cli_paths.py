"""Console output names paths the reader can act on, never this machine's home.

The rule this pins down: every line ``start`` / ``status`` / ``uninstall``
prints is what ends up in a bug report, a screenshot, a saved terminal log or a
paste into an issue.  ``HA_ROOT`` is always ``Path.home() / ".homeant"`` and
``PROJECT_ROOT`` is usually under the same home, so printing them raw spends the
reader's attention on a user name they already know — and hands that name to
whoever reads the paste.  ``~/.homeant`` says the same thing in eight columns,
and it is what both READMEs show.

Two forms exist because two shapes occur: ``_home_tilde`` for a single ``Path``
and ``_tilde_text`` for a finished sentence or a service command line carrying
several.  Both must agree — one line with ``~`` and the next with
``C:\\Users\\someone`` reads like a bug.

The carve-outs are deliberate and are asserted here too, so a later "cleanup"
cannot quietly widen the rule:

* the ``removing <dir>`` record of ``uninstall`` keeps the absolute path — it is
  written *before* an irreversible deletion and is the only account of what was
  there afterwards;
* the systemd unit, the launchd plist and the Windows service environment hold
  absolute paths by necessity: the OS starts those processes with no cwd and no
  shell to expand a ``~``.

Display only: nothing here may change an argument handed to a process.

The installers follow the same rule through their own `_tilde` / `_Tilde`
(they are POSIX sh and PowerShell, so they cannot call into the CLI): the tree
they relocate into is announced as `~/homeant`, not `/Users/<someone>/homeant`,
and both platforms hand over the same sentence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from gate_paths import REPO_ROOT

INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"


def test_home_tilde_shortens_a_path_under_home(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert cli._home_tilde(tmp_path / ".homeant") == "~/.homeant"
    # Nested, and posix separators even on Windows: the two READMEs show the
    # same shape, so a `~\\Documents\\...` answer would be a second dialect.
    assert cli._home_tilde(tmp_path / "a" / "b") == "~/a/b"


def test_home_tilde_leaves_a_path_outside_home_alone(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    outside = tmp_path / "elsewhere"
    # No `~` is possible here: this path is not under this user's home, and
    # inventing one would make the line unusable for diagnosis.
    assert cli._home_tilde(outside) == str(outside)


def test_home_tilde_renders_home_itself_as_one_tilde(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # `~/.` and `~/` both appear if the relative part is allowed to be empty.
    assert cli._home_tilde(tmp_path) == "~"


def test_tilde_text_rewrites_both_spellings_of_the_same_path(cli, monkeypatch,
                                                            tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # The same root reaches this process with either separator; Git Bash rewrites
    # backslashes on the way in, and the SCM's ImagePath stores them on the way
    # out.  Both spellings therefore have to collapse, or the rule holds for
    # half the lines.
    win = (f'"{tmp_path}\\'
           f'.venv\\Scripts\\python.exe" "{tmp_path}\\scripts\\homeant.py"')
    posix = f"{tmp_path.as_posix()}/scripts/homeant.py"
    tilted = (f'"~\\'
              f'.venv\\Scripts\\python.exe" "~\\scripts\\homeant.py"')
    assert cli._tilde_text(win) == tilted
    assert cli._tilde_text(posix) == "~/scripts/homeant.py"


def test_start_announces_the_deployment_with_a_tilde(cli, monkeypatch, tmp_path):
    """The line every `start` prints, which is the one people paste the most."""
    fake_home = tmp_path / "home"
    root = fake_home / ".homeant"
    root.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(cli, "HA_ROOT", root)
    monkeypatch.setattr(cli, "SEED_MARKER", tmp_path / "unused.seed")
    lines: list[str] = []
    monkeypatch.setattr(cli, "log", lines.append)

    cli._deploy_config_files()

    assert "deploying config → ~/.homeant" in lines, (
        f"start named the home directory literally: {lines}"
    )
    assert not any(str(fake_home) in line for line in lines), (
        f"a deploy line still carried the absolute home: {lines}"
    )


def test_the_units_that_boot_the_machine_keep_absolute_paths(cli):
    """The carve-out, asserted: the OS expands no `~` when it starts a service.

    `ExecStart` runs with no cwd and no shell, the launchd plist is read by
    launchd itself, and the Windows service gets the interpreter path through
    the registry — a tilde in any of them is a service that never starts.
    """
    unit = cli._render_unit()[0]
    assert f'WorkingDirectory={REPO_ROOT}' in unit
    assert "~" not in unit


def test_the_deletion_record_keeps_the_absolute_path(cli):
    """The one console line that must not shorthand: it describes a deletion.

    Written before the tree is removed, so after it runs this line is the only
    account of what was there — and a record that needs the reader to resolve
    `~` against a home that may since have moved is not a record.
    """
    source = (REPO_ROOT / "scripts" / "homeant.py").read_text(encoding="utf-8")
    assert 'log(f"removing {HA_ROOT}' in source, (
        "the uninstall record lost its absolute path — see _home_tilde's docstring"
    )


def test_the_installer_announces_the_tree_it_relocates_into_with_a_tilde():
    """`[install] installing into ~/homeant`, the same words on every platform.

    Run the real `_tilde` body from install.sh verbatim: it is pure string work,
    so no home has to exist.  Trailing slash on `$HOME` is included because that
    is the shape a naive `$HOME/*` test misses — the path then falls through to
    "outside home" and prints raw, which is the bug this pins.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    start = text.index("_tilde() {")
    fn = text[start:text.index("\n}\n", start) + 3]

    script = (
        "set -eu\n"
        'HOME="/home/probe/"\n'
        + fn
        + "\n"
        # `_tilde` writes no newline of its own — it is used inside messages,
        # so the probe supplies the separator.
        + '_tilde /home/probe/homeant; echo\n'
        + '_tilde /home/probe; echo\n'
        + '_tilde /opt/homeant; echo\n'
        # A sibling that merely shares the prefix must not be shortened.
        + '_tilde /home/probex; echo\n'
    )
    proc = subprocess.run(["sh", "-c", script], capture_output=True,
                          text=True, check=False, cwd=REPO_ROOT)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["~/homeant", "~", "/opt/homeant",
                                   "/home/probex"], proc.stdout


def test_both_installers_route_their_tree_paths_through_the_helper():
    """Static half: neither file may print the install tree raw again.

    PowerShell cannot be executed by the gate on every platform, so its half is
    the text contract — a function of its own, and the same lines wrapped.  The
    two installers are meant to be one installer in two languages and have
    drifted before; this is one of the sentences users actually read.
    """
    sh = INSTALL_SH.read_text(encoding="utf-8")
    ps1 = INSTALL_PS1.read_text(encoding="utf-8")
    assert "_tilde() {" in sh, "install.sh lost its path shortener"
    assert "function _Tilde" in ps1, "install.ps1 lost its path shortener"
    for raw in (
        "installing into $_here",
        "installing into $__newDir",
        "HomeAnt source found in $_here",
        "HomeAnt source found in $here",
        "upgrade target is not a HomeAnt tree: $_here",
        "upgrade target is not a HomeAnt tree: $here",
        "cannot create install directory: $_here",
        "cannot create install directory: $__newDir",
    ):
        assert raw not in sh and raw not in ps1, (
            f"an installer prints the tree raw again: {raw}"
        )
