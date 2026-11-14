"""Loopback discipline for ``scripts/homeant.py`` — no HA, no real sockets.

The distinction under test is *connection target* vs *protocol field*:

* A connection target only has to reach our own hass.  Writing the IPv4
  literal removes the resolver and the address-list walk from the path; on
  Windows a refused second attempt costs ~2s, so a two-address name doubles
  every closed-port probe.  These six sites must use ``127.0.0.1``.
* ``client_id`` / ``redirect_uri`` / ``Host`` are protocol fields: their text
  is part of HA's IndieAuth identity and of the HTTP request, not merely a
  place to send bytes.  They are covered by their own cases below.
* ``localhost`` must stay in the loopback *recognition set*: a user is allowed
  to write that name in ``homeant.cfg`` and the MQTT gate must still see it as
  loopback.

Every probe replaces the transport at the module boundary.  Nothing here
starts Home Assistant, opens a socket, or touches ``~/.homeant`` — the same
rule conftest.py states for the rest of the gate.
"""

from __future__ import annotations

import urllib.parse
import urllib.request

import pytest

LOOPBACK_IP = "127.0.0.1"


def _hostname(url: str) -> str | None:
    return urllib.parse.urlsplit(url).hostname


class _FakeResponse:
    """Context-manager stand-in for what ``urlopen`` returns."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return b"{}"


def _text_frame(payload: bytes) -> bytes:
    assert len(payload) < 126, "the helper only builds short frames"
    return bytes([0x81, len(payload)]) + payload


class _HandshakeSock:
    """A websocket peer that answers the upgrade and the two auth frames."""

    def __init__(self) -> None:
        self.sent = b""
        self._chunks = [
            b"HTTP/1.1 101 Switching Protocols\r\n"
            b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n",
            _text_frame(b'{"type": "auth_required"}'),
            _text_frame(b'{"type": "auth_ok"}'),
        ]

    def settimeout(self, _t) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        pass


@pytest.fixture
def ws_probe(monkeypatch, cli):
    """Run ``_ws_open`` against a scripted peer; hand back what it sent."""
    seen: dict = {}
    sock = _HandshakeSock()

    def fake_connect(address, *args, **kwargs):
        seen["address"] = address
        return sock

    monkeypatch.setattr(cli.socket, "create_connection", fake_connect)
    with cli._ws_open("probe-token"):
        pass
    seen["request"] = sock.sent
    return seen


# ── step 1: six connection targets ──────────────────────────────────────────

def test_hass_serving_uses_ipv4_literal(monkeypatch, cli):
    seen = []

    def fake_urlopen(url, *args, **kwargs):
        seen.append(url)
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert cli._hass_serving() is True
    assert seen and _hostname(seen[0]) == LOOPBACK_IP, seen


def test_mcp_probe_base_is_ipv4_literal(cli):
    assert _hostname(cli.MCP_PROBE_BASE) == LOOPBACK_IP, cli.MCP_PROBE_BASE


def test_xiaomi_flow_get_uses_ipv4_literal(monkeypatch, cli):
    seen = []

    def fake_get(url, **kwargs):
        seen.append(url)
        return 200, {"flow_id": "probe"}

    monkeypatch.setattr(cli, "_http_get", fake_get)
    cli._xiaomi_flow_get("tok", "probe")
    assert seen and _hostname(seen[0]) == LOOPBACK_IP, seen


def test_xiaomi_start_flow_uses_ipv4_literal(monkeypatch, cli):
    seen = []

    def fake_post(url, **kwargs):
        seen.append(url)
        return 200, {"flow_id": "probe"}

    monkeypatch.setattr(cli, "_http_post", fake_post)
    cli._xiaomi_start_flow("tok")
    assert seen and _hostname(seen[0]) == LOOPBACK_IP, seen


def test_xiaomi_walk_posts_to_ipv4_literal(monkeypatch, cli):
    seen = []

    def fake_post(url, **kwargs):
        seen.append(url)
        return 200, {"type": "abort", "abort_reason": "probe"}

    monkeypatch.setattr(cli, "_http_post", fake_post)
    monkeypatch.setattr(cli, "_xiaomi_answer", lambda _flow: {})
    cli._xiaomi_walk("tok", {"flow_id": "probe", "type": "form",
                             "step_id": "probe"})
    assert seen and _hostname(seen[0]) == LOOPBACK_IP, seen


def test_xiaomi_walk_reports_the_flow_refusal(monkeypatch, cli):
    """A form that comes back with a non-empty ``errors`` is the flow refusing.

    Re-posting it cannot succeed, so the reason has to leave the loop — but not
    as a reason code, not as advice about the reader's network, and not as a
    pointer at a log: every refusal seen so far was a fault on our side, all of
    them fixed here, so the sentence carries the only thing left to do.
    """
    def fake_post(url, **kwargs):
        return 200, {"type": "form", "step_id": "network_detect_config",
                     "errors": {"base": "unreachable_mqtt_broker"}}

    monkeypatch.setattr(cli, "_http_post", fake_post)
    monkeypatch.setattr(cli, "_xiaomi_answer", lambda _flow: {})
    state, url, why = cli._xiaomi_walk(
        "tok", {"flow_id": "probe", "type": "form",
                "step_id": "network_detect_config"})
    assert (state, url) == ("failed", None)
    assert why == cli._XIAOMI_REFUSED, why
    # The command is not part of the sentence: the caller prints it on a line of
    # its own, spelled for this platform (see log_run / _cmd).
    assert "homeant start" not in why, why
    assert "network_detect_config" not in why, why
    assert "home-assistant.log" not in why, why
    # Nothing here reads as "your machine cannot reach the internet": that was
    # the claim, and the machine was online the whole time.
    for verdict in ("unreachable", "cannot reach", "no working route"):
        assert verdict not in why, why


def test_xiaomi_walk_ignores_the_empty_error_every_form_carries(
        monkeypatch, cli):
    """`errors={'base': ''}` is how a form is *shown*, not how one is refused.

    xiaomi_home hands the next form back with that key emptied, and its
    `__show_*` helpers use the same key for a real refusal — so only a non-empty
    value is a verdict.  Treating the empty one as one aborted setup on the very
    first answer, and the FAIL line then blamed the network for it.
    """
    posts = []

    def fake_post(url, **kwargs):
        posts.append(url)
        if len(posts) == 1:
            return 200, {"type": "form", "step_id": "auth_config",
                         "errors": {"base": ""}}
        return 200, {"type": "create_entry"}

    monkeypatch.setattr(cli, "_http_post", fake_post)
    monkeypatch.setattr(cli, "_xiaomi_answer", lambda _flow: {})
    state, url, why = cli._xiaomi_walk(
        "tok", {"flow_id": "probe", "type": "form", "step_id": "eula"})
    assert (state, url, why) == ("done", None, None)
    assert len(posts) == 2, posts


def test_ws_open_connects_to_ipv4_literal(ws_probe):
    host, port = ws_probe["address"][:2]
    assert host == LOOPBACK_IP, ws_probe["address"]
    assert port == ws_probe["address"][1]


# ── never "fix": the loopback recognition set ───────────────────────────────

def test_mqtt_gate_still_recognises_the_name_localhost(cli):
    assert cli._mqtt_is_loopback("localhost") is True
    assert cli._mqtt_is_loopback("127.0.0.1") is True
    assert cli._mqtt_is_loopback("::1") is True
    assert cli._mqtt_is_loopback("192.168.1.10") is False


# ── step 2: dial the IP, keep the IndieAuth identity ────────────────────────

def test_login_posts_to_ipv4_but_oauth_fields_stay_localhost(monkeypatch, cli):
    """``client_id`` is persisted in the refresh token: never re-key it."""
    replies = [
        (200, {"flow_id": "probe"}),
        (200, {"type": "create_entry", "result": "probe-code"}),
        (200, {"access_token": "probe-token"}),
    ]
    seen: list[tuple[str, dict]] = []

    def fake_post(url, **kwargs):
        seen.append((url, kwargs.get("json_body") or kwargs.get("form_body")))
        return replies[len(seen) - 1]

    monkeypatch.setattr(cli, "_http_post", fake_post)
    assert cli._login("admin", "probe-pw") == "probe-token"
    assert len(seen) == 3
    assert all(_hostname(url) == LOOPBACK_IP for url, _ in seen), seen

    origin = f"http://localhost:{cli.PORT}"
    create, _answer, token = (body for _, body in seen)
    for body in (create, token):
        assert body["client_id"] == origin + "/", body
        assert body["redirect_uri"] == origin + "/?auth_callback=1", body
    # indieauth.py: client_id and redirect_uri must share a netloc.
    assert _hostname(create["client_id"]) == _hostname(create["redirect_uri"])
    assert _hostname(create["client_id"]) == "localhost"


# ── step 3: the WS Host header is deliberately NOT the dial address ─────────

def test_ws_host_header_stays_localhost(ws_probe):
    """Regression guard for the evaluated decision, not an overlooked string.

    ``get_url()`` in HA's helpers/network.py lists ``localhost`` as a known
    hostname unconditionally but only accepts a loopback IP while ``allow_ip``
    is on.  Real HA answers the upgrade regardless of this header, so there is
    nothing to win by replacing it — see the comment in ``_ws_open``.
    """
    head = ws_probe["request"].split(b"\r\n\r\n", 1)[0].decode()
    host = next(
        (line.split(":", 1)[1].strip() for line in head.split("\r\n")
         if line.lower().startswith("host:")),
        None,
    )
    assert host == f"localhost:{ws_probe['address'][1]}", head
    assert ws_probe["address"][0] == LOOPBACK_IP
