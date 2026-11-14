"""Windows: ProactorEventLoop must answer add_reader/add_writer for hass's MQTT.

The defect this pins down is the one README-level diagnosis misses: the MQTT
entry connects, logs a successful CONNACK and then reads nothing, because every
packet after it is expected from ``loop.add_reader`` (mqtt/client.py:683) and
Proactor does not implement it.  Measured 11-15 on this machine: a retained
discovery topic produced 0 of 482 entities, ``Connected to MQTT`` never appeared
and each attempt left two ``Error doing job`` tracebacks.

Two layers are tested separately, on purpose:

* the registry itself (``SoftSelector``) with a stand-in loop that only records
  what was scheduled — this is the part that must be right on any platform, and
  it is the part a future refactor is most likely to break silently;
* the real thing on Windows: patch the actual ``ProactorEventLoop`` class, then
  prove a byte written to a socketpair reaches a callback through
  ``add_reader`` — the same path paho's ``loop_read`` takes.

Plus the wiring rule: ``_hass_child_env()`` is None on POSIX (nothing to fix
there) and, on Windows, puts ``config/runtime`` on PYTHONPATH together with the
marker sitecustomize pops before installing.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import socket
import sys
import types
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gate_paths import REPO_ROOT  # noqa: E402

SHIM_PATH = REPO_ROOT / "config" / "runtime" / "sitecustomize.py"


@pytest.fixture()
def shim():
    """Load config/runtime/sitecustomize.py by path, uninstalled."""
    assert SHIM_PATH.is_file(), f"missing shim: {SHIM_PATH}"
    name = "homeant_runtime_sitecustomize"
    spec = importlib.util.spec_from_file_location(name, SHIM_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Deterministic import: the marker must not be set, or the module would
    # patch the real asyncio class as a side effect of loading.
    os.environ.pop("HOMEANT_SOFT_SELECTOR", None)
    spec.loader.exec_module(mod)
    return mod


class FakeLoop:
    """Records scheduled callbacks instead of running them."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.closed = False

    def call_soon_threadsafe(self, callback, *args):
        with self.lock:
            self.calls.append((callback, args))

    def is_closed(self):
        return self.closed

    def scheduled(self):
        with self.lock:
            return list(self.calls)


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _make_patched(shim):
    """A fresh patched stand-in class plus one instance of it."""
    loop_class = type("StandInLoop", (FakeLoop,), {})
    assert shim.install(loop_class) is True
    return loop_class()


def test_install_teaches_the_class_all_four_primitives(shim):
    loop_class = type("StandInLoop", (FakeLoop,), {})
    assert shim.install(loop_class) is True
    # Idempotent: a second call is a no-op, not a second thread per loop.
    assert shim.install(loop_class) is False
    for name in ("add_reader", "remove_reader", "add_writer", "remove_writer"):
        assert getattr(loop_class, name) is not None


def test_reader_fires_when_the_socket_becomes_readable(shim):
    loop = _make_patched(shim)
    a, b = socket.socketpair()
    seen = []
    try:
        loop.add_reader(a, lambda payload: seen.append(payload), "tag")
        b.sendall(b"ping")
        assert _wait_for(lambda: loop.scheduled()), "reader callback never dispatched"
        callback, args = loop.scheduled()[0]
        assert args == ("tag",)
        callback(*args)  # what call_soon_threadsafe would have run
        assert seen == ["tag"]
        assert loop._homeant_soft_selector.remove_reader(a) is True
        assert loop._homeant_soft_selector.remove_reader(a) is False
    finally:
        loop._homeant_soft_selector.close()
        a.close()
        b.close()


def test_writer_fires_on_a_writable_socket(shim):
    loop = _make_patched(shim)
    a, b = socket.socketpair()
    try:
        loop.add_writer(a, lambda: None)
        assert _wait_for(lambda: loop.scheduled()), "writer callback never dispatched"
    finally:
        loop._homeant_soft_selector.close()
        a.close()
        b.close()


def test_removed_reader_stops_being_dispatched(shim):
    loop = _make_patched(shim)
    a, b = socket.socketpair()
    try:
        loop.add_reader(a, lambda: None)
        b.sendall(b"one")
        assert _wait_for(lambda: loop.scheduled()), "first read never dispatched"
        loop.remove_reader(a)
        before = len(loop.scheduled())
        time.sleep(0.2)  # let an in-flight select() cycle finish
        b.sendall(b"two")
        time.sleep(0.4)
        assert len(loop.scheduled()) == before, "a removed reader was still dispatched"
    finally:
        loop._homeant_soft_selector.close()
        a.close()
        b.close()


def test_a_closed_socket_is_dropped_instead_of_spinning(shim):
    loop = _make_patched(shim)
    a, b = socket.socketpair()
    try:
        loop.add_reader(a, lambda: None)
        b.sendall(b"x")
        assert _wait_for(lambda: loop.scheduled()), "reader never dispatched once"
        a.close()  # the real shape of a paho reconnect
        sel = loop._homeant_soft_selector
        assert _wait_for(lambda: not sel._readers, timeout=3.0), (
            "closed fd stayed registered — select() would raise 10038 in a loop"
        )
    finally:
        loop._homeant_soft_selector.close()
        b.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Proactor is Windows-only")
def test_real_proactor_loop_delivers_a_read(shim):
    """The actual class HA runs on: a byte through a socketpair reaches the loop."""
    assert shim.install() is True or getattr(
        asyncio.ProactorEventLoop, "_homeant_soft_selector_patched", False
    ), "ProactorEventLoop was not patched"
    loop = asyncio.new_event_loop()
    assert type(loop).__name__ == "ProactorEventLoop", type(loop).__name__
    a, b = socket.socketpair()
    got = []
    try:
        fut = loop.create_future()
        # add_reader is level-triggered and stays registered — that is asyncio's
        # own semantics, not a shim quirk.  The select thread can therefore queue
        # a second dispatch before the first one has run, so a blocking recv here
        # deadlocks the loop (this case hung in ~2 of 5 runs).
        # Real callers are shaped for it: HA sets the paho socket non-blocking and
        # loop_read() treats EAGAIN as "nothing left to read".
        a.setblocking(False)

        def on_readable():
            try:
                chunk = a.recv(16)
            except BlockingIOError:
                return  # a spurious level-triggered dispatch, nothing to read
            if not chunk or fut.done():
                return
            got.append(chunk)
            fut.set_result(None)

        loop.add_reader(a, on_readable)
        b.setblocking(False)
        b.sendall(b"hello")
        loop.run_until_complete(asyncio.wait_for(fut, 5))
        assert got == [b"hello"]
        loop.remove_reader(a)
    finally:
        loop.close()
        a.close()
        b.close()


def test_posix_install_is_a_no_op(shim, monkeypatch):
    monkeypatch.setattr(shim.sys, "platform", "linux")
    assert shim.install() is False


def test_hass_child_env_wires_the_shim_on_windows(cli, monkeypatch):
    monkeypatch.setattr(cli, "IS_WIN", True)
    monkeypatch.setenv("PYTHONPATH", r"C:\existing")
    env = cli._hass_child_env()
    assert env is not None
    assert env["HOMEANT_SOFT_SELECTOR"] == "1"
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(cli._RUNTIME_DIR)
    assert parts[1] == r"C:\existing"
    assert Path(parts[0]).name == "runtime"


def test_ca_bundle_is_certifi_when_it_is_there(cli, monkeypatch, tmp_path):
    """``_ca_bundle`` answers with certifi's file, or with nothing at all.

    Measured 11-18 on Windows 11: python 3.14.10 / OpenSSL 3.5.9 refused
    ``account.xiaomi.com``'s chain from the Windows store ("Basic Constraints of
    CA cert not marked critical") while the browser and curl were fine, so
    xiaomi_home called the host unreachable and setup stopped.  The bundle is
    what fixes that, and a missing certifi must stay harmless.
    """
    bundle = tmp_path / "cacert.pem"
    bundle.write_text("", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "certifi",
                        types.SimpleNamespace(where=lambda: str(bundle)))
    assert cli._ca_bundle() == str(bundle)

    monkeypatch.setitem(sys.modules, "certifi", None)
    assert cli._ca_bundle() is None


def test_hass_child_env_trusts_that_bundle_on_windows(cli, monkeypatch):
    monkeypatch.setattr(cli, "IS_WIN", True)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(cli, "_ca_bundle", lambda: r"C:\bundle\cacert.pem")
    assert cli._hass_child_env()["SSL_CERT_FILE"] == r"C:\bundle\cacert.pem"


def test_an_explicit_ca_bundle_is_never_overwritten(cli, monkeypatch):
    """A user who pointed SSL_CERT_FILE somewhere meant it."""
    monkeypatch.setattr(cli, "IS_WIN", True)
    monkeypatch.setenv("SSL_CERT_FILE", r"C:\corp\roots.pem")
    monkeypatch.setattr(cli, "_ca_bundle", lambda: r"C:\bundle\cacert.pem")
    assert cli._hass_child_env()["SSL_CERT_FILE"] == r"C:\corp\roots.pem"


def test_no_bundle_means_no_variable(cli, monkeypatch):
    monkeypatch.setattr(cli, "IS_WIN", True)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr(cli, "_ca_bundle", lambda: None)
    assert "SSL_CERT_FILE" not in cli._hass_child_env()


def test_hass_child_env_leaves_posix_alone(cli, monkeypatch):
    monkeypatch.setattr(cli, "IS_WIN", False)
    assert cli._hass_child_env() is None


def test_the_marker_is_consumed_so_grandchildren_start_clean(shim):
    """sitecustomize pops the marker before installing."""
    source = SHIM_PATH.read_text(encoding="utf-8")
    assert 'os.environ.pop(MARKER, None) == "1"' in source


def test_the_shim_is_never_deployed_into_ha_root(cli, tmp_path, monkeypatch):
    """HA_ROOT gets configuration; the shim stays in the program tree.

    hass reads the shim through ``PYTHONPATH=<tree>/config/runtime``, so a copy
    under HA_ROOT would never be read — and it would rot there: ``_merge_file``
    never rewrites a ``.py``, so the first deploy would freeze whatever shipped
    first.  Measured 11-15: ``~/.homeant/runtime/sitecustomize.py`` had already
    diverged from the tree it is supposed to mirror, which reads like "the fix
    was not deployed" on the next debugging session.
    """
    monkeypatch.setattr(cli, "HA_ROOT", tmp_path / "ha")
    monkeypatch.setattr(cli, "log", lambda *a, **k: None)

    cli._deploy_config_files()

    assert (tmp_path / "ha").is_dir(), "the deploy copied nothing at all"
    assert not (tmp_path / "ha" / "runtime").exists(), (
        "config/runtime was deployed into HA_ROOT: an unread copy that "
        "_merge_file will never update"
    )
    # The injection point has to be the tree, not the data root.
    assert cli._RUNTIME_DIR == REPO_ROOT / "config" / "runtime"


def test_the_shim_ships_in_the_release_archive():
    """A fix nobody receives is not a fix: the packager must carry the shim.

    This is the failure mode R8 hid behind for hours — the shim is optional at
    runtime (sitecustomize is only imported when homeant puts it on PYTHONPATH),
    so a forgotten INCLUDE_PATHS entry produces a release that reads no MQTT
    while every log line still says success.
    """
    from gate_cli import load_by_path

    pack = load_by_path("homeant_pack", REPO_ROOT / "scripts" / "dist-pack.py")
    arcs = {arc for _src, arc in pack.collect_files(REPO_ROOT)}
    assert "config/runtime/sitecustomize.py" in arcs, (
        "the read shim is missing from the release file list"
    )


def test_a_command_hint_is_spelled_for_this_platform(cli, monkeypatch):
    """`homeant` lives in the install root, not on PATH — so `homeant start` in
    a message is not typeable anywhere.  The prefix is the whole difference
    between a hint a person can paste and one they have to translate.
    """
    monkeypatch.setattr(cli, "IS_WIN", True)
    assert cli._cmd("start") == ".\\homeant start"
    assert cli._installer_cmd() == ".\\install.ps1 --upgrade"

    monkeypatch.setattr(cli, "IS_WIN", False)
    assert cli._cmd("start") == "./homeant start"
    assert cli._installer_cmd() == "./install.sh --upgrade"


def test_a_command_hint_says_where_to_stand(cli, monkeypatch, capsys, tmp_path):
    """`./homeant` only names the program *inside* the install root.

    Both installs drop a directory called `homeant` into the home directory and
    put the wrapper of the same name inside it, so from `~` that path names the
    directory and the paste fails before doing anything.  The hint therefore
    chains the `cd` in front of the command — but only when the reader is not
    already there, and only once per block: `then:` runs in the directory the
    line above left them in.

    One line, not two: a two-line block is pasted as one by half the readers
    (the command running in the old directory) and run separately by the rest,
    which is the very failure the pair exists to prevent.
    """
    root = tmp_path.resolve()
    monkeypatch.setattr(cli, "PROJECT_ROOT", root)
    monkeypatch.chdir(root)

    cli.log_run(cli._cmd("start"))
    at_root = capsys.readouterr().out
    assert "cd " not in at_root, at_root
    assert f"run: {cli._cmd('start')}" in at_root, at_root

    sub = root / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)

    cli.log_run(cli._cmd("stop"))
    cli.log_run(cli._cmd("start"), "then")
    away = capsys.readouterr().out
    assert away.count(f"cd {cli._home_tilde(root)}") == 1, away
    assert (f"run: cd {cli._home_tilde(root)}{cli._shell_and()}"
            f"{cli._cmd('stop')}") in away, away
    # The second command of the block does not repeat it: the shell is there.
    assert f"then: {cli._cmd('start')}" in away, away
