# HomeAnt

English | [简体中文](./README.zh-CN.md)

Home Control by Agent — Control smart-home devices by AI agents

HomeAnt packages Home Assistant with the `xiaomi_home` integration and
exposes it over MCP, so agents can drive lights, AC, curtains, robot vacuums,
and more — and play local music or network audio on a DLNA speaker.

```
AI Agent ──MCP Client──▶ Home Assistant (HomeAnt.local:8123)
                                          │
                                          ├── xiaomi_home (MIoT integration)
                                          └── Xiaomi cloud / local network
```

## Install

One-shot online bootstrap — it unpacks the source package into `~/homeant`,
syncs the venv, then hands over to `homeant start`:

```powershell
# Windows (PowerShell)
irm https://dl.homeant.cn/core/install.ps1 | iex
```

```sh
# Linux / macOS
curl -fsSL https://dl.homeant.cn/core/install.sh | sh
```

From then on everything goes through the `homeant` command in `~/homeant`:

| Platform | Command |
| --- | --- |
| Windows | `.\homeant start` |
| Linux / macOS | `./homeant start` |

`homeant start` auto-installs `uv` (project-local at `.tools/uv/`, never
global), runs `uv sync`, deploys config files and starts Home Assistant. The
post-start work runs as two gates — Xiaomi OAuth, then the MCP config — and an
unsatisfied gate stops the run before the next one. Re-running `homeant start`
re-checks everything and resumes where you left off. Each run ends with
`SUCCESS` / `PENDING` / `FAIL`; the MCP config block opens the final `SUCCESS`
block, so nothing scrolls past what you copy. If the source tree is incomplete
(missing `core/` or `vendor/`), the online installer auto-downloads the full
package and overwrites — no manual submodule init, no extra steps.

## Daily Commands

All control commands go through `homeant`:

| Command | What it does |
| --- | --- |
| `start` | Bring the install up to date, then start Home Assistant (this also turns autostart on) |
| `stop` | Stop Home Assistant now; autostart is untouched, so it comes back at the next boot |
| `disable` | Turn autostart off; the running home keeps running (`start` turns it back on) |
| `status` | Health check + paste-ready MCP config block |
| `logs` | Stream Home Assistant logs in real time |
| `uninstall` | Stop, remove the service registration (and autostart), delete `~/.homeant` |

## MQTT broker

A built-in `amqtt` broker serves LAN devices (ESP32, anything that
speaks MQTT) and HA's own MQTT integration. It is a child of `start`, not a
system service or an add-on: it comes up with `homeant start` and stops with
`homeant stop`. Defaults are `0.0.0.0:1883`.

## Music playback (DLNA)

Play music on a DLNA speaker (Xiaoai and friends). Starting HomeAnt discovers
the speakers on your network and adds them for you — no manual step in the UI.
If there is only one, it also becomes the default speaker.

A speaker that comes online later is picked up by the next start
(`homeant start`); `homeant status` only reports, it adds nothing. To use a
different default, just tell the agent which speaker to play on.

### Audio sources

| Source | What you give | Notes |
| --- | --- | --- |
| Local file | the audio file's full path | Plays a file from that machine — no extra streaming server needed |
| Local folder | the folder's full path | Plays the whole folder, in order or shuffled |
| Network URL | a direct `http(s)://` audio link — or just give the page and let the agent resolve one | The speaker fetches it itself, so it must be reachable |

Common audio formats are supported (mp3, m4a, flac, wav, ogg, ...).

The audio has to live on the machine running HomeAnt: the path is a path on
that machine, not on the one you are talking to the agent from.

### Examples

You just say a sentence to the agent; it fills in the paths and parameters:

```text
play some music with HomeAnt: ~\Music\
```

```text
play the music in <path to a local folder> on shuffle all evening with HomeAnt
```

```text
play https://example.com/audio.mp3 with HomeAnt
```

```text
play the songs on this page with HomeAnt: https://example.com/album/123
```

To use another speaker, say "play it on the XX speaker"; to see what is
available, ask "which speakers can you play to right now".

### Queue

Each request starts at the beginning of its own queue and replaces the previous
one; nothing is appended. Tracks advance on their own, and playback stops after
the last one.

| What you say | What happens |
| --- | --- |
| Play a file / folder / URL | Starts from the top (one track is a one-track queue) |
| Resume · keep playing | Continues where it stopped; after too long, just play it again |
| Play it on the XX speaker | Switches the default speaker, effective at once |
| Stop · that's enough | Stops at once and drops the queue; "resume" no longer works, play it again |

### Worth knowing

- Commands go out but nothing plays? Check that the firewall lets the speaker
  reach Home Assistant — the speaker fetches the audio itself.
- A web page cannot be handed to the speaker as-is: it only fetches an audio
  stream, and a page gives it nothing to play. Just pass the page address on —
  the agent resolves the audio link inside it first, and tells you when it
  cannot instead of queueing the page.
- If no speaker is found, check in the Xiaoai app that its DLNA switch is on.

## Connect an AI agent over MCP

`status` doubles as the connection helper: it verifies the MCP token and prints
a config block with the token already inlined, ready to paste into any MCP
client (WorkBuddy, Claude Code, Cursor, TRAE, Codex, opencode, ...).

```text
[homeant] OK    HA_ROOT: ~/.homeant
[homeant] OK    hass: running
[homeant] OK    mcp: token valid
[homeant] mcp config — paste into your MCP client:
{
   "mcpServers": {
      "homeant": {
         "url": "http://HomeAnt.local:8123/api/mcp",
         "headers": {
            "Authorization": "Bearer <token>"
         }
      }
   }
}
```

The block is plain JSON — no comments, no trailing commas — so it passes the
validation a client's config editor runs on paste. A client that already has an
`mcpServers` object only needs the `"homeant"` entry merged into it.

`status` only reports success when every check passes. The endpoint speaks MCP over
Streamable HTTP; the token is a long-lived token of the `admin` owner, cached in
`~/.homeant/.mcp-token`, so agents never trigger a browser login. If a client
reports `401`, run `homeant start` (it re-issues the cached token) and
re-read `status`.

## First Run: Xiaomi OAuth

On the first `start`, the script:

1. Deploys preconfigured HA config into `~/.homeant/`
2. Boots Home Assistant and waits for the core to report running
3. Runs two gates; a failing gate stops the run before the next one:

| Stage | Windows / Linux / macOS |
| --- | --- |
| 1 OAuth | starts the `xiaomi_home` flow and opens the browser; a headless Linux box has no `DISPLAY`/`WAYLAND_DISPLAY`, and a Windows service has no Session-0 desktop, so both print the full OAuth URL instead — and stop |
| 2 MCP | prints the ready-to-paste MCP config, always last |

Nothing ever waits for the browser. Stage 1 leaves the flow pending and returns
at once; re-running `homeant start` picks that same flow back up (its
webhook is what the browser comes back to), answers the remaining steps —
`homes_select`, `advanced_options` — and only then reaches stage 2. The id of
the pending flow is remembered in `~/.homeant/.xiaomi-flow`, because HA has no
way to list in-progress flows over REST.

Every run ends with one of three printed states:

| State | Meaning |
| --- | --- |
| `SUCCESS` | all stages passed; the MCP config is above and ready to paste |
| `PENDING` | install is fine, one human step is outstanding — re-run `homeant start` |
| `FAIL` | something is actually broken; re-running alone will not fix it |

## Architecture

| Layer | Choice |
| --- | --- |
| Runtime | Home Assistant (editable submodule) |
| MCP Server | HA built-in `mcp_server` (Streamable HTTP) |
| MQTT broker | Built-in `0.0.0.0:1883`, child of `start` |
| Xiaomi integration | `XiaoMi/ha_xiaomi_home` (official, vendored) |
| Package manager | `uv` (project-local, `.tools/uv/`) |
| PyPI mirror | Aliyun (`mirrors.aliyun.com`) — no pypi.org |
| OS patches | specific fixes in HA Core (see `core/`) |
| HA data dir | `~/.homeant/` (separate from default HA dir) |
| OAuth callback | `homeassistant.local:8123` (Xiaomi whitelist) |

## Security

- MCP token = long-lived token of the `admin` owner, cached at `~/.homeant/.mcp-token` (mode `0600` where the OS supports it). It carries full admin rights on this instance, so treat it like a password — revoke via HA → Profile → Security
- One account only: the same owner signs in for the Xiaomi OAuth flow and owns the MCP token
- Admin password: `homeant.cfg` in the install directory is the source of truth, not the HA
  UI. Every `start` reconciles the HA admin password to the `PASSWORD=` value in that file,
  and an empty value makes `start` generate a 16-character password and write it back. Edit
  the file, then run `homeant start` — changing it only in the HA UI gets reverted by the
  next start
- MCP listens on `HomeAnt.local:8123` (the OAuth callback stays on
  `homeassistant.local` — that name is hardcoded into xiaomi_home's redirect
  whitelist and cannot change). `homeant_mdns` advertises both names to the
  LAN. Resolution is the
  client's job: Windows/macOS resolve `.local` natively, a Linux client needs
  `libnss-mdns`, and the server itself never needs to resolve either name. A
  client that can do none of it must be pointed at the server's LAN IP

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `core/homeassistant/ missing` | Re-run the online installer — it auto-repairs |
| Ends with `PENDING:` | Not an error — the install is fine; that line says what is left. Do it, then re-run `homeant start` |
| Ends with `FAIL:` | Something is really broken — the reason is on the line(s) above it |
| `hass not found` | Run `homeant start` — it installs the dependencies first |
| Port 8123 in use | Stop the existing process, or edit `config/configuration.yaml` |
| MCP endpoint 401/403 | Expected — the MCP server is up, token required |

## Legal

- License: Apache 2.0
- Terms: [TERMS.md](./TERMS.md)

GitHub: https://github.com/git-hub-cloud/HomeAnt-Core
