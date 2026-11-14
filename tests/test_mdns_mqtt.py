"""D11 — the broker has to be reachable from *another* machine.

``_announce_address`` pins ``homeassistant.local`` to 127.0.0.1 on win32/darwin
on purpose: the Xiaomi OAuth callback is completed in a browser on this machine,
so that name only ever has to work here.  The MQTT record inherited the decision
by being built from ``HOSTNAME``, which defeats the record's only purpose — a
client on another device runs ``avahi-browse -t _mqtt._tcp``, reads
``address = 127.0.0.1``, and dials itself.

So what this file asserts is the *pair* (host name, address) that reaches
zeroconf / dns-sd, read off the objects the component builds rather than from
its source text.  Both platforms are exercised because they build those objects
in two different places (``AsyncServiceInfo`` here, ``dns-sd -P`` argv on macOS),
and the defect sat one line away in each.

The loopback pin itself is pinned too, in the opposite direction: a future
"cleanup" that removes it to make this file pass would break Xiaomi setup, which
is the reason it exists.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types

LAN = "192.168.1.5"
HTTP_PORT = 8123
MQTT_PORT = 1883


class FakeInfo:
    """``AsyncServiceInfo`` that remembers what it was built from.

    The real class is a stub MagicMock under this harness, and a MagicMock
    answers ``parsed_addresses()`` with another Mock — so the log line that
    joins those addresses would raise instead of being observable.
    """

    def __init__(self, type_, name=None, server=None, parsed_addresses=None,
                 port=None):
        self.type = type_
        self.name = name
        self.server = server
        self._addresses = list(parsed_addresses or [])
        self.port = port

    def parsed_addresses(self):
        return list(self._addresses)


class FakeHass:
    def __init__(self):
        self.http = types.SimpleNamespace(server_port=HTTP_PORT)
        self.config = types.SimpleNamespace(config_dir="/nonexistent")
        self.bus = types.SimpleNamespace(async_listen_once=lambda *a, **k: None)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def _start(mod, monkeypatch, *, platform: str, mqtt_port=MQTT_PORT):
    """Run ``async_setup`` against a fixed LAN address; return what it built."""
    async def pick_address(hass):
        return LAN, []

    async def no_stale_children():
        return None

    monkeypatch.setattr(mod, "_async_pick_address", pick_address)
    monkeypatch.setattr(mod, "_mqtt_broker_port", lambda hass: mqtt_port)
    monkeypatch.setattr(mod, "_async_reap_stale_dns_sd", no_stale_children)
    # A real wait here would put a second of sleep into every run and change
    # nothing: the children are recorded before it.
    monkeypatch.setattr(mod, "_DNS_SD_SETTLE_SEC", 0)
    monkeypatch.setattr(sys, "platform", platform)

    built: list[FakeInfo] = []

    def make_info(*args, **kwargs):
        info = FakeInfo(*args, **kwargs)
        built.append(info)
        return info

    monkeypatch.setattr(mod, "AsyncServiceInfo", make_info)
    hooks: list = []
    monkeypatch.setattr(mod, "async_when_setup_or_start",
                        lambda hass, domain, cb: hooks.append(cb))
    asyncio.run(mod.async_setup(FakeHass(), {}))
    return built, hooks


def _record(built, name):
    matches = [info for info in built if info.name == name]
    assert len(matches) == 1, (
        f"expected one record named {name}, built {[i.name for i in built]}")
    return matches[0]


def test_the_broker_record_carries_the_lan_address(component, monkeypatch):
    mod = component("homeant_mdns")
    built, _ = _start(mod, monkeypatch, platform="win32")
    record = _record(built, mod.MQTT_SERVICE_NAME)
    assert record.parsed_addresses() == [LAN], (
        "the broker is announced as "
        f"{record.parsed_addresses()} — a client on another machine reads that "
        "address, dials itself, and never reaches this broker")
    assert record.server == f"{mod.ALIAS_HOSTNAME}.local.", (
        f"the broker's SRV target is {record.server!r}: it has to be the name "
        f"that carries the LAN address ({mod.ALIAS_HOSTNAME}.local.), not the "
        "one pinned to loopback")
    assert record.port == MQTT_PORT


def test_the_web_record_keeps_its_loopback_pin(component, monkeypatch):
    """The pin is deliberate: Xiaomi's OAuth callback runs on this machine."""
    mod = component("homeant_mdns")
    built, _ = _start(mod, monkeypatch, platform="win32")
    ha = _record(built, mod.SERVICE_NAME)
    assert ha.server == f"{mod.HOSTNAME}.local."
    assert ha.parsed_addresses() == ["127.0.0.1"], (
        "`homeassistant.local` lost its loopback pin — removing it breaks the "
        "Xiaomi OAuth callback this project completes in a local browser; the "
        "MQTT record is the one that has to move, not this one")
    alias = _record(built, mod.ALIAS_SERVICE_NAME)
    assert alias.parsed_addresses() == [LAN], (
        "`HomeAnt.local` is the name other devices dial and it lost the LAN "
        f"address: {alias.parsed_addresses()}")


def test_a_linux_box_announces_the_plain_lan_address(component, monkeypatch):
    """A server has no local browser, so both names carry the LAN address."""
    mod = component("homeant_mdns")
    built, _ = _start(mod, monkeypatch, platform="linux")
    for record in built:
        assert record.parsed_addresses() == [LAN], (
            f"{record.name} on Linux announced {record.parsed_addresses()}")


def test_the_broker_line_names_the_broker_port(component, monkeypatch, caplog):
    """The log is how a user checks what was announced; it has to say :1883.

    It said :8123 for the broker too — the web port, from a variable still in
    scope — which reads like a correct announcement and is not one.
    """
    mod = component("homeant_mdns")
    built, hooks = _start(mod, monkeypatch, platform="win32")
    assert hooks, "nothing hooked into zeroconf setup — the records never register"

    registered = []

    class FakeAioZc:
        def __init__(self):
            self.zeroconf = types.SimpleNamespace(_interfaces=[LAN])

        async def async_register_service(self, info, strict=False):
            registered.append(info)

    async def instance(hass):
        return FakeAioZc()

    monkeypatch.setattr(mod, "async_get_async_instance", instance)
    with caplog.at_level(logging.INFO):
        asyncio.run(hooks[0](FakeHass(), None))

    assert registered == built, (
        "the hook registered different records than the ones built")
    lines = [msg for msg in caplog.messages if msg.startswith("mDNS: ")]
    assert len(lines) == len(built), (
        f"expected one log line per record, got {lines}")
    assert lines[-1].endswith(f":{MQTT_PORT}"), (
        f"the broker's line does not name the broker's port: {lines[-1]!r}")
    assert not any(line.endswith(f":{HTTP_PORT}") for line in lines
                   if line == lines[-1]), (
        f"the broker's line reports the web port: {lines[-1]!r}")


def test_macos_announces_the_broker_on_the_alias_host(component, monkeypatch):
    """macOS goes through ``dns-sd -P``, which is built from a second list."""
    mod = component("homeant_mdns")

    started = []

    class FakeProc:
        returncode = None

        def terminate(self):
            pass

    async def create(*argv, **kwargs):
        started.append([str(a) for a in argv])
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    _start(mod, monkeypatch, platform="darwin")

    # argv: <dns-sd> -P <instance> <type> <domain> <port> <host> <address>
    broker = [argv for argv in started if argv[3] == mod.SD_MQTT_TYPE]
    assert broker, f"the broker is never announced on macOS: {started}"
    _dns_sd, _p, instance, _type, _domain, port, host, address = broker[0]
    assert instance == mod.HOSTNAME, (
        "the broker's instance name changed — tools look it up by type, but "
        "keeping one name per box is what makes the two records the same box")
    assert port == str(MQTT_PORT), f"dns-sd got port {port}"
    assert host == f"{mod.ALIAS_HOSTNAME}.local.", (
        f"the broker's host record is {host!r}: on a desktop "
        f"{mod.HOSTNAME}.local. resolves to 127.0.0.1")
    assert address == LAN, f"dns-sd was given {address}"


def test_no_record_when_there_is_no_broker(component, monkeypatch):
    """A record naming a port nobody serves is worse than no record."""
    mod = component("homeant_mdns")
    built, _ = _start(mod, monkeypatch, platform="win32", mqtt_port=None)
    assert not [i for i in built if i.type == mod.MQTT_SERVICE_TYPE], (
        "a broker record was announced with no broker configured")


# ── a name another instance holds ───────────────────────────────────────────
# `NonUniqueNameException` is the *expected* answer on a network with a second
# HomeAnt, or while a stopped box's records are still cached (TTL 120 s).  Two
# things have to be true about it: it must not be printed as a traceback (three
# per start read like a crash), and it must not be final (a record that stays
# unannounced is invisible from the LAN until this box restarts).


class _SchedulingHass(FakeHass):
    """``FakeHass`` plus the two attributes the retry path touches."""

    def __init__(self):
        super().__init__()
        self.is_stopping = False
        self.coros = []

    def async_create_task(self, coro, name=None):
        self.coros.append(coro)
        return None


def _holding_zc(mod, state, registered):
    """A zeroconf whose registration is refused while ``state["held"]``."""

    class FakeAioZc:
        def __init__(self):
            self.zeroconf = types.SimpleNamespace(_interfaces=[LAN])

        async def async_register_service(self, info, strict=False):
            if state["held"]:
                raise mod.NonUniqueNameException()
            registered.append(info)

    return FakeAioZc()


def test_a_held_name_is_reported_in_one_line_and_retried(
        component, monkeypatch, caplog):
    mod = component("homeant_mdns")
    built, hooks = _start(mod, monkeypatch, platform="win32")
    monkeypatch.setattr(mod, "_REGISTER_RETRY_DELAYS", (0, 0))

    state = {"held": True}
    registered: list = []
    zc = _holding_zc(mod, state, registered)

    async def instance(hass):
        return zc

    monkeypatch.setattr(mod, "async_get_async_instance", instance)
    hass = _SchedulingHass()

    with caplog.at_level(logging.WARNING):
        asyncio.run(hooks[0](hass, None))

    lines = [r for r in caplog.records
             if r.getMessage().startswith("mDNS: ")]
    assert len(lines) == len(built), (
        f"expected one line per held record, got {[r.getMessage() for r in lines]}")
    assert all(r.levelno == logging.WARNING for r in lines), (
        "a name another instance holds is still logged as an error")
    assert not any(r.exc_info for r in lines), (
        "a held name still prints a traceback — three of those per start read "
        "like a crash, and on a network with a second HomeAnt this is the "
        "expected answer")
    assert registered == [], "a held name was reported as announced"
    assert hass.coros, (
        "a held name was given up on immediately: the neighbour usually frees "
        "it, and until this box restarts the record stays invisible")

    # The neighbour leaves — the scheduled retry has to finish the job.
    state["held"] = False
    with caplog.at_level(logging.INFO):
        asyncio.run(hass.coros[0])
    assert {info.name for info in registered} == {info.name for info in built}, (
        "the retry did not announce every record once the name was free")


def test_a_name_held_after_the_ladder_is_reported_loudly(
        component, monkeypatch, caplog):
    """Silence would be the bug: the user cannot see a missing mDNS record."""
    mod = component("homeant_mdns")
    built, _ = _start(mod, monkeypatch, platform="win32")
    monkeypatch.setattr(mod, "_REGISTER_RETRY_DELAYS", (0,))

    registered: list = []
    zc = _holding_zc(mod, {"held": True}, registered)

    with caplog.at_level(logging.WARNING):
        asyncio.run(
            mod._async_retry_until_announced(_SchedulingHass(), zc, built, set()))

    final = caplog.messages[-1]
    assert "still held" in final, (
        f"the ladder gave up without saying so: {final!r}")
    assert str(len(built)) in final and "homeant start" in final, (
        f"the give-up line does not say how many records or what to do: {final!r}")
    assert not [m for m in caplog.messages if "already announced" in m], (
        "every retry pass repeated the conflict warning — three records over "
        "three attempts turn three lines into nine; the first pass said it")


def test_the_retry_stops_when_home_assistant_is_stopping(
        component, monkeypatch):
    """A shutting-down instance must not re-announce what it is withdrawing."""
    mod = component("homeant_mdns")
    built, _ = _start(mod, monkeypatch, platform="win32")
    monkeypatch.setattr(mod, "_REGISTER_RETRY_DELAYS", (0,))

    registered: list = []
    state = {"held": False}
    zc = _holding_zc(mod, state, registered)
    hass = _SchedulingHass()
    hass.is_stopping = True

    asyncio.run(mod._async_retry_until_announced(hass, zc, built, set()))
    assert registered == [], (
        "records were announced while HA was stopping: they would outlive the "
        "shutdown that was supposed to withdraw them")


def test_a_plain_error_on_retry_is_not_reported_as_success(
        component, monkeypatch, caplog):
    """The verdict has to come from what got announced, not from who clashed.

    A retry pass can fail for a reason that is not a name conflict — a backend
    error after the neighbouring instance left, say.  Then ``conflicts`` is
    empty while every record is still off the wire, and a verdict read off
    ``conflicts`` prints "every record is announced" over silence.  Same class
    of bug as the record that used to print the web port: a success line that
    cannot be trusted is worse than no line at all.
    """
    mod = component("homeant_mdns")
    built, _ = _start(mod, monkeypatch, platform="win32")
    monkeypatch.setattr(mod, "_REGISTER_RETRY_DELAYS", (0,))

    class FlakyAioZc:
        def __init__(self):
            self.zeroconf = types.SimpleNamespace(_interfaces=[LAN])

        async def async_register_service(self, info, strict=False):
            raise RuntimeError("backend hiccup, not a name conflict")

    with caplog.at_level(logging.INFO):
        asyncio.run(
            mod._async_retry_until_announced(
                _SchedulingHass(), FlakyAioZc(), built, set()))

    assert not [m for m in caplog.messages if "every record is announced" in m], (
        "the ladder claimed success while no record was announced: "
        f"{caplog.messages}")
    final = caplog.messages[-1]
    assert "still held" in final and str(len(built)) in final, (
        "a record that never made it onto the wire was not counted as "
        f"missing: {final!r}")

