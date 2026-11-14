"""Broker ownership decided without an administrator.

The defect these cases pin was found on a real install, not imagined: a Windows
service runs hass and the broker as LocalSystem, so a plain user's prompt cannot
read their command lines (``cmdline`` → AccessDenied).  ``_broker_is_ours`` used
to accept a command line as its *only* evidence, so an unelevated ``status``
reported "served by another program" and an unelevated ``stop`` refused to touch
the broker — both wrong, on a perfectly healthy home.  It also made ``start``
fail with "the service owns it".

What survives a restricted token was measured 11-16 on this box against the
SYSTEM-owned broker: ``name``, ``ppid``, ``create_time``, ``children`` and
``net_connections`` all read; only ``cmdline`` (and ``exe``, which comes back
mangled) do not.  Those are the facts the second tier is built on.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture
def mqtt(tmp_path, monkeypatch, cli):
    """Rebase the broker's files onto a throwaway HA_ROOT."""
    root = tmp_path / ".homeant"
    root.mkdir()
    monkeypatch.setattr(cli, "HA_ROOT", root)
    return cli._mqtt_paths()


def _fake(cli, monkeypatch, *, cmdline="", name="python.exe", created=100.0,
          stamp=100.0, descendants=None, listeners=None, port=1883):
    """Install one consistent world for the ownership test."""
    monkeypatch.setattr(cli, "_proc_cmdline", lambda pid: cmdline)
    monkeypatch.setattr(cli, "_proc_identity",
                        lambda pid: (name, created) if name else None)
    monkeypatch.setattr(cli, "_mqtt_pid_stamp", lambda: stamp)
    monkeypatch.setattr(cli, "_descendant_pids",
                        lambda pid: {pid} if descendants is None else set(descendants))
    monkeypatch.setattr(cli, "_listener_pids",
                        lambda p: {pid for pid in (listeners or [])})
    monkeypatch.setattr(cli, "_mqtt_conf", lambda: {"listen": "0.0.0.0", "port": port})


def test_pid_file_records_a_creation_stamp(mqtt, monkeypatch, cli):
    monkeypatch.setattr(cli, "_proc_identity", lambda pid: ("python.exe", 1712.25))
    cli._mqtt_pid_write(4242)

    raw = (mqtt["pid"]).read_text(encoding="utf-8").split()
    assert raw[0] == "4242"
    assert float(raw[1]) == pytest.approx(1712.25)
    assert cli._mqtt_pid_raw() == 4242
    assert cli._mqtt_pid_stamp() == pytest.approx(1712.25)


def test_a_file_from_an_older_build_still_reads(mqtt, cli):
    """The single-column shape must not become unreadable — that file is what
    an upgraded home has on disk until the next write."""
    (mqtt["pid"]).write_text("777\n", encoding="utf-8")
    assert cli._mqtt_pid_raw() == 777
    assert cli._mqtt_pid_stamp() is None


def test_a_readable_command_line_decides_on_its_own(mqtt, monkeypatch, cli):
    """Strongest evidence first: when it is readable and matches, that is the
    answer — no identity or topology needed."""
    conf = mqtt["conf"].as_posix()
    _fake(cli, monkeypatch, cmdline=f"x -m amqtt.scripts.broker_script -c {conf}",
          name="", descendants=set(), listeners=[])
    assert cli._broker_is_ours(11, 1883) is True


def test_a_readable_command_line_that_disagrees_is_final(mqtt, monkeypatch, cli):
    """A readable command line saying "not amqtt" must not be overruled by a
    matching stamp and topology: that combination is how a stranger's process
    would get killed."""
    _fake(cli, monkeypatch, cmdline="C:/Windows/explorer.exe",
          created=100.0, stamp=100.0, descendants=[11], listeners=[11])
    assert cli._broker_is_ours(11, 1883) is False


def test_identity_and_topology_stand_in_when_the_text_is_unreadable(mqtt, monkeypatch, cli):
    """The LocalSystem case: no command line, but the same process by
    creation time, and it holds the port."""
    _fake(cli, monkeypatch, cmdline="", name="python.exe",
          created=100.0, stamp=100.0, descendants=[11, 12], listeners=[12])
    assert cli._broker_is_ours(11, 1883) is True


def test_a_recycled_number_is_refused(mqtt, monkeypatch, cli):
    """Same pid, later creation time: the file's number now names something
    else.  Killing it is exactly the mistake decision #9 forbids."""
    _fake(cli, monkeypatch, name="python.exe", created=999.0, stamp=100.0,
          descendants=[11], listeners=[11])
    assert cli._broker_is_ours(11, 1883) is False


def test_a_non_python_listener_is_refused(mqtt, monkeypatch, cli):
    _fake(cli, monkeypatch, name="mosquitto.exe", created=100.0, stamp=100.0,
          descendants=[11], listeners=[11])
    assert cli._broker_is_ours(11, 1883) is False


def test_a_process_holding_no_listener_is_refused(mqtt, monkeypatch, cli):
    """A python of ours that is not serving the port is not the broker."""
    _fake(cli, monkeypatch, name="python.exe", created=100.0, stamp=100.0,
          descendants=[11], listeners=[99])
    assert cli._broker_is_ours(11, 1883) is False


def test_topology_will_not_re_adopt_a_number_the_stamp_rejected(mqtt, monkeypatch, cli):
    """``_mqtt_pid`` refuses a recycled number, then falls back to a scan — the
    scan must not undo that verdict by adopting the very same number."""
    (mqtt["pid"]).write_text("11 100.0\n", encoding="utf-8")
    _fake(cli, monkeypatch, name="python.exe", created=999.0, stamp=100.0,
          descendants=[11], listeners=[11])
    monkeypatch.setattr(cli, "_service_anchor_pid", lambda: 0)
    assert cli._mqtt_pid_by_topology() is None


def test_topology_adopts_a_listener_under_our_supervisor(mqtt, monkeypatch, cli):
    """The stale-file repair path: the number is gone, the supervisor is not."""
    (mqtt["pid"]).write_text("", encoding="utf-8")
    _fake(cli, monkeypatch, name="python.exe", created=555.0, stamp=None,
          descendants=[77], listeners=[77])
    monkeypatch.setattr(cli, "_service_anchor_pid", lambda: 500)
    monkeypatch.setattr(cli, "_ancestor_of", lambda pid, anchor: anchor == 500)
    assert cli._mqtt_pid_by_topology() == 77


def test_the_scan_ends_at_topology(cli):
    """Both platform branches must hand over to the topology pass rather than
    returning None — otherwise an unelevated ``start`` still fails the half-state
    check that ``_mqtt_pid() is None`` drives."""
    import inspect

    src = inspect.getsource(cli._mqtt_pid_scan)
    assert src.count("return _mqtt_pid_by_topology()") == 2, (
        "the command-line scan must fall through to topology on Windows and POSIX"
    )


def test_the_parent_link_is_available_here(cli):
    """``_ancestor_of`` is built on this: a parent that always reads 0 (the
    Windows behaviour before this fix) silently disabled the whole tier."""
    assert cli._parent_pid(os.getpid()) == os.getppid()
