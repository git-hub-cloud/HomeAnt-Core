"""P3-2 / D15 action 3: preflight must cover 1883 without refusing our broker.

Adding a second entry to the two port lists is the easy half.  The hard half is
ownership, and it is platform-specific:

* Linux procfs names no owner at all, so install.sh asks the service manager
  (``_service_active``).  A running homeant unit is the only listener it may
  take down; since B-full the broker is in that unit's process tree.
* Windows has a pid from netstat, so install.ps1 asks the command line.  But
  the broker's command line is
  ``python.exe -m amqtt.scripts.broker_script -c <HA_ROOT>/mqtt.yaml`` —
  measured on this box, 11-15 — which has neither ``--config`` nor a
  ``hass.exe``.  The hass predicate alone would call our own broker a stranger
  and hard-stop every upgrade on a running install.

So both halves are exercised as they ship: the function bodies are lifted out
of the real installers and run by the real shell / PowerShell, with the owner
lookup faked.  A test that only grepped the port number would pass while the
upgrade still failed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from gate_paths import REPO_ROOT

INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
INSTALL_PS1 = REPO_ROOT / "scripts" / "install.ps1"

SH_TEXT = INSTALL_SH.read_text(encoding="utf-8")
PS1_TEXT = INSTALL_PS1.read_text(encoding="utf-8")


def sh_function(text: str, header: str) -> str:
    """One shell function's own text: from ``header`` to a ``}`` at column 0."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(header)]
    assert len(starts) == 1, f"{header!r}: found {len(starts)} definitions, expected 1"
    for end in range(starts[0] + 1, len(lines)):
        if lines[end] == "}":
            return "\n".join(lines[starts[0]:end + 1])
    raise AssertionError(f"{header!r}: no closing brace at column 0")


def ps1_function(text: str, header: str) -> str:
    """One PowerShell function's own text (same closing-brace convention)."""
    return sh_function(text, header)


def sh_port_entries() -> dict[str, str]:
    m = re.search(r'(?m)^PREFLIGHT_PORTS="([^"]*)"', SH_TEXT)
    assert m, "install.sh: PREFLIGHT_PORTS not found"
    entries = [ln.split("|", 1) for ln in m.group(1).splitlines() if ln.strip()]
    return {port.strip(): label.strip() for port, label in entries}


def ps1_port_entries() -> dict[str, str]:
    found = re.findall(
        r'@\{\s*Port\s*=\s*(\d+)\s*;\s*Label\s*=\s*"([^"]*)"\s*\}', PS1_TEXT
    )
    assert found, "install.ps1: no `@{ Port = ...; Label = ... }` entries"
    return {port: label for port, label in found}


# ── static contract ────────────────────────────────────────────────────────


def test_both_installers_check_8123_and_1883() -> None:
    sh, ps1 = sh_port_entries(), ps1_port_entries()
    assert set(sh) == {"8123", "1883"}, f"install.sh ports: {sorted(sh)}"
    assert set(ps1) == {"8123", "1883"}, f"install.ps1 ports: {sorted(ps1)}"
    assert "MQTT" in sh["1883"] and "MQTT" in ps1["1883"], (
        "the 1883 label must name the broker, not just another port"
    )


def test_comments_no_longer_describe_a_future_broker() -> None:
    # These two phrases used to say "the MQTT broker this project will grow".
    # They became false the moment 1883 was added; leaving them would invite
    # somebody to add the entry a second time.
    for name, text in (("install.sh", SH_TEXT), ("install.ps1", PS1_TEXT)):
        assert "will grow" not in text, f"{name}: stale 'will grow' wording"
        assert "a later release\n" not in text, f"{name}: stale future-tense wording"


# ── runtime: install.sh's preflight ────────────────────────────────────────

SH_SCRIPT = """\
set -u
# preflight() also ends with a writable-directory probe on $_here.  That check
# is not what this file tests, so give it a directory that is always writable;
# leaving it unset would fail the probe under `set -u` before the port loop's
# verdict is even reached.
_here="."
PREFLIGHT_PORTS={ports}
info() {{ echo "INFO $*"; }}
err() {{ echo "ERR $*"; }}
_fatal() {{ echo "FATAL $*"; exit 1; }}
_pf_listeners() {{
    echo "probe:$1" >&2
    if [ "${{BUSY_ALL:-0}}" = 1 ] || [ "${{BUSY_PORT:-}}" = "$1" ]; then
        printf 'unknown'
    fi
    return 0
}}
_service_active() {{ [ "${{SERVICE_ACTIVE:-0}}" = 1 ]; }}
{body}
preflight
echo "preflight-ok"
"""


def run_sh_preflight(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    ports = re.search(r'(?m)^PREFLIGHT_PORTS="([^"]*)"', SH_TEXT).group(1)
    body = sh_function(SH_TEXT, "preflight() {")
    script = tmp_path / "preflight.sh"
    script.write_text(
        SH_SCRIPT.format(ports=f'"{ports}"', body=body),
        encoding="utf-8", newline="\n",
    )
    proc = subprocess.run(["sh", str(script)], capture_output=True, text=True,
                          timeout=120, env={**os.environ, **env})
    return proc


def test_sh_preflight_probes_both_ports_and_spares_our_service(tmp_path: Path) -> None:
    proc = run_sh_preflight(tmp_path, BUSY_ALL="1", SERVICE_ACTIVE="1")
    assert proc.returncode == 0, f"preflight refused our own service: {proc.stdout} {proc.stderr}"
    assert "probe:8123" in proc.stderr and "probe:1883" in proc.stderr, (
        f"not both ports were probed: {proc.stderr}"
    )
    assert proc.stdout.count("will be stopped before uv sync") == 2, (
        f"both occupied ports should be reported as ours: {proc.stdout}"
    )
    assert "preflight-ok" in proc.stdout


def test_sh_preflight_refuses_a_foreign_1883(tmp_path: Path) -> None:
    proc = run_sh_preflight(tmp_path, BUSY_PORT="1883", SERVICE_ACTIVE="0")
    assert proc.returncode != 0, "a stranger on 1883 must be a hard stop"
    assert "probe:1883" in proc.stderr, f"1883 was never probed: {proc.stderr}"
    assert "1883" in proc.stdout and "HomeAnt MQTT" in proc.stdout, (
        f"the refusal must name the port it stopped on: {proc.stdout}"
    )
    assert "FATAL" in proc.stdout


def test_sh_preflight_is_quiet_when_both_ports_are_free(tmp_path: Path) -> None:
    proc = run_sh_preflight(tmp_path, BUSY_ALL="0", SERVICE_ACTIVE="0")
    assert proc.returncode == 0, f"free ports must pass: {proc.stdout} {proc.stderr}"
    assert "FATAL" not in proc.stdout
    assert "preflight-ok" in proc.stdout


# ── runtime: install.ps1's owner predicates ────────────────────────────────

PS_SCRIPT = """\
$ErrorActionPreference = 'Stop'
$script:FakeName = "{name}"
$script:FakeCmd = "{cmd}"
function Get-CimInstance {{
    [CmdletBinding()]
    param([Parameter(Position=0)]$ClassName, $Filter)
    return [pscustomobject]@{{ Name = $script:FakeName; CommandLine = $script:FakeCmd; ProcessId = 43244 }}
}}
{body}
if ((_is_our_hass_pid 43244) -or (_is_our_broker_pid 43244)) {{ exit 0 }} else {{ exit 1 }}
"""


def run_ps1_owner(tmp_path: Path, name: str, cmd: str) -> int:
    if shutil.which("powershell") is None:
        pytest.skip("no powershell on PATH")
    body = "\n\n".join([
        ps1_function(PS1_TEXT, "function _is_our_hass_pid"),
        ps1_function(PS1_TEXT, "function _is_our_broker_pid"),
    ])
    script = tmp_path / "owner.ps1"
    script.write_text(
        PS_SCRIPT.format(name=name, cmd=cmd, body=body),
        encoding="utf-8-sig",
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode in (0, 1), (
        f"owner probe errored: rc={proc.returncode} {proc.stdout} {proc.stderr}"
    )
    return proc.returncode


@pytest.mark.parametrize("name,cmd,expected", [
    ("python.exe", "OUR_BROKER", 0),
    ("python.exe", "OUR_HASS", 0),
    ("mosquitto.exe", "FOREIGN_MOSQUITTO", 1),
    ("python.exe", "FOREIGN_AMQTT", 1),
    ("mosquitto.exe", "OUR_BROKER", 1),
])
def test_ps1_owner_predicate(tmp_path: Path, name: str, cmd: str,
                             expected: int) -> None:
    root = Path.home() / ".homeant"
    cmds = {
        "OUR_BROKER": f'python.exe -m amqtt.scripts.broker_script -c {root / "mqtt.yaml"}',
        "OUR_HASS": f'python.exe hass.exe --ignore-os-check --config {root}',
        "FOREIGN_MOSQUITTO": "mosquitto.exe -c C:/other/mosquitto.conf",
        "FOREIGN_AMQTT": "python.exe -m amqtt.scripts.broker_script -c C:/other/amqtt.yaml",
    }
    got = run_ps1_owner(tmp_path, name, cmds[cmd])
    assert got == expected, (
        f"{cmd} (image {name}): predicate said {'ours' if got == 0 else 'foreign'}, "
        f"expected {'ours' if expected == 0 else 'foreign'}"
    )
