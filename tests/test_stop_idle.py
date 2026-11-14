"""An idle `stop` must not claim it stopped something.

The defect is a reporting one, and it is invisible to exit codes: every
supervisor answers a stop request with success whether or not a process was
there to receive it — ``systemctl stop`` on an inactive unit exits 0, a
``SERVICE_CONTROL_STOP`` against an already-stopped service succeeds,
``bootout`` of an unloaded label is an idempotent success.  So ``cmd_stop``
used to decide the closing line from *its own request succeeding*, and an idle
``homeant stop`` printed ``hass stopped`` on all three platforms (measured
11-15 on Windows; the same branch shape exists for systemd and launchd).

The fix has to read the state **before** the verb, which is why these cases
assert on the recorded fact rather than on the stop call: after the stop, "it
was running" is no longer answerable by anyone.

Kept in its own file so the three platform branches can be exercised on one
machine: the CLI is imported, never executed, and every supervisor call is
monkeypatched — nothing here touches a real service, a port, or ``~/.homeant``.
"""

from __future__ import annotations

import pytest


class _Result:
    """What ``subprocess.run`` returns for a systemctl call."""

    def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err


class _Sup:
    """Monkeypatch bundle for "one supervisor, in a given state".

    Every name a `stop` path can reach is faked: the platform branch that is
    not under test must stay unreachable, so ``_service_kind`` is what selects
    it and the others are asserted-not-called by simply doing nothing.
    """

    def __init__(self, cli, monkeypatch, tmp_path, kind: str, running: bool):
        self.cli = cli
        self.kind = kind
        self.calls: list[str] = []
        unit = tmp_path / "homeant.service"
        unit.write_text("[Unit]\n", encoding="utf-8")
        monkeypatch.setattr(cli, "UNIT_PATH", unit)
        monkeypatch.setattr(cli, "_service_kind", lambda: kind)
        # No pid is the case that used to be mislabelled; the pid path ends in
        # its own "hass stopped" after a real kill and is unchanged.
        monkeypatch.setattr(cli, "_ha_instance", lambda: None)
        monkeypatch.setattr(cli, "_mqtt_stop", lambda **kw: True)
        monkeypatch.setattr(cli, "_systemctl",
                            lambda *a, **kw: (self.calls.append("systemctl"),
                                              _Result(0))[1])
        monkeypatch.setattr(cli, "_service_active", lambda: running)
        monkeypatch.setattr(cli, "_launchd_active", lambda: running)
        monkeypatch.setattr(cli, "_launchd_bootout",
                            lambda *a, **kw: (self.calls.append("bootout"), True)[1])
        monkeypatch.setattr(cli, "_win_service_installed", lambda: True)
        monkeypatch.setattr(cli, "_win_service_state",
                            lambda: "running" if running else "stopped")
        monkeypatch.setattr(cli, "_win_service_stop",
                            lambda: (self.calls.append("scm-stop"), True)[1])


@pytest.fixture
def idle_systemd(cli, monkeypatch, tmp_path):
    return _Sup(cli, monkeypatch, tmp_path, "systemd-user", running=False)


@pytest.fixture
def busy_systemd(cli, monkeypatch, tmp_path):
    return _Sup(cli, monkeypatch, tmp_path, "systemd-user", running=True)


@pytest.fixture
def idle_windows(cli, monkeypatch, tmp_path):
    return _Sup(cli, monkeypatch, tmp_path, "service", running=False)


@pytest.fixture
def busy_windows(cli, monkeypatch, tmp_path):
    return _Sup(cli, monkeypatch, tmp_path, "service", running=True)


def _stop_lines(capsys) -> list[str]:
    return [ln for ln in capsys.readouterr().out.splitlines() if "hass " in ln]


def test_an_idle_systemd_stop_says_not_running(idle_systemd, capsys):
    """The reported defect, on the platform where it is easiest to reach."""
    assert idle_systemd.cli.cmd_stop(None) == 0
    lines = _stop_lines(capsys)
    assert any("hass not running" in ln for ln in lines), lines
    assert not any("hass stopped" in ln for ln in lines), (
        "an idle stop claimed it stopped a home: " + repr(lines))


def test_a_running_systemd_home_still_says_stopped(busy_systemd, capsys):
    """The fix must not silence the line that a real stop has to print."""
    assert busy_systemd.cli.cmd_stop(None) == 0
    lines = _stop_lines(capsys)
    assert any("hass stopped" in ln for ln in lines), lines


def test_an_idle_windows_service_stop_says_not_running(idle_windows, capsys):
    """`_win_service_stop` returns True for a service that is already stopped.

    That is correct for the verb — the requested state holds — and wrong as a
    report of what just happened.  The distinction has to be made here.
    """
    assert idle_windows.cli.cmd_stop(None) == 0
    lines = _stop_lines(capsys)
    assert any("hass not running" in ln for ln in lines), lines
    assert not any("hass stopped" in ln for ln in lines), repr(lines)


def test_a_running_windows_service_still_says_stopped(busy_windows, capsys):
    assert busy_windows.cli.cmd_stop(None) == 0
    lines = _stop_lines(capsys)
    assert any("hass stopped" in ln for ln in lines), lines


def test_a_supervisor_with_nothing_to_stop_is_asked_only_once(
        idle_windows, monkeypatch):
    """The state is read before the verb, and the verb still runs exactly once.

    Two reads would be harmless but a *missed* read is not: the check has to
    happen while the answer still exists.
    """
    order: list[str] = []
    cli = idle_windows.cli
    real_state = cli._win_service_state

    monkeypatch.setattr(cli, "_win_service_state",
                        lambda: (order.append("query"), real_state())[1])
    monkeypatch.setattr(cli, "_win_service_stop",
                        lambda: (order.append("stop"), True)[1])
    cli.cmd_stop(None)
    assert order == ["query", "stop"], (
        "the running-state query has to come before the stop, got " + repr(order))


def test_the_verdict_does_not_need_the_port(cli, monkeypatch):
    """`_supervisor_running` is the cheap half of `_hass_via_service`.

    If it ever grew the socket probe, every `stop` on Windows would pay the 2s
    refused connect documented on ``port_in_use`` — and the answer would be
    worse, because "active but not listening yet" must still count as running.
    """
    monkeypatch.setattr(cli, "_service_kind", lambda: "service")
    monkeypatch.setattr(cli, "_win_service_state", lambda: "running")

    def boom(port):
        raise AssertionError("a stop must not probe the port")

    monkeypatch.setattr(cli, "port_in_use", boom)
    assert cli._supervisor_running() is True


def test_hass_via_service_still_needs_the_socket(cli, monkeypatch):
    """The refactor kept the difference between "up" and "serving"."""
    monkeypatch.setattr(cli, "_service_kind", lambda: "service")
    monkeypatch.setattr(cli, "_win_service_state", lambda: "running")
    monkeypatch.setattr(cli, "port_in_use", lambda port: False)
    assert cli._hass_via_service() is False
    monkeypatch.setattr(cli, "port_in_use", lambda port: True)
    assert cli._hass_via_service() is True


def test_an_idle_stop_still_stops_the_broker(cli, monkeypatch, tmp_path, capsys):
    """A broker can outlive hass, so "nothing to stop" applies to hass only.

    The exit code follows the broker, not the absent home: leaving 1883
    answering would be half a stop, and rc=0 would promise otherwise.
    """
    monkeypatch.setattr(cli, "UNIT_PATH", tmp_path / "absent.service")
    monkeypatch.setattr(cli, "_service_kind", lambda: "none")
    monkeypatch.setattr(cli, "_ha_instance", lambda: None)
    reached: list[str] = []
    monkeypatch.setattr(cli, "_mqtt_stop",
                        lambda **kw: (reached.append("broker"), False)[1])

    assert cli.cmd_stop(None) == 1
    assert reached == ["broker"], "the broker was not even asked"
    assert any("hass not running" in ln for ln in _stop_lines(capsys))
