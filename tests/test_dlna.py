"""D25 — DLNA speaker bridge: discovery parsing, source decisions, status file.

No Home Assistant and no sockets are started.  The SSDP/M-SEARCH parsers take
recorded bytes; the component's path/URL helpers are imported under the shared
HA stubs; the state file is redirected to tmp_path.  What is asserted is the
observable contract the reader depends on:

  * only MediaRenderer replies are enrolled (a router's IGD answer is ignored);
  * the signed view refuses non-audio paths and malformed tokens;
  * an http(s) source is passed through, a directory is traversed in the
    requested order, and a loopback URL is refused (the speaker would resolve
    it to itself);
  * `start` and `status` share one idempotent discovery that lists each
    renderer live (name @ IP + entity id) and reads the default from HA.
"""

from __future__ import annotations

import types


# ── SSDP / description parsers (CLI side) ──────────────────────────────────

def test_ssdp_keeps_only_media_renderers(cli):
    replies = [
        (b"HTTP/1.1 200 OK\r\n"
         b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
         b"LOCATION: http://192.168.31.252:9999/uuid.xml\r\n\r\n"),
        (b"HTTP/1.1 200 OK\r\n"
         b"ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
         b"LOCATION: http://192.168.31.1:80/igd.xml\r\n\r\n"),
        (b"HTTP/1.1 200 OK\r\n"  # renderer via USN, not ST
         b"USN: uuid:abc::urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
         b"LOCATION: http://10.0.0.9:8080/desc.xml\r\n\r\n"),
        (b"HTTP/1.1 200 OK\r\n"  # no LOCATION at all
         b"ST: urn:schemas-upnp-org:device:MediaRenderer:1\r\n\r\n"),
    ]
    got = cli._dlna_locations(replies)
    assert got == {
        "http://192.168.31.252:9999/uuid.xml": "192.168.31.252",
        "http://10.0.0.9:8080/desc.xml": "10.0.0.9",
    }


def test_description_xml_needs_a_media_renderer(cli):
    good = (
        "<root><device><deviceType>urn:schemas-upnp-org:"
        "device:MediaRenderer:1</deviceType>"
        "<friendlyName>Living Room</friendlyName>"
        "<UDN>uuid:e2a179df</UDN></device></root>"
    )
    assert cli._dlna_device_info(good) == {
        "friendly_name": "Living Room",
        "udn": "uuid:e2a179df",
        "device_type": "urn:schemas-upnp-org:device:MediaRenderer:1",
    }
    igd = good.replace("MediaRenderer", "InternetGatewayDevice")
    assert cli._dlna_device_info(igd) is None
    assert cli._dlna_device_info("<root><device/></root>") is None


# ── component helpers (exec'd in isolation, no global stub pollution) ──────

def _load_dlna(monkeypatch):
    """Load the component's pure helpers without touching global sys.modules.

    ``component``'s loader installs stubs process-wide, which collides with the
    ``ha_stubs`` fixture other tests rely on.  This test file only needs the
    module-scope helpers, so it execs the source in a namespace whose HA imports
    are answered by local fakes and restores everything on teardown.
    """
    import sys
    import types
    from gate_paths import REPO_ROOT

    class View:
        pass

    class Web:
        class Request: pass
        class StreamResponse: pass
        class FileResponse: pass
        class HTTPNotFound(Exception): pass

    web_mod = types.ModuleType("aiohttp")
    web_mod.web = Web
    monkeypatch.setitem(sys.modules, "aiohttp", web_mod)

    class Optional:
        def __init__(self, *a, **k): pass
        def __hash__(self): return 1

    vol = types.ModuleType("voluptuous")
    vol.Schema = lambda *a, **k: None
    vol.Optional = Optional
    vol.ALLOW_EXTRA = object()
    monkeypatch.setitem(sys.modules, "voluptuous", vol)

    def stub(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, name, m)

    stub("homeassistant")
    stub("homeassistant.components")
    stub("homeassistant.components.http", HomeAssistantView=View)
    stub("homeassistant.components.http.auth", async_sign_path=lambda *a, **k: "")
    stub("homeassistant.const", EVENT_HOMEASSISTANT_START="start")
    stub("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    stub("homeassistant.helpers")
    stub("homeassistant.helpers.entity_registry")

    path = REPO_ROOT / "config" / "custom_components" / "homeant_dlna" / "__init__.py"
    # @dataclass resolves annotations via sys.modules[cls.__module__]; exec'd
    # code isn't registered there, so register a real module for the duration.
    iso = types.ModuleType("homeant_dlna_isolated")
    monkeypatch.setitem(sys.modules, "homeant_dlna_isolated", iso)
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), iso.__dict__)
    return types.SimpleNamespace(**iso.__dict__)


def test_signed_path_token_round_trips_cjk_and_spaces(monkeypatch):
    mod = _load_dlna(monkeypatch)
    for path in (r"D:\music\周杰伦 - 稻香.mp3", "/home/me/my songs/a.flac"):
        token = mod.encode_path(path)
        assert mod.decode_path(token) == path
        assert "/" not in token and "=" not in token  # one safe URL segment
        assert mod.decode_path(token + "=") is None   # non-canonical refused
    assert mod.decode_path("!!!not-base64!!!") is None


def test_audio_allowlist_blocks_non_audio_even_when_signed(monkeypatch):
    mod = _load_dlna(monkeypatch)
    for name in ("a.mp3", "A.FLAC", "song.m4a", "track.opus"):
        assert mod.is_audio(name) is True, name
    for name in ("win.ini", "a.mp4", "a.svg", "a.mp3.exe", "readme"):
        assert mod.is_audio(name) is False, name


def test_loopback_source_is_refused(monkeypatch):
    mod = _load_dlna(monkeypatch)
    assert mod.is_loopback_url("http://localhost:8123/a.mp3") is True
    assert mod.is_loopback_url("http://127.0.0.1:8123/a.mp3") is True
    assert mod.is_loopback_url("http://192.168.31.252:9999/a.mp3") is False
    assert mod.is_loopback_url("https://music.example.com/a.mp3") is False


def test_url_host_prefers_default_route_over_ha_local_ip(monkeypatch):
    """HA's api.local_ip may be a Hyper-V adapter; the speaker needs real LAN."""
    mod = _load_dlna(monkeypatch)
    monkeypatch.setitem(
        mod.local_host.__globals__, "_route_source_ip", lambda: "192.168.31.2"
    )
    assert mod.local_host("172.28.224.1") == "192.168.31.2"


def test_url_host_falls_back_when_probe_has_no_route(monkeypatch):
    mod = _load_dlna(monkeypatch)
    monkeypatch.setitem(mod.local_host.__globals__, "_route_source_ip", lambda: None)
    assert mod.local_host("172.28.224.1") == "172.28.224.1"


def test_route_probe_rejects_unusable_addresses(monkeypatch):
    mod = _load_dlna(monkeypatch)
    import socket as _socket

    class _Sock:
        def __init__(self, ip):
            self._ip = ip

        def setblocking(self, flag):
            pass

        def connect(self, addr):
            pass

        def getsockname(self):
            return (self._ip, 50000)

        def close(self):
            pass

    for ip, expected in (
        ("127.0.0.1", None),
        ("169.254.10.20", None),
        ("0.0.0.0", None),
        ("192.168.31.2", "192.168.31.2"),
    ):
        monkeypatch.setattr(_socket, "socket", lambda *a, _ip=ip, **k: _Sock(_ip))
        assert mod._route_source_ip() == expected, ip


def test_directory_expands_whole_queue_in_order_and_random(monkeypatch, tmp_path):
    mod = _load_dlna(monkeypatch)
    d = tmp_path / "music"
    d.mkdir()
    for name in ("b.mp3", "a.mp3", "c.flac", "notes.txt"):
        (d / name).write_text("x", encoding="utf-8")

    from pathlib import Path as _Path

    # A directory expands to ALL audio files, sorted; no cursor, no wrap.
    seq = mod.resolve_sources(str(d), [], "sequential")
    assert [_Path(p).name for p in seq] == ["a.mp3", "b.mp3", "c.flac"]

    rnd = mod.resolve_sources(str(d), [], "random")
    assert sorted(_Path(p).name for p in rnd) == ["a.mp3", "b.mp3", "c.flac"]

    # A single file / http(s) is a one-element queue; unsupported scheme is empty.
    assert mod.resolve_sources(str(d / "a.mp3"), []) == [str(d / "a.mp3")]
    assert mod.resolve_sources("https://example.com/s.mp3", []) == [
        "https://example.com/s.mp3"]
    assert mod.resolve_sources("ftp://example.com/s.mp3", []) == []
    assert mod.resolve_sources("", [str(d)], "sequential") == seq


def test_multi_source_argument_becomes_one_ordered_queue(monkeypatch, tmp_path):
    """A '|'/newline separated argument is one queue, every part expanded."""
    from pathlib import Path as _Path

    mod = _load_dlna(monkeypatch)
    d = tmp_path / "music"
    d.mkdir()
    for name in ("b.mp3", "a.mp3"):
        (d / name).write_text("x", encoding="utf-8")
    u1, u2 = "https://example.com/1.mp3", "https://example.com/2.mp3"

    # Both separators, in the order given, duplicates kept (?empty lines drop).
    assert mod.resolve_sources(f"{u1}|{u2}", []) == [u1, u2]
    assert mod.resolve_sources(f"{u1}\r\n{u2}\n\n{u2}", []) == [u1, u2, u2]

    # A directory in the middle keeps its own name-sorted expansion.
    mixed = mod.resolve_sources(f"{u1}|{d}", [])
    assert mixed[0] == u1
    assert [_Path(p).name for p in mixed[1:]] == ["a.mp3", "b.mp3"]

    # An unresolvable part is dropped, the rest still queues; all-bad is empty.
    assert mod.resolve_sources(f"ftp://example.com/x.mp3|{u1}", []) == [u1]
    assert mod.resolve_sources("ftp://example.com/x.mp3|", []) == []

    # A single source keeps the pre-multi-source behaviour byte-for-byte.
    assert mod.resolve_sources(u1, []) == [u1]
    assert mod.resolve_sources("", [str(d)]) == [
        str(d / "a.mp3"), str(d / "b.mp3")]


def test_component_keeps_cursor_in_memory_only(monkeypatch):
    """D26: no Store / .state on disk — a restart starts from the first track."""
    from gate_paths import REPO_ROOT

    mod = _load_dlna(monkeypatch)  # would fail if the module still imported Store
    src = (REPO_ROOT / "config" / "custom_components" / "homeant_dlna"
           / "__init__.py").read_text(encoding="utf-8")
    assert "helpers.storage" not in src
    assert "STORE_KEY" not in src and "async_save" not in src
    assert not hasattr(mod, "Store")


# ── D27: sequential queue state machine (pure; no HA) ──────────────────────

def test_single_track_is_a_queue_and_stops_on_wrap(monkeypatch):
    """One track = a one-element queue; its wrap stops (renderer never idles)."""
    from gate_paths import REPO_ROOT

    mod = _load_dlna(monkeypatch)
    # Pure state machine: a single URL's wrap is the LAST track -> stop.
    q = mod.QueueState(urls=["only.mp3"], speaker="media_player.x")
    q.mark_pushed(0.0)
    assert q.is_last
    assert q.observe({"state": "playing", "position": 50, "duration": 100}, 2.0) == "none"
    assert q.observe({"state": "playing", "position": 1, "duration": 100}, 4.0) == "stop"

    # play() must feed the expanded URL list to the shared queue helper, not
    # push directly (a bare push would loop forever on this renderer).
    src = (REPO_ROOT / "config" / "custom_components" / "homeant_dlna"
           / "__init__.py").read_text(encoding="utf-8")
    play_body = src.split("async def play(call)", 1)[1].split(
        'hass.services.async_register(DOMAIN, "play"', 1)[0]
    assert "_begin_queue(urls, speaker, queue_ttl)" in play_body
    assert "play_media" not in play_body
    # play is the only enqueue path now; no second caller builds a queue.
    assert src.count("await _begin_queue(") == 1
    assert "async def enqueue(" not in src


def test_queue_wrap_advances_and_last_track_stops(monkeypatch):
    """Position wrap -> next; on the last track -> stop. No stall heuristic."""
    mod = _load_dlna(monkeypatch)
    q = mod.QueueState(urls=["a.mp3", "b.mp3", "c.mp3"], speaker="media_player.x")
    q.mark_pushed(0.0)

    # position climbs, no action
    assert q.observe({"state": "playing", "position": 10, "duration": 100}, 2.0) == "none"
    assert q.last_pos == 10
    # big drop = wrap = this track ended -> next (not last)
    assert q.observe({"state": "playing", "position": 1, "duration": 100}, 4.0) == "next"
    assert q.advance(4.0) is True and q.idx == 1

    # sparse/stalled position must NOT end the track early
    assert q.observe({"state": "playing", "position": 50, "duration": 100}, 6.0) == "none"
    assert q.observe({"state": "playing", "position": 50, "duration": 100}, 14.0) == "none"

    # advance to last, then a wrap there stops
    assert q.observe({"state": "playing", "position": 1, "duration": 100}, 16.0) == "next"
    assert q.advance(16.0) is True and q.idx == 2 and q.is_last
    q.mark_pushed(16.0)
    assert q.observe({"state": "playing", "position": 50, "duration": 100}, 18.0) == "none"
    assert q.observe({"state": "playing", "position": 2, "duration": 100}, 20.0) == "stop"
    assert q.advance(20.0) is False


def test_queue_bad_url_skips_after_grace(monkeypatch):
    """duration==0 while state=playing: skip after FAIL_GRACE, not before."""
    mod = _load_dlna(monkeypatch)
    q = mod.QueueState(urls=["bad.mp3", "good.mp3"], speaker="media_player.x")
    q.mark_pushed(0.0)
    # within grace -> wait
    assert q.observe({"state": "playing", "position": None, "duration": 0}, 3.0) == "none"
    # past grace with still-zero duration -> next
    assert q.observe({"state": "playing", "position": None, "duration": 0}, 7.0) == "next"


def test_queue_idle_resume_and_expire(monkeypatch):
    """External stop -> idle+position; TTL expiry drops; resume seek/head rules."""
    mod = _load_dlna(monkeypatch)
    q = mod.QueueState(urls=["a.mp3", "b.mp3"], speaker="media_player.x", ttl=600.0)
    q.mark_pushed(0.0)
    q.observe({"state": "playing", "position": 42, "duration": 100}, 2.0)

    # HA / speaker stops -> idle, pause point captured, resume seek target set
    assert q.observe({"state": "idle", "position": 42, "duration": 100}, 3.0) == "idle"
    assert q.phase == "idle" and q.paused_pos == 42 and q.idle_since == 3.0
    assert q.resume_target() == 42.0

    # within ttl -> no expire; spontaneous play becomes new baseline
    assert q.observe({"state": "idle", "position": 42, "duration": 100}, 100.0) == "none"

    # fresh push with no position (or tiny) -> replay from head, no seek
    q.mark_pushed(101.0)
    assert q.resume_target() is None
    q.paused_pos = 1.0
    assert q.resume_target() is None   # below RESUME_SEEK_MIN

    # idle past ttl -> expire
    q.phase = "idle"
    q.idle_since = 0.0
    assert q.observe({"state": "idle", "position": 42, "duration": 100}, 601.0) == "expire"


def test_queue_scripts_and_services_registered(monkeypatch):
    """D27: play/resume/cancel services + the two MCP scripts; no enqueue."""
    from gate_paths import REPO_ROOT

    mod = _load_dlna(monkeypatch)
    src = (REPO_ROOT / "config" / "custom_components" / "homeant_dlna"
           / "__init__.py").read_text(encoding="utf-8")
    for svc in ("play", "resume", "cancel"):
        assert f'"{svc}"' in src
    assert '"enqueue"' not in src
    assert not hasattr(mod, "ENQUEUE_SCRIPT_ALIAS")
    assert mod.RESUME_SCRIPT_ALIAS == "HomeAntDLNAResume"
    assert mod.CANCEL_SCRIPT_ALIAS == "HomeAntDLNACancel"
    scripts = (REPO_ROOT / "config" / "scripts.yaml").read_text(encoding="utf-8")
    assert "alias: HomeAntDLNAPlay" in scripts
    assert "alias: HomeAntDLNAResume" in scripts
    assert "alias: HomeAntDLNACancel" in scripts
    assert "alias: HomeAntDLNAEnqueue" not in scripts
    assert "multiple: true" not in scripts  # no string[] field in the MCP schema
    assert "queue_ttl" in src           # configurable TTL (10 min default)


def test_secs_left_anchors_position_to_capture_time(monkeypatch):
    """position alone is ~10s stale; pairing it with updated_at gives an ETA."""
    from datetime import datetime, timezone

    mod = _load_dlna(monkeypatch)
    now = 1_700_000_000.0
    at = datetime.fromtimestamp(now - 2, timezone.utc)   # captured 2s ago
    # 302 - 100 - 2 = 200
    assert mod.secs_left({"position": 100, "duration": 302, "updated_at": at}, now) == 200.0
    # the same reading aged 12s instead of 2 => 10s less left
    at12 = datetime.fromtimestamp(now - 12, timezone.utc)
    assert mod.secs_left({"position": 100, "duration": 302, "updated_at": at12}, now) == 190.0
    # unknown duration / missing position / missing stamp -> no estimate
    assert mod.secs_left({"position": 1, "duration": 0, "updated_at": at}, now) is None
    assert mod.secs_left({"position": None, "duration": 100, "updated_at": at}, now) is None
    assert mod.secs_left({"position": 1, "duration": 100, "updated_at": None}, now) is None
    # a naive stamp is read as UTC rather than rejected
    naive = datetime.fromtimestamp(now - 5, timezone.utc).replace(tzinfo=None)
    assert mod.secs_left({"position": 10, "duration": 100, "updated_at": naive}, now) == 85.0


def test_queue_ends_on_wall_clock_before_the_renderer_wraps(monkeypatch):
    """Remaining <= END_MARGIN ends the track before it loops audibly again."""
    from gate_paths import REPO_ROOT

    mod = _load_dlna(monkeypatch)
    near = mod.END_MARGIN - 0.1      # inside the stop window
    far = mod.END_MARGIN + 1.0       # still playing
    # single track: near the end -> stop
    q = mod.QueueState(urls=["only.mp3"], speaker="media_player.x")
    q.mark_pushed(0.0)
    assert q.observe({"state": "playing", "position": 300, "duration": 302},
                     2.0, left=far) == "none"
    assert q.observe({"state": "playing", "position": 301, "duration": 302},
                     4.0, left=near) == "stop"

    # more tracks: near the end -> next, not stop
    q2 = mod.QueueState(urls=["a.mp3", "b.mp3"], speaker="media_player.x")
    q2.mark_pushed(0.0)
    assert q2.observe({"state": "playing", "position": 301, "duration": 302},
                      2.0, left=near) == "next"

    # paused: the clock keeps running, so a stale reading must not read as end
    q3 = mod.QueueState(urls=["a.mp3"], speaker="media_player.x")
    q3.mark_pushed(0.0)
    q3.observe({"state": "playing", "position": 100, "duration": 302}, 2.0)
    assert q3.observe({"state": "paused", "position": 100, "duration": 302},
                      30.0, left=-20.0) == "idle"

    # the loop only forces a refresh inside the tail window
    src = (REPO_ROOT / "config" / "custom_components" / "homeant_dlna"
           / "__init__.py").read_text(encoding="utf-8")
    assert "await _refresh(q.speaker)" in src
    assert "TAIL_POLL_INTERVAL if tail else POLL_INTERVAL" in src
    assert '"homeassistant", "update_entity"' in src


# ── shared discovery: live listing, idempotent enrolment ───────────────────

def test_scan_sends_once_and_keeps_all_replies(cli, monkeypatch):
    """One M-SEARCH per default scan; pinning the source fixed the old misses."""
    import socket

    sends: list[tuple] = []
    opts: list[tuple] = []

    class _Sock:
        def setsockopt(self, *a):
            opts.append(a)

        def settimeout(self, *a):
            pass

        def connect(self, *a):
            pass

        def getsockname(self):
            return ("192.168.31.2", 12345)

        def sendto(self, *a):
            sends.append(a)

        def recvfrom(self, _n):
            raise socket.timeout

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *a, **k: _Sock())
    assert cli._dlna_scan(window=0.01, progress=False) == {}
    assert len(sends) == 1
    assert (socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
            socket.inet_aton("192.168.31.2")) in opts


def test_discover_lists_each_renderer_and_reads_default(cli, monkeypatch, capsys):
    """No state file: names/IPs are live, the default comes from HA helpers."""
    import contextlib

    desc = (
        b"<root><device><deviceType>urn:schemas-upnp-org:device:MediaRenderer:1"
        b"</deviceType><friendlyName>Living Room</friendlyName>"
        b"<UDN>uuid:abc</UDN></device></root>"
    )

    class _Resp:
        def read(self):
            return desc

    class _Opener:
        def open(self, *a, **k):
            return contextlib.nullcontext(_Resp())

    seen: list[str] = []
    monkeypatch.setattr(cli, "_dlna_scan", lambda **kw: {
        "http://192.168.31.252:9999/desc.xml": "192.168.31.252"})
    monkeypatch.setattr(cli, "_no_proxy_opener", lambda: _Opener())
    monkeypatch.setattr(cli, "_dlna_existing_udns", lambda *a: set())
    monkeypatch.setattr(cli, "_dlna_enrol_one",
                        lambda *a: seen.append(a[2]) or "ok")
    monkeypatch.setattr(cli, "_dlna_entity_id",
                        lambda *a: "media_player.living_room")
    monkeypatch.setattr(cli, "_dlna_default_speaker",
                        lambda *a: "media_player.living_room")

    cli._dlna_discover("token", progress=False)
    out = capsys.readouterr().out
    assert "dlna: Living Room @ 192.168.31.252 (media_player.living_room)" in out
    assert "dlna: default speaker media_player.living_room" in out
    assert seen == ["http://192.168.31.252:9999/desc.xml"]


def test_discover_without_enrol_only_reports(cli, monkeypatch, capsys):
    """`status` reports renderers and the default; it never creates an entry.

    Entry creation is `start`'s job (D25).  A read-only command that quietly
    enrolled a speaker would turn `status` into a write, so this case pins all
    three write paths shut: the UDN read, the config flow, the default speaker.
    """
    import contextlib

    desc = (
        b"<root><device><deviceType>urn:schemas-upnp-org:device:MediaRenderer:1"
        b"</deviceType><friendlyName>Living Room</friendlyName>"
        b"<UDN>uuid:abc</UDN></device></root>"
    )

    class _Resp:
        def read(self):
            return desc

    class _Opener:
        def open(self, *a, **k):
            return contextlib.nullcontext(_Resp())

    reads: list[str] = []
    enrolled: list[str] = []
    defaults: list[str] = []
    monkeypatch.setattr(cli, "_dlna_scan", lambda **kw: {
        "http://192.168.31.252:9999/desc.xml": "192.168.31.252"})
    monkeypatch.setattr(cli, "_no_proxy_opener", lambda: _Opener())
    monkeypatch.setattr(cli, "_dlna_existing_udns",
                        lambda *a: reads.append("read") or set())
    monkeypatch.setattr(cli, "_dlna_enrol_one",
                        lambda *a: enrolled.append(a[2]) or "ok")
    monkeypatch.setattr(cli, "_dlna_entity_id",
                        lambda *a: "media_player.living_room")
    monkeypatch.setattr(cli, "_dlna_default_speaker", lambda *a: "")
    monkeypatch.setattr(cli, "_dlna_set_default",
                        lambda *a: defaults.append(a[2]) or True)

    cli._dlna_discover("token", progress=False, enrol=False)
    out = capsys.readouterr().out
    assert "dlna: Living Room @ 192.168.31.252 (media_player.living_room)" in out
    assert "dlna: none selected" in out
    assert reads == []
    assert enrolled == []
    assert defaults == []


def test_scan_prints_one_done_for_both_rounds(cli, monkeypatch, capsys):
    """`scanning ... done` belongs to the whole scan, not to each M-SEARCH."""
    import socket

    monkeypatch.setattr(cli, "_route_source_ip", lambda: None)

    class _Sock:
        def setsockopt(self, *a):
            pass

        def settimeout(self, *a):
            pass

        def sendto(self, *a):
            pass

        def recvfrom(self, _n):
            raise socket.timeout

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *a, **k: _Sock())
    cli._dlna_scan(window=0.01, attempts=2, progress=True)
    out = capsys.readouterr().out
    assert out.count("dlna: scanning") == 1
    assert out.count("done") == 1


def test_scan_ticks_when_no_renderer_answers(cli, monkeypatch, capsys):
    """Dots follow the wall clock, not replies: silent LAN still shows progress."""
    import socket

    monkeypatch.setattr(cli, "_route_source_ip", lambda: None)

    class _Sock:
        def setsockopt(self, *a):
            pass

        def settimeout(self, *a):
            pass

        def sendto(self, *a):
            pass

        def recvfrom(self, _n):
            raise socket.timeout

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *a, **k: _Sock())
    cli._dlna_scan(window=1.05, attempts=1, progress=True)
    out = capsys.readouterr().out
    assert "dlna: scanning." in out          # one dot at the 1s mark
    assert out.count("done") == 1


def test_discover_sets_the_only_renderer_as_default(cli, monkeypatch, capsys):
    """One renderer and no saved choice: use HA's own select script."""
    import contextlib

    desc = (
        b"<root><device><deviceType>urn:schemas-upnp-org:device:MediaRenderer:1"
        b"</deviceType><friendlyName>Living Room</friendlyName>"
        b"<UDN>uuid:abc</UDN></device></root>"
    )

    class _Resp:
        def read(self):
            return desc

    class _Opener:
        def open(self, *a, **k):
            return contextlib.nullcontext(_Resp())

    calls: list[tuple] = []
    monkeypatch.setattr(cli, "_dlna_scan", lambda **kw: {
        "http://192.168.31.252:9999/desc.xml": "192.168.31.252"})
    monkeypatch.setattr(cli, "_no_proxy_opener", lambda: _Opener())
    monkeypatch.setattr(cli, "_dlna_existing_udns", lambda *a: set())
    monkeypatch.setattr(cli, "_dlna_enrol_one", lambda *a: "ok")
    monkeypatch.setattr(cli, "_dlna_entity_id",
                        lambda *a: "media_player.living_room")
    monkeypatch.setattr(cli, "_dlna_default_speaker", lambda *a: "")
    monkeypatch.setattr(cli, "_dlna_set_default",
                        lambda *a: calls.append(a) or True)

    cli._dlna_discover("token", progress=False)
    out = capsys.readouterr().out
    assert "only one renderer — set default to media_player.living_room" in out
    assert calls and calls[0][2] == "media_player.living_room"
