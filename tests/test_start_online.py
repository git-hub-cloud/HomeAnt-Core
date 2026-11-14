"""`start --no-upgrade / --reinstall`: which stage each one runs.

The two switches override the default ("probe the release host"), which is why
they differ only in what happens *before* the dependency gate; each test stops
the flow there and records the calls: no host, no network, no tree.  The
dependency gate answers "broken" on purpose — it is the first place
`--no-upgrade` and the default disagree in a way a test can see, and it returns
before anything is written.  There is no positive form of the default: probing
needs no spelling, and the help test pins that.

The pair is named after the install, not the network: handing over to the
installer *is* an upgrade, so the installer's own tail says `--no-upgrade` and
the user's override is `--reinstall` — opposite directions, same axis.

The last group covers the source-tree branch: a checkout hands over to its own
impression of the installer, because the released one would unpack over work
that exists nowhere else.

The mutual-exclusion half is judged by argparse itself, in a subprocess: the
parser is built inside ``main()``, and a test that rebuilt it here would be
testing its own copy instead of the shipped one.
"""

from __future__ import annotations

import subprocess
import sys
import types

import pytest

from gate_cli import CLI_PATH


# Any sentence will do where the source-tree branch is the thing under test: it
# replaces the caller's wording with its own line, so the text never surfaces.
ONLINE = "installing 0.2.0 — handing over to the online installer ..."


def _args(**kw) -> types.SimpleNamespace:
    base = dict(foreground=False, service_host=False,
                no_upgrade=False, reinstall=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


class _Rec:
    """What `start` asked for, and what it was told."""

    def __init__(self) -> None:
        self.probes = 0
        self.deps = 0
        self.installs: list[tuple] = []
        self.newer: str | None = None
        self.rc = 0

    def start(self, cli, **kw) -> int:
        return cli.cmd_start(_args(**kw))


@pytest.fixture
def rec(cli, monkeypatch):
    """Patch every stage `start` reaches on this path; record instead of run.

    The ownership gate answers "clean" — a root-owned tree would return before
    the version check and every test here would measure the wrong thing.
    """
    r = _Rec()

    def probe():
        r.probes += 1
        return r.newer

    def installer(message):
        # One positional, by signature — a recorder that took **kw would hide
        # the extra flags this refactor removed.
        r.installs.append(message)
        return r.rc

    def deps():
        r.deps += 1
        return "uv reports the environment as outdated"

    monkeypatch.setattr(cli, "_ownership_problem", lambda: None)
    monkeypatch.setattr(cli, "_check_newer_version", probe)
    monkeypatch.setattr(cli, "_run_installer", installer)
    monkeypatch.setattr(cli, "_deps_why", deps)
    return r


# ── the two modes, and the default they override ────────────────────────────

def test_no_upgrade_never_probes(cli, rec, capsys):
    """The installer's own tail: upgrading here would reopen
    py → installer → py. A broken venv is then a FAIL with the installer URL,
    not another round."""
    assert rec.start(cli, no_upgrade=True) == 1
    assert (rec.probes, rec.deps, rec.installs) == (0, 1, [])
    out = capsys.readouterr().out
    assert "FAIL: dependencies incomplete" in out
    assert "Re-run the installer" in out


def test_the_default_probes_and_repairs_through_the_installer(cli, rec):
    """No flag means "check the release host"; a broken venv is handed over
    rather than repaired in place.

    The hand-over carries one sentence and nothing else: the installer runs the
    same sequence whichever reason got us here, so the reason is the caller's
    to say — it is the only thing that differs."""
    assert rec.start(cli) == 0
    assert (rec.probes, rec.deps) == (1, 1)
    assert rec.installs == [
        "dependencies incomplete — handing over to the online installer ..."]


def test_a_newer_release_is_installed_by_version(cli, rec):
    rec.newer = "0.2.0"
    rec.rc = 7
    assert rec.start(cli) == 7
    assert rec.installs == [
        "installing 0.2.0 — handing over to the online installer ..."]


def test_reinstall_hands_over_without_asking_anything(cli, rec):
    """The user asked for the release, not for a verdict: no probe, no version
    comparison, no prompt — and the dependency gate is never reached, because
    the installer is the thing that would fix it."""
    rec.rc = 7
    assert rec.start(cli, reinstall=True) == 7
    assert (rec.probes, rec.deps) == (0, 0)
    assert rec.installs == ["re-installing from the online installer ..."]


# ── a source tree hands over to its own copy ────────────────────────────────

def test_a_source_tree_redeploys_in_place_instead_of_downloading(cli, monkeypatch):
    """`.git` present means the released installer would unpack over work that
    exists nowhere else, so the copy inside the tree takes the hand-over: same
    flag, and there it means "make the deployment match these files"."""
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli, "_local_installer",
                        lambda: ["sh", "/tree/scripts/install.sh", "--upgrade"])
    monkeypatch.setattr(cli, "subprocess",
                        types.SimpleNamespace(run=fake_run, DEVNULL=-3))

    assert cli._run_installer(ONLINE) == 0
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv[-1] == "--upgrade"
    assert kw["cwd"] == str(cli.PROJECT_ROOT)


def test_the_in_tree_installer_exit_code_is_the_answer(cli, monkeypatch, capsys):
    """Same rule as the online path: once the hand-over ran, the files under
    our feet may already be new, so its exit code is the honest answer."""
    monkeypatch.setattr(cli, "_local_installer", lambda: ["sh", "/x/install.sh"])
    monkeypatch.setattr(
        cli, "subprocess",
        types.SimpleNamespace(
            run=lambda argv, **kw: types.SimpleNamespace(returncode=9),
            DEVNULL=-3))

    assert cli._run_installer(ONLINE) == 9
    out = capsys.readouterr().out
    assert "source tree — redeploying this tree in place" in out
    # The caller's sentence would be a lie here: this branch never reaches the
    # online installer, so it is not printed at all.
    assert "online installer" not in out


def test_local_installer_needs_both_a_checkout_and_its_script(cli, tmp_path,
                                                              monkeypatch):
    """Two conditions, and neither alone is enough: `.git` says this is a
    checkout (the release zip has none), the script says the copy is there."""
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    assert cli._local_installer() is None

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / ("install.ps1" if cli.IS_WIN else "install.sh")).write_text(
        "exit 0\n", encoding="utf-8")
    assert cli._local_installer() is None

    # a worktree's `.git` is a file, not a directory — exists() is the test
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    got = cli._local_installer()
    assert got is not None and got[-1] == "--upgrade"


def test_the_hand_over_takes_one_sentence(cli):
    """One parameter, named for what it is.

    The three callers differ only in the words they print; the sequence
    (download → stop → replace → sync → start) lives in the installer and must
    not grow a flag here that changes it — that is how an upgrade starts
    behaving unlike a fresh install. A signature says this more reliably than a
    grep over the body, and it survives renames.
    """
    import inspect

    assert list(inspect.signature(cli._run_installer).parameters) == ["message"]


# ── the parser ──────────────────────────────────────────────────────────────

def _cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CLI_PATH), *argv],
                          capture_output=True, text=True, timeout=120)


def test_the_two_switches_exclude_each_other():
    """The two opposite overrides on one command line is a usage error, decided
    by argparse — not by the order of the `if`s in cmd_start."""
    proc = _cli("start", "--no-upgrade", "--reinstall")
    assert proc.returncode == 2
    assert "not allowed with" in proc.stderr


def test_help_is_one_screen_everywhere():
    """Six commands, one help text.

    Five of them take no options, so a page per command would print a usage line
    and nothing else. `-h` stays accepted (the habit) and prints the top-level
    text, so there is one place to keep true.
    """
    top = _cli("--help")
    assert top.returncode == 0
    for argv in (("start", "--help"), ("status", "-h"), ("uninstall", "--help")):
        proc = _cli(*argv)
        assert proc.returncode == 0
        assert proc.stdout == top.stdout
    assert "--reinstall" in top.stdout
    # The brace list once, not twice: argparse would print it in `usage:` and
    # again as the positional heading. The usage line is suppressed outright —
    # a `<command>` placeholder would say less than the list two lines below it.
    assert top.stdout.count("{start,status,stop,disable,logs,uninstall}") == 1
    assert "usage:" not in top.stdout
    assert top.stdout.startswith("[homeant] HomeAnt — Control home through Agent.")
    for internal in ("--no-upgrade", "--foreground", "--service-host"):
        assert internal not in top.stdout
    assert "--no-install" not in top.stdout


def test_the_user_switch_is_still_parsed():
    """`add_help=False` removed the per-command page, not the command's options:
    `--reinstall` plus `--help` parses without a usage error."""
    proc = _cli("start", "--reinstall", "--help")
    assert proc.returncode == 0
    assert proc.stdout.startswith("[homeant] HomeAnt — Control home through Agent.")


def _pyproject_version() -> str:
    import re
    text = (CLI_PATH.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text).group(1)


def test_every_entry_point_opens_with_the_same_two_lines():
    """`homeant status` and `homeant --help` start with the same banner — same
    program, and "which build?" answered without being asked.

    There is no `--version` on purpose: asking is not a separate verb. `status`
    is the read-only verb, so the check costs nothing and changes nothing.
    """
    ver = _pyproject_version()
    expected = ["[homeant] HomeAnt — Control home through Agent.",
                f"[homeant] version {ver}"]
    run = _cli("status").stdout.splitlines()
    helped = _cli("--help").stdout.splitlines()
    assert [ln.strip() for ln in run[:2]] == expected
    assert [ln.strip() for ln in helped[:2]] == expected
    assert _cli("--version").returncode == 2


def test_a_bare_invocation_prints_the_screen_and_fails():
    """rc=2 keeps "no verb typed" an error for scripts; the body is the full
    screen, because with the usage line suppressed argparse's own complaint
    would name a placeholder nobody can act on."""
    bare = _cli()
    assert bare.returncode == 2
    assert bare.stdout == _cli("--help").stdout
