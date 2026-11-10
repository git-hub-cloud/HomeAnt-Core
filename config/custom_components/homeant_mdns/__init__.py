"""HomeAnt mDNS custom_component.

Publishes hostnames -> this machine's LAN IP.

  * homeassistant.local — xiaomi_home's OAuth callback host (miot/const.py:142
    hardcodes it into OAUTH_REDIRECT_URL); that name cannot change.
  * HomeAnt.local — the short name handed to MCP clients
    (homeant.py's MCP_HOSTNAME).

Both names live under a private service type (``_homeant._tcp``).  HA core's
own zeroconf integration registers ``_home-assistant._tcp`` unconditionally
once ``frontend`` starts (server = ``{uuid}.local``, instance =
``location_name``); registering a second entry there for the same machine
makes LAN-wide HA discovery see a duplicate.  We only need the A records
anyway, so a private type carries them without polluting discovery.

**One address, on the default gateway's own segment.**  The record carries the
single IPv4 a LAN client can actually reach.  Announcing several A records is
not an option here: some resolvers hand back only the first answer, and on a
box that also runs Hyper-V, VirtualBox, WSL or Tailscale that answer can be a
172.x virtual switch or a 100.64/10 tunnel — an address no other machine can
reach.  A client that only honours the first record then pings into the void.
RFC 6762 §6.2 lets a multihomed host advertise every address *of the interface
it is answering on*; our two backends register a host name globally rather
than per interface, so one LAN address is the honest choice.  The single
exception is ``homeassistant.local`` on a desktop, pinned to loopback on
purpose (see ``_announce_address``) — still exactly one A record per name,
never two for the same name.

Selection is by *route*, not by vendor and not by HA's adapter settings.  HA's
``async_load_adapters()`` marks an adapter ``auto`` from a UDP probe to
224.0.0.251; on Windows that probe lands on the WSL/Hyper-V NAT interface, so
``enable_auto_detected_adapters()`` enables *only* that interface and
``async_get_adapters()`` reports no LAN address at all.  Worse,
``network/__init__.py:85`` rewrites a source-probe answer that is not on an
enabled adapter into ``all_ipv4s[0]``.  Both of HA's source probes therefore
converge on the wrong 172.x address (measured on this machine 11-07).

So the candidates come from ``async_get_adapters()`` **without** the
``enabled`` filter — that flag is a UI setting, not a statement about
reachability — and the source address comes from a raw UDP ``connect()`` to a
public target: the kernel's own route lookup, which by definition picks the
address on the default gateway's segment.  No packet is transmitted, so a
firewalled or absent 8.8.8.8 changes nothing.  If that address is not in HA's
adapter list (the list can lag a hot-plugged interface), a candidate sharing
its configured subnet wins — the "gateway segment" fallback without parsing
any platform's routing table.  The 224.0.0.251 probe is the second choice for
a box with no default route at all (isolated LAN).  Loopback and link-local
are the only exclusions, because no other machine can ever reach them; no
address block is blacklisted, since a box whose only network really is a
100.64/10 tunnel still has to announce something.  Every other address is
logged at debug instead of guessed at.

**Two backends, mutually exclusive** — never run both, they fight over the same
names (see below):

  * Linux / Windows — register on HA's **shared** zeroconf instance obtained
    via ``async_get_async_instance(hass)``.  HA already owns UDP/5353, so there
    is no port conflict.  macOS (Bonjour) and Windows (built-in mDNS) resolve
    ``.local`` natively; Linux clients need avahi + nss-mdns.

  * macOS 15+ — hand the announcement to mDNSResponder through ``dns-sd -P``
    instead of sending packets ourselves.  The Local Network privacy control
    authorises by code identity: an ad-hoc / linker-signed interpreter (which
    is what ``uv`` installs — Identifier=-, no TeamIdentifier, Info.plist not
    bound) gets EHOSTUNREACH on every sendto to an on-link host or to
    224.0.0.251:5353, so zeroconf cannot announce at all.  ``/usr/bin/dns-sd``
    is Apple-signed, so it is exempt, and it publishes the A records for our
    custom host names as well — something DNSServiceRegister alone does not do.
    Requires no admin rights and no GUI session.  mDNSResponder owns every
    interface, so the A record we hand it is what decides "which adapter".

  * WSL — mode-dependent.  The guest's address follows the host's
    ``networkingMode`` (``nat``, ``mirrored``, ``consomme``/``virtioproxy``,
    ``bridged``); it is not necessarily a NAT'd 172.x — with Consomme on a
    bridged switch the guest can share the host's own LAN address, and a LAN
    client can then reach :8123 by IP.  The effective mode comes from
    ``wslinfo --networking-mode`` (WSL 2.0.4+), not ``.wslconfig`` alone.
    ``mirrored`` is the only mode Microsoft documents as carrying multicast:
    there the guest shares the host's physical adapter, so this component
    takes the regular Linux/Windows path and registers both names on HA's
    shared zeroconf instance.  In every other mode — and whenever the mode
    cannot be read — announcing has no usable path out (under Consomme raw
    Ethernet frames become ordinary host sockets), so we announce nothing and
    ``homeant.py`` asks for the Windows hosts entry instead: the browser that
    follows the OAuth callback runs on the Windows side, and WSL2 forwards
    ``127.0.0.1`` to the guest.  Measured 11-08 on a Consomme guest, a forced
    registration still reached no LAN client and hand-sent multicast reached
    only the Windows host, never the physical LAN.  The mirrored path is
    **unverified on real hardware**: this host runs Windows 10, below
    mirrored's Win11 22H2 requirement, so it needs a mirrored machine before
    release.

Why the two backends must not overlap: measured on macOS 15.7 — a second
responder for the same host name is **not** rejected (DNSServiceRegister and
zeroconf both report success), the A record silently flips to whichever
registered last, and a duplicate instance name gets renamed to "probe9 (2)".
Worse, whichever responder exits first sends a TTL=0 goodbye that erases the
record the other one is still advertising, so clients flap.
"""

from __future__ import annotations

import asyncio
from ipaddress import IPv4Address, IPv4Network
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys

from homeassistant.components import network
from homeassistant.components.network.const import MDNS_TARGET_IP, PUBLIC_TARGET_IP
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

if sys.platform != "darwin":
    from homeassistant.components.zeroconf import async_get_async_instance
    from homeassistant.components.zeroconf import DOMAIN as ZEROCONF_DOMAIN
    from homeassistant.setup import async_when_setup_or_start
    from zeroconf.asyncio import AsyncServiceInfo

_LOGGER = logging.getLogger(__name__)

DOMAIN = "homeant_mdns"

HOSTNAME = "homeassistant"
ALIAS_HOSTNAME = "HomeAnt"
# Private type shared by both names — see docstring.
SERVICE_TYPE = "_homeant._tcp.local."
SERVICE_NAME = f"{HOSTNAME}.{SERVICE_TYPE}"
ALIAS_SERVICE_NAME = f"{ALIAS_HOSTNAME}.{SERVICE_TYPE}"

# ── macOS: mDNSResponder proxy ───────────────────────────────────────
# dns-sd -P <Name> <Type> <Domain> <Port> <Host> <IP>
DNS_SD = "/usr/bin/dns-sd"
SD_TYPE = "_homeant._tcp"
SD_DOMAIN = "local"
# Matches the dns-sd children this component starts.  Used to reap orphans left
# by a hass that died before running its stop handler: a stale child keeps the
# instance name registered, so the next run would come up as "HomeAnt (2)".
_STALE_PATTERN = r"dns-sd -P .*_homeant\._tcp"

# How long dns-sd gets to either fail outright or stay up before we call it good.
_DNS_SD_SETTLE_SEC = 2.0


def _is_wsl() -> bool:
    """True inside WSL (mirrored guests announce, the others do not).

    Same predicate as homeant.py's is_wsl(): the env var is set by every
    launcher, the kernel release string is the fallback for a bare `wsl -e`
    shell that inherited no WSL_* variables.
    """
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text(
            encoding="utf-8"
        ).lower()
    except OSError:
        return False


def _wsl_networking_mode() -> str:
    """Effective WSL networking mode, or "" when it cannot be determined.

    ``wslinfo`` ships with the WSL engine (``/usr/bin/wslinfo -> /init``, since
    2.0.4) and reports the mode that actually took effect; ``.wslconfig`` is
    only a request and can fall back or be overridden.  ``mirrored`` is the
    only mode documented to carry multicast onto the LAN.  An unreadable mode
    is deliberately treated as "not mirrored": homeant.py keeps asking for the
    Windows hosts entry in that case, so both sides must agree.  Called through
    the executor to keep this subprocess off HA's event loop.  Keep this
    identical to homeant.py's _wsl_networking_mode().
    """
    try:
        out = subprocess.run(
            ["wslinfo", "--networking-mode"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    return out.stdout.strip().lower()


def _announce_address(hostname: str, lan: str) -> str:
    """The address to publish for one of our two names.

    A desktop pins ``homeassistant.local`` to loopback: the browser that
    completes the Xiaomi OAuth flow and the callback it returns to both live on
    this machine, so the name only has to work here — and loopback works before
    any network route exists.  ``HomeAnt.local`` keeps the LAN address, because
    that is the name MCP clients on other devices dial.  A server has no local
    browser: the user pastes the URL into some other device, which follows the
    ``redirect_uri`` host, so a loopback record would send that device to
    itself.  Both names therefore carry the LAN address there.

    "Desktop" is decided by platform, not by a graphical-session probe: the
    only two desktop platforms this ships on are Windows and macOS, and a
    Linux box here is always the headless server.  Measured 11-10: HA itself
    announces only ``<uuid>.local``, never ``homeassistant.local``, so pinning
    this name cannot collide with a second A record for it.
    """
    if hostname == HOSTNAME and sys.platform in ("win32", "darwin"):
        return "127.0.0.1"
    return lan


def _is_announcable(ip: str) -> bool:
    """False only for addresses no other machine could ever reach.

    Loopback and link-local are dropped because no other machine can ever use
    them: 169.254/16 only exists before DHCP finishes, and 127.0.0.1 is what a
    bare 224.0.0.251 probe answers on Windows.  Every other address stays a
    candidate — including tunnel addresses, because a box whose only uplink is
    a tunnel still has to announce something.
    """
    try:
        addr = IPv4Address(ip)
    except ValueError:
        return False
    return not (addr.is_loopback or addr.is_link_local)


def _route_source(target: str) -> str | None:
    """The local address the OS would put on a packet heading to ``target``.

    A UDP ``connect()`` resolves the route and binds the socket without
    transmitting anything; the target need not exist.  This deliberately
    bypasses HA's ``async_get_source_ip``: when its answer is not on an
    *enabled* adapter, that helper substitutes ``all_ipv4s[0]``
    (``network/__init__.py:85``), and on a machine with WSL/Hyper-V that first
    address is the virtual switch, not the LAN.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((target, 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def _same_segment(address: str,
                  candidates: list[tuple[str, str, int]]) -> str | None:
    """First candidate whose configured subnet contains ``address``."""
    try:
        probe = IPv4Address(address)
    except ValueError:
        return None
    for ip, _name, prefix in candidates:
        try:
            if probe in IPv4Network(f"{ip}/{prefix}", strict=False):
                return ip
        except ValueError:
            continue
    return None


async def _async_pick_address(hass) -> tuple[str | None, list[str]]:
    """Return (address to announce, the others as "ip=adapter" for the log).

    One address only: a client that reads just the first A record must not be
    handed a virtual-switch or tunnel address.  Candidates are every IPv4 on
    every adapter HA knows about, *including* adapters the user has not
    enabled — that flag is a UI setting, not a statement about reachability,
    and on this machine HA enables only the WSL NAT adapter because its
    224.0.0.251 probe lands there.

    The preferred address is the source the kernel picks for a public target:
    that interface holds the default route, so its address is on the default
    gateway's own segment.  A UDP connect sends nothing, so a black-holed
    8.8.8.8 is fine.  Where HA's adapter list spells that segment differently,
    the candidate in the same subnet wins.  The 224.0.0.251 probe covers a box
    with no default route at all (isolated LAN).  Last resort: the kernel's
    answer itself (a local, non-loopback address), then the first candidate.
    """
    adapters = await network.async_get_adapters(hass)
    candidates: list[tuple[str, str, int]] = []
    for adapter in adapters:
        for ipv4 in adapter["ipv4"]:
            ip = ipv4["address"]
            if _is_announcable(ip):
                candidates.append((ip, adapter["name"],
                                   int(ipv4.get("network_prefix", 24))))
    if not candidates:
        return None, []

    ips = [ip for ip, _, _ in candidates]
    preferred: str | None = None
    for target in (PUBLIC_TARGET_IP, MDNS_TARGET_IP):
        source = _route_source(target)
        if not source or not _is_announcable(source):
            continue
        if source in ips:
            preferred = source
        else:
            preferred = _same_segment(source, candidates) or source
        break
    if preferred is None:
        preferred = ips[0]

    return preferred, [f"{ip}={name}" for ip, name, _ in candidates
                       if ip != preferred]


async def _async_reap_stale_dns_sd() -> None:
    """Kill dns-sd -P children left behind by a previous hass run."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/pgrep",
            "-f",
            _STALE_PATTERN,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except OSError:
        return
    for token in out.split():
        if not token.isdigit():
            continue
        pid = int(token)
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            _LOGGER.debug("mDNS: reaped stale dns-sd pid %s", pid)
        except OSError:
            pass


async def _async_announce_via_mdnsresponder(hass, address: str, port: int) -> None:
    """Publish both names through mDNSResponder (macOS 15+)."""
    await _async_reap_stale_dns_sd()

    procs = []
    for instance in (HOSTNAME, ALIAS_HOSTNAME):
        host = f"{instance}.local"
        addr = _announce_address(instance, address)
        try:
            proc = await asyncio.create_subprocess_exec(
                DNS_SD,
                "-P",
                instance,
                SD_TYPE,
                SD_DOMAIN,
                str(port),
                host,
                addr,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as err:
            _LOGGER.error("mDNS: cannot start dns-sd for %s: %s", host, err)
            continue
        procs.append((instance, host, proc, addr))

    # dns-sd refuses a duplicate name immediately; a rejected registration exits
    # within a second, so a still-running child means the record went live.
    await asyncio.sleep(_DNS_SD_SETTLE_SEC)

    live = []
    for instance, host, proc, addr in procs:
        if proc.returncode is None:
            live.append(proc)
            _LOGGER.info("mDNS: %s -> %s:%s (via mDNSResponder)", host, addr, port)
        else:
            _LOGGER.error(
                "mDNS: dns-sd exited (rc=%s) for %s — name may already be taken",
                proc.returncode,
                host,
            )

    if not live:
        return

    async def _stop(event):  # noqa: ARG001 - HA passes the event
        for proc in live:
            if proc.returncode is None:
                proc.terminate()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)


async def _async_ensure_announce_interface(aio_zc, address: str) -> None:
    """Make HA's shared zeroconf instance also send on the announced address.

    HA builds that instance from its enabled-adapter list; on Windows a
    224.0.0.251 source probe can mark only a WSL/Hyper-V NAT adapter, so the
    physical LAN adapter never gets a socket and the A record we register is
    never announced where another machine can hear it.  ``interfaces`` takes
    IP addresses, not adapter names — zeroconf resolves each one to its
    interface and sets IP_MULTICAST_IF itself — so the address picked above
    is already exactly what this API wants; no adapter lookup is needed.

    Append rather than replace: HA and other integrations register on this
    same instance.  The address comes from the kernel's route lookup at
    runtime, never hard-coded, and when HA already picked the right adapter
    the call is a no-op.  Failure is best-effort — registration still proceeds.
    """
    zc = aio_zc.zeroconf
    current = getattr(zc, "_interfaces", None)
    if not isinstance(current, (list, tuple)):
        # InterfaceChoice.Default/All: zeroconf already owns every interface.
        return
    if address in current:
        return
    try:
        await aio_zc.async_update_interfaces([*current, address])
    except Exception as err:  # pragma: no cover - never break setup
        _LOGGER.warning(
            "mDNS: cannot add %s to zeroconf interfaces: %s", address, err
        )
        return
    _LOGGER.info("mDNS: announce interface added %s", address)


async def async_setup(hass, config):
    """Set up homeant_mdns."""
    if _is_wsl():
        mode = await hass.async_add_executor_job(_wsl_networking_mode)
        if mode != "mirrored":
            # Non-mirrored (or unreadable) WSL: the Windows hosts entry is the
            # only path that works — see the module docstring.  Announcing here
            # would add an unreachable record.
            _LOGGER.info(
                "mDNS: no announce in WSL (mode=%s) — homeassistant.local / "
                "HomeAnt.local resolve through the Windows hosts entry, not "
                "through mDNS", mode or "unknown",
            )
            return True
        # mirrored shares the host's physical adapter: fall through and use
        # the regular Linux/Windows backend.  Unverified on real hardware —
        # see the module docstring.
        _LOGGER.info("mDNS: WSL mirrored mode — announcing via HA zeroconf")

    address, others = await _async_pick_address(hass)
    if address is None:
        _LOGGER.warning(
            "No IPv4 address to announce — .local names will not resolve via mDNS",
        )
        return True  # not fatal; the WSL hosts entry still works
    if others:
        _LOGGER.debug(
            "mDNS: announcing %s only — other interfaces not announced: %s",
            address,
            ", ".join(others),
        )

    try:
        port = hass.http.server_port
    except Exception:
        port = 8123

    if sys.platform == "darwin":
        await _async_announce_via_mdnsresponder(hass, address, port)
        return True

    infos = [
        AsyncServiceInfo(
            SERVICE_TYPE,
            name=SERVICE_NAME,
            server=f"{HOSTNAME}.local.",
            parsed_addresses=[_announce_address(HOSTNAME, address)],
            port=port,
        ),
        AsyncServiceInfo(
            SERVICE_TYPE,
            name=ALIAS_SERVICE_NAME,
            server=f"{ALIAS_HOSTNAME}.local.",
            parsed_addresses=[_announce_address(ALIAS_HOSTNAME, address)],
            port=port,
        ),
    ]

    async def _when_zeroconf_ready(hass_, component):
        """Register as soon as HA's zeroconf creates the shared instance."""
        aio_zc = await async_get_async_instance(hass_)
        await _async_ensure_announce_interface(aio_zc, address)
        for info in infos:
            try:
                await aio_zc.async_register_service(info, strict=False)
            except Exception:  # pragma: no cover
                _LOGGER.exception("mDNS register failed for %s", info.server)
                continue
            _LOGGER.info(
                "mDNS: %s -> %s:%s",
                info.server,
                ", ".join(info.parsed_addresses()),
                port,
            )

    # Hook into zeroconf's init — guaranteed AFTER HA owns the socket.
    async_when_setup_or_start(hass, ZEROCONF_DOMAIN, _when_zeroconf_ready)

    return True
