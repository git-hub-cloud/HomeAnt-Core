# HomeAnt

Home Control by Agent — Control smart-home devices by AI agents

HomeAnt packages Home Assistant with the `xiaomi_home` integration and
exposes it over MCP, so agents such as TRAE, Cursor, Claude Code, Codex, and
Windsurf can drive lights, AC, curtains, robot vacuums, and more.

```
AI Agent ──MCP Client──▶ Home Assistant (HomeAnt.local:8123)
                                          │
                                          ├── xiaomi_home (MIoT integration)
                                          └── Xiaomi cloud / local BLE mesh
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

| Platform      | Command               |
|---------------|-----------------------|
| Windows       | `.\homeant start` |
| Linux / macOS | `./homeant start` |

`homeant start` auto-installs `uv` (project-local at `.tools/uv/`, never
global), runs `uv sync`, deploys config files and starts Home Assistant. The
post-start work runs as three gated stages — hosts (WSL only), Xiaomi OAuth,
MCP config — and an unsatisfied stage stops the run without doing the next one;
re-running `homeant start` re-checks everything and resumes where you left
off. Each run ends with `SUCCESS` / `PENDING` / `FAIL`; the MCP config block
opens the final `SUCCESS` block, so nothing scrolls past what you copy. If the
source tree is incomplete (missing `core/` or `vendor/`), the online installer
auto-downloads the full package and overwrites — no manual submodule init, no
extra steps.

## Daily Commands

All control commands go through `homeant`:

| Command   | What it does                                                    |
|-----------|-----------------------------------------------------------------|
| `start`   | Bring the install up to date, then start Home Assistant          |
| `stop`    | Stop Home Assistant                                             |
| `status`  | Health check + paste-ready MCP config block                     |
| `logs`    | Stream Home Assistant logs in real time                         |

```powershell
# Windows
.\homeant status
.\homeant logs
```

```sh
# Linux / macOS
./homeant status
./homeant logs
```

## Connect an AI agent over MCP

`status` doubles as the connection helper: it verifies the MCP token and prints
a config block with the token already inlined, ready to paste into any MCP
client (WorkBuddy, Claude Code, Cursor, TRAE, Codex, opencode, ...).

```text
[homeant] OK    HA_ROOT: C:\Users\<you>\.homeant
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

`status` only reports success when every check passes. The endpoint speaks MCP over
Streamable HTTP; the token is a long-lived token of the `admin` owner, cached in
`~/.homeant/.mcp-token`, so agents never trigger a browser login. If a client
reports `401`, run `homeant start` (it re-issues the cached token) and
re-read `status`.

## First Run: Xiaomi OAuth

On the first `start`, the script:

1. Deploys preconfigured HA config into `~/.homeant/`
2. Boots Home Assistant and waits for the core to report running
3. Runs three gated stages — each one must pass before the next runs:

| Stage | WSL | Windows / Linux / macOS |
|---|---|---|
| 1 hosts | reads the Windows hosts file, requires `127.0.0.1 homeassistant.local HomeAnt.local` (one line, both names); missing → prints the PowerShell line and stops | skipped — the server itself never needs to resolve these names. Clients resolve them via mDNS (Windows/macOS natively; Linux needs `libnss-mdns`), or fall back to the server's LAN IP |
| 2 OAuth | starts the `xiaomi_home` flow, prints the full OAuth URL for the Windows browser, stops | starts the flow, opens the browser, stops — a headless Linux box has no `DISPLAY`/`WAYLAND_DISPLAY`, so it prints the URL instead of launching a console browser |
| 3 MCP | prints the ready-to-paste MCP config, always last | same |

Nothing ever waits for the browser. Stage 2 leaves the flow pending and returns
at once; re-running `homeant start` picks that same flow back up (its
webhook is what the browser comes back to), answers the remaining steps —
`homes_select`, `advanced_options` — and only then reaches stage 3. The id of
the pending flow is remembered in `~/.homeant/.xiaomi-flow`, because HA has no
way to list in-progress flows over REST.

Every run ends with one of three printed states:

| State | Meaning |
|---|---|
| `SUCCESS` | all stages passed; the MCP config is above and ready to paste |
| `PENDING` | install is fine, one human step is outstanding — re-run `homeant start` |
| `FAIL` | something is actually broken; re-running alone will not fix it |

## Architecture

| Layer              | Choice                                          |
|--------------------|------------------------------------------------|
| Runtime            | Home Assistant (editable submodule)   |
| MCP Server         | HA built-in `mcp_server` (Streamable HTTP)     |
| Xiaomi integration | `XiaoMi/ha_xiaomi_home` (official, vendored)   |
| Package manager    | `uv` (project-local, `.tools/uv/`)              |
| PyPI mirror        | Aliyun (`mirrors.aliyun.com`) — no pypi.org     |
| OS patches         | specific fixes in HA Core (see `core/`)|
| HA data dir        | `~/.homeant/` (separate from default HA dir)    |
| OAuth callback     | `homeassistant.local:8123` (Xiaomi whitelist)   |

## Security

- MCP token = long-lived token of the `admin` owner, cached at `~/.homeant/.mcp-token` (mode `0600` where the OS supports it). It carries full admin rights on this instance, so treat it like a password — revoke via HA → Profile → Security
- One account only: the same owner signs in for the Xiaomi OAuth flow and owns the MCP token
- MCP listens on `HomeAnt.local:8123` (the OAuth callback stays on
  `homeassistant.local` — that name is hardcoded into xiaomi_home's redirect
  whitelist and cannot change). `homeant_mdns` advertises both names to the
  LAN, and on WSL the Windows hosts entry carries both. Resolution is the
  client's job: Windows/macOS resolve `.local` natively, a Linux client needs
  `libnss-mdns`, and the server itself never needs to resolve either name. A
  client that can do none of it must be pointed at the server's LAN IP

## Troubleshooting

| Symptom | Fix |
|---------|-----|
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
