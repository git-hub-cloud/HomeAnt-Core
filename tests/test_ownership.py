"""The `sudo start` leftovers, named at the entry instead of a week later.

The defect is a *silent* one, which is why it needs a dedicated guard: after a
single `sudo homeant start`, every file that run created under HA_ROOT belongs
to root, and the next unprivileged run fails somewhere else entirely — HA
answering 500 from a `.storage` it cannot write, a log it cannot reopen, an
installed component frozen at its old bytes.  None of those messages mentions
ownership, so the user reports a different bug each time.

Two rules are pinned here:

* ownership is *reported*, never repaired — a root-owned tree is perfectly
  usable by root, and silently chowning a directory is not this layer's call;
* the verdict is reached without reading a single file's contents, so it costs
  one `walk` on a path `start` runs anyway.

Every case builds its own tree and its own owner map: the CLI is imported, not
executed, and nothing here touches the real ``~/.homeant``, a port or a service.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

ME = 1000
ROOT = 0


class _Home:
    """A throwaway HA_ROOT plus the owner map the test author declares by hand.

    ``owners`` is keyed by posix-relative path, ``"."`` naming HA_ROOT itself —
    the case a `sudo mkdir` produces and the one a shallow check would miss.
    Anything unlisted is owned by ``uid``, so a test only writes down deviations.
    """

    def __init__(self, cli, monkeypatch, tmp_path, *, files=(), owners=None,
                 uid=ME, euid=None):
        self.root = tmp_path / ".homeant"
        self.root.mkdir()
        for rel in files:
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        self.owners = dict(owners or {})
        self.uid = uid
        self.calls = 0
        monkeypatch.setattr(cli, "HA_ROOT", self.root)
        monkeypatch.setattr(cli, "_entry_uid", self.owner)
        monkeypatch.setattr(cli, "_current_ids",
                            lambda: (uid, uid if euid is None else euid, uid))

    def owner(self, path: Path) -> int:
        self.calls += 1
        rel = "." if path == self.root else path.relative_to(self.root).as_posix()
        return self.owners.get(rel, self.uid)


# ── the walk ────────────────────────────────────────────────────────────────

def test_a_tree_that_is_entirely_ours_reports_nothing(tmp_path, monkeypatch, cli):
    home = _Home(cli, monkeypatch, tmp_path,
                 files=[".storage/core.config_entries", "mqtt.yaml"])
    assert cli._ownership_problem() is None
    assert home.calls >= 3


def test_the_root_directory_itself_counts(tmp_path, monkeypatch, cli):
    """`sudo mkdir ~/.homeant` leaves an empty root-owned tree — nothing inside
    it to find, and every write into it still fails.  The verdict is a sentence
    about the directory, not an inventory of entries."""
    home = _Home(cli, monkeypatch, tmp_path, owners={".": ROOT})
    problem = cli._ownership_problem()
    assert problem is not None
    assert "uid 0" in problem[0]
    assert "nothing under it can be written" in problem[0]
    assert problem[1] == f"sudo chown -R {ME}:{ME} {home.root}"


def test_the_tree_is_named_and_nothing_inside_it(tmp_path, monkeypatch, cli):
    """One directory, one owner: no tally and no entry path.  The count needs a
    cap to stay cheap, the cap makes it a lie on a big tree, and naming entries
    only invites the reader to check those entries and stop."""
    home = _Home(cli, monkeypatch, tmp_path,
                 files=[".storage/core.config_entries", "mqtt.yaml"],
                 owners={".storage/core.config_entries": ROOT})
    problem = cli._ownership_problem()
    assert problem is not None
    assert problem[0].startswith(f"{home.root} do not belong to this account")
    assert "entries" not in problem[0]
    assert "core.config_entries" not in problem[0]


def test_a_big_tree_still_produces_one_short_line(tmp_path, monkeypatch, cli):
    files = [f"d{i}/f{i}" for i in range(6)]
    _Home(cli, monkeypatch, tmp_path, files=files,
          owners={name: ROOT for name in files})
    count, root_foreign, uids = cli._foreign_owners(cli.HA_ROOT, ME, cli._entry_uid)
    assert (count, root_foreign, uids) == (6, False, {ROOT})
    problem = cli._ownership_problem()
    assert problem is not None
    assert problem[0] == (f"{cli.HA_ROOT} do not belong to this account — "
                          f"{cli._uid_name(ROOT, number=False)}")


def test_the_walk_stops_at_the_entry_cap(tmp_path, monkeypatch, cli):
    files = [f"d{i}/f{i}" for i in range(50)]
    home = _Home(cli, monkeypatch, tmp_path, files=files)
    count, _, _ = cli._foreign_owners(
        cli.HA_ROOT, ME, home.owner, max_entries=5)
    assert home.calls <= 6          # the root, plus the cap, and nothing more
    assert count == 0


# ── the verdict ─────────────────────────────────────────────────────────────

def test_a_root_installed_tree_run_as_root_is_not_a_fault(tmp_path, monkeypatch, cli):
    """Installing as root is a choice, not a mistake: the tree is root's and the
    caller is root, so nothing here is out of reach and `start` must proceed.
    Only a *mismatch* between the tree's owner and the caller is a fault."""
    _Home(cli, monkeypatch, tmp_path,
          files=[".storage/core.config_entries", ".mqtt.pid"],
          uid=ROOT, euid=ROOT)
    assert cli._ownership_problem() is None


def test_running_as_root_on_a_users_tree_says_drop_the_sudo(tmp_path, monkeypatch, cli):
    """The other direction of the same mistake, caught *before* it writes: root
    would succeed once and leave the trail this module exists to find.  So the
    tree is the user's and the caller is root — the exact `sudo start` shape."""
    _Home(cli, monkeypatch, tmp_path, files=["mqtt.yaml"],
          owners={"mqtt.yaml": ME}, uid=ROOT, euid=ROOT)
    problem = cli._ownership_problem()
    assert problem is not None
    assert "running as root" in problem[0]
    assert "uid 1000" in problem[0]
    assert "drop the sudo" in problem[1]
    assert "chown" not in problem[1]


def test_root_on_a_tree_it_does_not_own_names_the_whole_directory(tmp_path, monkeypatch, cli):
    _Home(cli, monkeypatch, tmp_path, owners={".": ME}, uid=ROOT, euid=ROOT)
    problem = cli._ownership_problem()
    assert problem is not None
    assert "everything written here would become root's" in problem[0]
    assert "drop the sudo" in problem[1]


def test_the_fix_is_a_copy_pasteable_line_with_the_account_numbers(tmp_path, monkeypatch, cli):
    home = _Home(cli, monkeypatch, tmp_path, files=["mqtt.yaml"],
                 owners={"mqtt.yaml": ROOT})
    problem = cli._ownership_problem()
    assert problem is not None
    assert problem[1] == f"sudo chown -R {ME}:{ME} {home.root}"


def test_the_fix_shows_the_tilde_when_the_tree_is_under_home(tmp_path, monkeypatch, cli):
    home = _Home(cli, monkeypatch, tmp_path, files=["mqtt.yaml"],
                 owners={"mqtt.yaml": ROOT})
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    problem = cli._ownership_problem()
    assert problem is not None
    assert problem[1] == f"sudo chown -R {ME}:{ME} ~/.homeant"
    assert str(home.root) not in problem[0]


def test_a_platform_without_ownership_is_never_asked(tmp_path, monkeypatch, cli):
    """Windows: no per-file owner to compare, and the ACL makes the question
    moot — the question must not even be asked there."""
    _Home(cli, monkeypatch, tmp_path, files=["mqtt.yaml"],
          owners={"mqtt.yaml": ROOT})
    monkeypatch.setattr(cli, "_current_ids", lambda: None)
    assert cli._ownership_problem() is None


def test_a_missing_home_is_not_a_problem(tmp_path, monkeypatch, cli):
    monkeypatch.setattr(cli, "HA_ROOT", tmp_path / "nowhere")
    monkeypatch.setattr(cli, "_current_ids", lambda: (ME, ME, ME))
    assert cli._ownership_problem() is None


# ── the two ways a user meets it ────────────────────────────────────────────

def test_a_clean_home_prints_nothing(cli, capsys):
    assert cli._log_ownership(None, "WARN") is False
    assert capsys.readouterr().out == ""


def test_the_warning_and_the_fix_go_out_together(cli, capsys):
    assert cli._log_ownership(("3 entries belong to root", "sudo chown -R 1:1 x"),
                              "WARN") is True
    lines = capsys.readouterr().out.splitlines()
    assert "WARN  home: 3 entries belong to root" in lines[0]
    assert lines[1].endswith("run: sudo chown -R 1:1 x")


def test_start_refuses_before_any_stage_runs(tmp_path, monkeypatch, cli, capsys):
    """The gate sits ahead of the version probe, the dependency check and every
    write, so a poisoned tree costs one `chown` instead of a diagnosis."""
    def boom(*a, **kw):
        raise AssertionError("a stage ran despite the ownership gate")
    for name in ("_check_newer_version", "_deps_why", "_service_kind",
                 "_deploy_config_files"):
        monkeypatch.setattr(cli, name, boom)
    monkeypatch.setattr(cli, "_ownership_problem",
                        lambda: ("tree belongs to root", "sudo chown -R 1:1 x"))

    args = types.SimpleNamespace(foreground=False, service_host=False,
                                 no_upgrade=True)
    assert cli.cmd_start(args) == 1
    out = capsys.readouterr().out
    assert "FAIL  home: tree belongs to root" in out
    assert "run: sudo chown -R 1:1 x" in out


def test_the_supervised_path_warns_instead_of_refusing(tmp_path, monkeypatch, cli):
    """The unit/SCM path is restarted by its supervisor, so the same verdict has
    to be a line in the log rather than an exit — otherwise one readable
    message becomes a crash loop.  Reached before any instance check, which is
    what keeps this test from spawning anything."""
    seen: list[str] = []
    monkeypatch.setattr(cli, "_log_ownership",
                        lambda problem, level: seen.append(level))
    monkeypatch.setattr(cli, "_ownership_problem",
                        lambda: ("tree belongs to root", "sudo chown -R 1:1 x"))
    monkeypatch.setattr(cli, "_win_job_arm", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "HA_ROOT", tmp_path / ".homeant")
    monkeypatch.setattr(cli, "_ha_instance", lambda: 42)   # stop right after
    assert cli._run_foreground() == 1
    assert seen == ["WARN"]


def test_status_reports_it_as_a_warning_without_changing_the_exit_code(tmp_path, monkeypatch, cli, capsys):
    """`status` must not fail on it: the home may be serving fine right now.
    Only the next write notices, and that is what the line is warning about."""
    home = _Home(cli, monkeypatch, tmp_path, files=["configuration.yaml"],
                 owners={"configuration.yaml": ROOT})
    monkeypatch.setattr(cli, "_seed_present", lambda: True)
    monkeypatch.setattr(cli, "port_in_use", lambda port: False)
    monkeypatch.setattr(cli, "_service_kind", lambda: "none")
    monkeypatch.setattr(cli, "_autostart_state", lambda: None)
    monkeypatch.setattr(cli, "_mqtt_gate", lambda: "no broker")
    monkeypatch.setattr(cli, "_xiaomi_already_configured", lambda: False)
    monkeypatch.setattr(cli, "XIAOMI_FLOW_FILE", tmp_path / "no-flow.json")

    cli.cmd_status(types.SimpleNamespace())
    out = capsys.readouterr().out
    assert "WARN  home: " in out
    assert "do not belong to this account" in out
    assert f"sudo chown -R {ME}:{ME} {home.root}" in out
    assert "FAIL  home:" not in out
