"""`_finish_setup` runs OAuth then MCP, and never prints the config early.

The hosts-entry stage that used to gate both of them was WSL-only and went away
with the rest of the WSL support (11-17): every supported platform resolves
``homeassistant.local`` through HA's own zeroconf announcement, so there is no
entry to check and no human step to wait on.

What still matters is the order.  A configured install goes straight to the MCP
config; an unfinished OAuth run prints the URL and stops — the config names a
host the client is about to dial, so handing it over before setup finishes is
the one thing this sequence exists to prevent.

`status` shares that gate: it used to print the same config while OAuth was
still waiting, so the two commands told opposite stories about the same home.

Nothing here starts Home Assistant, opens a socket, or touches ``~/.homeant``:
every stage is monkeypatched, and the one file path ``_finish_setup`` unlinks is
redirected into ``tmp_path``.
"""

from __future__ import annotations

import json
import types


def test_a_configured_install_prints_the_mcp_config(cli, monkeypatch, tmp_path, capsys):
    """Nothing is left to ask, so the config is the whole output."""
    monkeypatch.setattr(cli, "_xiaomi_already_configured", lambda: True)
    monkeypatch.setattr(cli, "XIAOMI_FLOW_FILE", tmp_path / "xiaomi-flow.json")

    rc = cli._finish_setup('{"mcpServers": {}}')

    out = capsys.readouterr().out
    assert rc == 0
    assert "SUCCESS: mcp ready" in out
    assert "mcpServers" in out


def test_an_unfinished_oauth_run_prints_the_url_and_stops(cli, monkeypatch, tmp_path, capsys):
    """The URL is the retry instruction; the config must not appear before it."""
    monkeypatch.setattr(cli, "_xiaomi_already_configured", lambda: False)
    monkeypatch.setattr(cli, "XIAOMI_FLOW_FILE", tmp_path / "xiaomi-flow.json")
    monkeypatch.setattr(
        cli, "_xiaomi_advance",
        lambda: ("waiting", "https://example.invalid/auth", None))

    rc = cli._finish_setup('{"mcpServers": {}}')

    out = capsys.readouterr().out
    assert rc == 0
    assert "PENDING: open this URL in your browser" in out
    assert "https://example.invalid/auth" in out
    # The command is its own line, spelled for this platform — never the tail of
    # the sentence above it.
    assert cli._cmd("start") in out, out
    assert "re-run:" not in out, out
    assert "mcpServers" not in out


def test_a_refused_flow_says_why(cli, monkeypatch, tmp_path, capsys):
    """The FAIL line carries the flow's own verdict.

    It used to say "see the lines above" while nothing above named the reason:
    the walker silently re-answered a form it had just been refused, so the one
    sentence the reader needed was never printed anywhere.
    """
    monkeypatch.setattr(cli, "_xiaomi_already_configured", lambda: False)
    monkeypatch.setattr(cli, "XIAOMI_FLOW_FILE", tmp_path / "xiaomi-flow.json")
    monkeypatch.setattr(
        cli, "_xiaomi_advance",
        lambda: ("failed", None,
                 "the Xiaomi integration refused the answer homeant sent it"))

    rc = cli._finish_setup('{"mcpServers": {}}')

    out = capsys.readouterr().out
    assert rc == 1
    assert "did not complete" in out
    assert "refused the answer" in out, out
    assert "see the lines above" not in out
    # Verdict on one line, command on the next, and the command is typeable on
    # this platform instead of a bare name that is not on PATH.
    assert cli._cmd("start") in out, out
    assert "re-run:" not in out, out


def _status_home(cli, monkeypatch, tmp_path):
    """Point `cmd_status` at an empty home with hass up and no service."""
    home = tmp_path / "homeant-home"
    home.mkdir()
    monkeypatch.setattr(cli, "HA_ROOT", home)
    monkeypatch.setattr(cli, "cf", lambda key, default=None: default)
    monkeypatch.setattr(cli, "_seed_present", lambda: True)
    monkeypatch.setattr(cli, "_ownership_problem", lambda: None)
    monkeypatch.setattr(cli, "port_in_use", lambda port: True)
    monkeypatch.setattr(cli, "_ha_instance", lambda: 42)
    monkeypatch.setattr(cli, "_service_kind", lambda: "none")
    monkeypatch.setattr(cli, "_autostart_state", lambda: None)
    monkeypatch.setattr(cli, "_mqtt_gate", lambda: None)
    monkeypatch.setattr(cli, "_mqtt_pid", lambda: 7)
    monkeypatch.setattr(cli, "_dns_hint", lambda: "unresolved")


def test_status_says_nothing_about_mcp_while_setup_is_pending(
        cli, monkeypatch, tmp_path, capsys):
    """Same gate as `_finish_setup`, so `start` and `status` agree.

    `status` used to print "mcp: token valid" and the whole config while
    `start` was still holding at PENDING — one command handing out the last step
    of a sequence the other had not begun.  Nothing MCP-shaped is printed here,
    and the token is not even looked up.
    """
    _status_home(cli, monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_xiaomi_already_configured", lambda: False)
    flow = tmp_path / "xiaomi-flow.json"
    flow.write_text("{}", encoding="utf-8")   # the marker: a flow is open
    monkeypatch.setattr(cli, "XIAOMI_FLOW_FILE", flow)
    monkeypatch.setattr(cli, "_xiaomi_flow_state", lambda: "authorizing")
    asked = []
    monkeypatch.setattr(cli, "_mcp_token", lambda create=True: asked.append(create))
    monkeypatch.setattr(cli, "_mcp_diag", lambda: (True, "token valid"))

    rc = cli.cmd_status(types.SimpleNamespace())

    out = capsys.readouterr().out
    assert rc == 0
    assert "WARN  oauth: xiaomi_home waiting for authorization" in out, out
    assert cli._cmd("start") in out, out
    assert "mcp" not in out, out
    assert asked == [], asked


def test_status_prints_the_config_once_setup_is_done(
        cli, monkeypatch, tmp_path, capsys):
    """The other half of the gate: nothing is withheld from a finished home."""
    _status_home(cli, monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_xiaomi_already_configured", lambda: True)
    monkeypatch.setattr(cli, "_mcp_diag", lambda: (True, "token valid"))
    monkeypatch.setattr(cli, "_mcp_token", lambda create=True: "llat")
    monkeypatch.setattr(cli, "_mcp_probe", lambda tok, timeout=5: None)

    rc = cli.cmd_status(types.SimpleNamespace())

    out = capsys.readouterr().out
    assert rc == 0
    assert "oauth: xiaomi_home configured" in out, out
    assert "mcp: token valid" in out, out
    assert "mcp config — paste into your MCP client:" in out, out


def test_the_config_block_is_plain_json(cli):
    """Pasted into a client's config editor, the block has to pass validation.

    It used to carry ``// === start copy ===`` markers naming the lines worth
    taking.  An editor that parses what was pasted rejected the whole block
    with "invalid JSON" — a verdict with no next step in it, since the reader
    cannot tell a comment from a typo.  The markers are gone: the sentence
    printed above the block says what to copy, and the block itself is only
    JSON.
    """
    text = cli._mcp_config_text("llat")

    doc = json.loads(text)          # a comment or a trailing comma dies here
    entry = doc["mcpServers"]["homeant"]
    assert entry["url"] == cli.MCP_CLIENT_URL
    assert entry["headers"]["Authorization"] == "Bearer llat"
    assert "===" not in text, text
    # The token is the one field a client cannot fill in for itself, so a
    # placeholder left in the print would be a dead end.
    assert "<token>" not in text, text


def test_dns_line_ticks_while_resolving(cli, monkeypatch, capsys):
    """A slow mDNS lookup shows a dot per second; a silent console read as hung."""
    import time

    monkeypatch.setattr(cli, "_dns_hint",
                        lambda: time.sleep(1.1) or "192.168.31.2")
    cli._dns_line()
    out = capsys.readouterr().out
    assert "dns: HomeAnt.local." in out, out          # a dot before the answer
    assert out.rstrip().endswith("-> 192.168.31.2"), out


def test_dns_line_instant_is_one_plain_line(cli, monkeypatch, capsys):
    """An instant lookup prints exactly what the old single log() line did."""
    monkeypatch.setattr(cli, "_dns_hint", lambda: "unresolved")
    cli._dns_line()
    out = capsys.readouterr().out
    assert out == "[homeant] dns: HomeAnt.local -> unresolved\n", out
