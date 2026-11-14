"""Best-effort firewall exceptions registered on `start`.

The code under test writes OS firewall rules, so every case here replaces the
command runner (``_fw_run``) or the privilege probe — no case touches a real
netsh / ufw / iptables.  What is pinned is the decision logic: look before
adding (idempotence), prefer the first available backend, never sit on a sudo
password prompt, and turn every failure into silence rather than a blocked
start.
"""

from __future__ import annotations

import types

import pytest


def _calls_recorder():
    calls: list[list[str]] = []

    def record(cmd):
        calls.append(list(cmd))
        return 0, ""

    return calls, record


# ── _fw_run: failures become data ───────────────────────────────────────────

def test_fw_run_swallows_subprocess_failure(cli, monkeypatch):
    def boom(*a, **kw):
        raise OSError("no such tool")

    monkeypatch.setattr(cli.subprocess, "run", boom)
    assert cli._fw_run(["whatever"]) == (1, "")


def test_fw_run_joins_stdout_and_stderr(cli, monkeypatch):
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda *a, **kw: types.SimpleNamespace(returncode=0, stdout=" out \n", stderr=" err \n"),
    )
    rc, out = cli._fw_run(["whatever"])
    assert rc == 0
    assert "out" in out and "err" in out


# ── Windows: idempotent by rule name ────────────────────────────────────────

def test_win_adds_only_missing_rules(cli, monkeypatch, capsys):
    calls = []

    def fake_run(cmd):
        calls.append(list(cmd))
        # 'show' probe: 8123 already allowed, 1883 missing.
        if "show" in cmd:
            if "HomeAnt-8123" in " ".join(cmd):
                return 0, "HomeAnt-8123"
            return 1, "No rules match."
        return 0, ""

    monkeypatch.setattr(cli, "_fw_run", fake_run)
    cli._fw_windows([8123, 1883])

    adds = [c for c in calls if "add" in c]
    assert len(adds) == 1
    assert "localport=1883" in " ".join(adds[0])
    assert "tcp/1883" in capsys.readouterr().out


def test_win_a_failed_add_does_not_raise(cli, monkeypatch):
    def fake_run(cmd):
        if "show" in cmd:
            return 1, "No rules match."
        return 1, "Access is denied."

    monkeypatch.setattr(cli, "_fw_run", fake_run)
    cli._fw_windows([8123])          # must not raise, must not block start


# ── privilege: empty prefix as root, sudo -n else none ──────────────────────

def test_privilege_root_needs_no_prefix(cli, monkeypatch):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0, raising=False)
    assert cli._fw_privilege() == []


def test_privilege_uses_non_interactive_sudo(cli, monkeypatch):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
    calls = []

    def fake_run(cmd):
        calls.append(list(cmd))
        return 0, ""

    monkeypatch.setattr(cli, "_fw_run", fake_run)
    assert cli._fw_privilege() == ["sudo", "-n"]
    assert calls == [["sudo", "-n", "true"]]


def test_privilege_no_sudo_returns_none(cli, monkeypatch):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(cli, "_fw_run", lambda cmd: (1, "password required"))
    assert cli._fw_privilege() is None


# ── Linux: first backend wins, all idempotent ───────────────────────────────

def test_linux_no_privilege_is_silent_noop(cli, monkeypatch):
    calls, record = _calls_recorder()
    monkeypatch.setattr(cli, "_fw_privilege", lambda: None)
    monkeypatch.setattr(cli, "_fw_run", record)
    cli._fw_linux([8123])
    assert calls == []


def test_linux_no_backend_is_silent_noop(cli, monkeypatch):
    calls, record = _calls_recorder()
    monkeypatch.setattr(cli, "_fw_privilege", lambda: [])
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(cli, "_fw_run", record)
    cli._fw_linux([8123])
    assert calls == []


def test_linux_ufw_skips_existing_port(cli, monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append(list(cmd))
        if cmd[0:2] == ["ufw", "status"]:
            return 0, "8123/tcp                   ALLOW IN    Anywhere"
        return 0, ""

    monkeypatch.setattr(cli, "_fw_privilege", lambda: [])
    monkeypatch.setattr(cli.shutil, "which",
                        lambda name: "/usr/sbin/ufw" if name == "ufw" else None)
    monkeypatch.setattr(cli, "_fw_run", fake_run)
    cli._fw_linux([8123, 1883])

    allows = [c for c in calls if "allow" in c]
    assert len(allows) == 1
    assert allows[0][-1] == "1883/tcp"


def test_linux_firewalld_preferred_over_iptables(cli, monkeypatch):
    calls = []

    def fake_which(name):
        return "/usr/bin/firewall-cmd" if name == "firewall-cmd" else None

    monkeypatch.setattr(cli, "_fw_privilege", lambda: [])
    monkeypatch.setattr(cli.shutil, "which", fake_which)
    monkeypatch.setattr(cli, "_fw_run", lambda cmd: calls.append(list(cmd)) or (0, ""))
    cli._fw_linux([8123])

    assert any("firewall-cmd" in c for c in calls)
    assert not any("iptables" in c for c in calls)


def test_linux_iptables_check_before_insert(cli, monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append(list(cmd))
        return (0, "") if "-C" in cmd else (1, "fail")

    monkeypatch.setattr(cli, "_fw_privilege", lambda: ["sudo", "-n"])
    monkeypatch.setattr(cli.shutil, "which",
                        lambda name: "/usr/sbin/iptables" if name == "iptables" else None)
    monkeypatch.setattr(cli, "_fw_run", fake_run)
    cli._fw_linux([8123])

    check = next(c for c in calls if "-C" in c)
    assert check[:3] == ["sudo", "-n", "iptables"]
    assert not any("-I" in c for c in calls)   # rule exists -> no insert


# ── platform dispatch ───────────────────────────────────────────────────────

def test_dispatch_windows_only_when_admin(cli, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "IS_WIN", True)
    monkeypatch.setattr(cli, "_mqtt_conf", lambda: {"port": 1883})
    monkeypatch.setattr(cli, "_is_admin", lambda: False)
    monkeypatch.setattr(cli, "_fw_windows", lambda ports: seen.append(("win", ports)))
    monkeypatch.setattr(cli, "_fw_linux", lambda ports: seen.append(("linux", ports)))

    cli._firewall_ensure()
    assert seen == []

    monkeypatch.setattr(cli, "_is_admin", lambda: True)
    cli._firewall_ensure()
    assert seen == [("win", [8123, 1883])]


def test_dispatch_filters_invalid_mqtt_port(cli, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "IS_WIN", True)
    monkeypatch.setattr(cli, "_is_admin", lambda: True)
    monkeypatch.setattr(cli, "_mqtt_conf", lambda: {"port": "not-a-port"})
    monkeypatch.setattr(cli, "_fw_windows", lambda ports: seen.append(ports))

    cli._firewall_ensure()
    assert seen == [[8123]]


# ── idempotence across repeated `start` (a persistent rule store) ───────────

def test_win_repeated_start_never_duplicates_rules(cli, monkeypatch):
    """Three starts leave exactly one rule per port.

    netsh keys rules by name, so a same-named add is refused by the OS even if
    the show probe were fooled — but the probe is the first line of defence,
    and this test exercises it against a fake rule store that persists.
    """
    rules: set[str] = set()
    adds: list[str] = []

    def fake_run(cmd):
        name = next((a.split("=", 1)[1] for a in cmd if a.startswith("name=")), "")
        if "show" in cmd:
            if name in rules:
                return 0, f"Rule Name: {name}\nEnabled: Yes"
            return 1, "No rules match the specified criteria."
        if "add" in cmd:
            adds.append(name)
            rules.add(name)
            return 0, ""
        return 0, ""

    monkeypatch.setattr(cli, "_fw_run", fake_run)
    for _ in range(3):
        cli._fw_windows([8123, 1883])

    assert rules == {"HomeAnt-8123", "HomeAnt-1883"}
    assert adds == ["HomeAnt-8123", "HomeAnt-1883"]   # added once, ever


def test_ufw_repeated_start_never_duplicates(cli, monkeypatch):
    rules: set[str] = set()
    allows: list[str] = []

    def fake_run(cmd):
        if cmd[:2] == ["ufw", "status"]:
            text = "Status: active\n" + "\n".join(
                f"{r}  ALLOW IN  Anywhere" for r in sorted(rules))
            return 0, text
        if cmd[:2] == ["ufw", "allow"]:
            allows.append(cmd[2])
            rules.add(cmd[2])
            return 0, ""
        return 0, ""

    monkeypatch.setattr(cli, "_fw_privilege", lambda: [])
    monkeypatch.setattr(cli.shutil, "which",
                        lambda n: "/usr/sbin/ufw" if n == "ufw" else None)
    monkeypatch.setattr(cli, "_fw_run", fake_run)
    for _ in range(3):
        cli._fw_linux([8123, 1883])

    assert allows == ["8123/tcp", "1883/tcp"]


def test_firewalld_repeated_start_never_duplicates(cli, monkeypatch):
    rules: set[str] = set()
    adds: list[str] = []

    def fake_run(cmd):
        joined = " ".join(cmd)
        if "--query-port=" in joined:
            port = next(a.split("=", 1)[1] for a in cmd if a.startswith("--query-port="))
            return (0, "") if port in rules else (1, "no")
        if "--add-port=" in joined:
            port = next(a.split("=", 1)[1] for a in cmd if a.startswith("--add-port="))
            adds.append(port)
            rules.add(port)
            return 0, ""
        return 0, ""

    monkeypatch.setattr(cli, "_fw_privilege", lambda: [])
    monkeypatch.setattr(cli.shutil, "which",
                        lambda n: "/usr/bin/firewall-cmd" if n == "firewall-cmd" else None)
    monkeypatch.setattr(cli, "_fw_run", fake_run)
    for _ in range(3):
        cli._fw_linux([8123, 1883])

    assert adds == ["8123/tcp", "1883/tcp"]


def test_iptables_repeated_start_never_duplicates(cli, monkeypatch):
    rules: set[str] = set()
    inserts: list[str] = []

    def fake_run(cmd):
        if "-C" in cmd:
            port = cmd[cmd.index("--dport") + 1]
            return (0, "") if port in rules else (1, "no")
        if "-I" in cmd:
            port = cmd[cmd.index("--dport") + 1]
            inserts.append(port)
            rules.add(port)
            return 0, ""
        return 0, ""

    monkeypatch.setattr(cli, "_fw_privilege", lambda: [])
    monkeypatch.setattr(cli.shutil, "which",
                        lambda n: "/usr/sbin/iptables" if n == "iptables" else None)
    monkeypatch.setattr(cli, "_fw_run", fake_run)
    for _ in range(3):
        cli._fw_linux([8123, 1883])

    assert inserts == ["8123", "1883"]
