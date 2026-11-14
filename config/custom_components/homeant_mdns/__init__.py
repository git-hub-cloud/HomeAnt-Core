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
box that also runs Hyper-V, VirtualBox or Tailscale that answer can be a
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
224.0.0.251; on Windows that probe lands on the Hyper-V NAT interface, so
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
firewalled or absent 223.5.5.5 changes nothing.  If that address is not in HA's
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
from homeassistant.components.network.const import MDNS_TARGET_IP
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

# Own route probe instead of HA's PUBLIC_TARGET_IP (8.8.8.8): UDP connect sends
# nothing, so any public address works, and sharing the CLI's choice keeps the
# two route lookups on the same interface.
ROUTE_PROBE_IP = "223.5.5.5"

if sys.platform != "darwin":
    from homeassistant.components.zeroconf import async_get_async_instance
    from homeassistant.components.zeroconf import DOMAIN as ZEROCONF_DOMAIN
    from homeassistant.setup import async_when_setup_or_start
    from zeroconf import NonUniqueNameException
    from zeroconf.asyncio import AsyncServiceInfo

_LOGGER = logging.getLogger(__name__)

DOMAIN = "homeant_mdns"

HOSTNAME = "homeassistant"
ALIAS_HOSTNAME = "HomeAnt"
# Private type shared by both names — see docstring.
SERVICE_TYPE = "_homeant._tcp.local."
SERVICE_NAME = f"{HOSTNAME}.{SERVICE_TYPE}"
ALIAS_SERVICE_NAME = f"{ALIAS_HOSTNAME}.{SERVICE_TYPE}"

# The built-in broker, announced on the type every MQTT tool already looks for,
# so a LAN client can find it the way it finds any other server:
# `avahi-browse -t _mqtt._tcp`.  Same instance name as the record above — a name
# only has to be unique inside its own type, and one machine keeping one name
# makes the two records recognisably the same box.
MQTT_SERVICE_TYPE = "_mqtt._tcp.local."
MQTT_SERVICE_NAME = f"{HOSTNAME}.{MQTT_SERVICE_TYPE}"
# Named here rather than imported from the CLI: this component ships inside HA
# and is loaded by hass, which knows nothing about homeant.py.  The file sits in
# the config dir, which is where the CLI writes it.
MQTT_CONF_NAME = "mqtt.yaml"

# ── macOS: mDNSResponder proxy ───────────────────────────────────────
# dns-sd -P <Name> <Type> <Domain> <Port> <Host> <IP>
DNS_SD = "/usr/bin/dns-sd"
SD_TYPE = "_homeant._tcp"
SD_MQTT_TYPE = "_mqtt._tcp"
SD_DOMAIN = "local"
# Matches the dns-sd children this component starts.  Used to reap orphans left
# by a hass that died before running its stop handler: a stale child keeps the
# instance name registered, so the next run would come up as "HomeAnt (2)".
# Both types, or a dead run's _mqtt record would outlive the broker it points at.
#
# The instance name is part of the pattern, not decoration.  pgrep -f matches the
# whole command line, so a bare ``_(homeant|mqtt)\._tcp`` also matched every
# other app announcing an MQTT broker on the same box — and this runs a SIGTERM.
_STALE_PATTERN = r"dns-sd -P (HomeAnt|homeassistant) _(homeant|mqtt)\._tcp"


def _is_own_dns_sd(cmdline: str) -> bool:
    """True only for a command line this component itself would have started.

    The pattern above is the first filter, not the decision: ``pgrep -f`` also
    matches a ``tail -f`` on a log that merely quotes our argv, and killing a
    process we did not start is a bug with no upside.  So the pid's full command
    line is checked token by token before SIGTERM — argv[0] must be dns-sd, then
    ``-P``, then an instance and a service type we publish.

    A command line that cannot be read is treated as "not ours": a stale record
    of ours surviving one more boot is recoverable, killing a stranger is not.
    """
    parts = cmdline.split()
    if len(parts) < 4:
        return False
    if Path(parts[0]).name != "dns-sd":
        return False
    if parts[1] != "-P":
        return False
    if parts[2] not in (HOSTNAME, ALIAS_HOSTNAME):
        return False
    return parts[3] in (SD_TYPE, SD_MQTT_TYPE)

# How long dns-sd gets to either fail outright or stay up before we call it good.
_DNS_SD_SETTLE_SEC = 2.0


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


def _mqtt_record_target(lan: str) -> tuple[str, str]:
    """``(host name, address)`` the broker's SRV record points at.

    The broker is the one service in this file that nobody opens in a browser:
    every client that uses the record is on another machine, so it has to carry
    the LAN address and the host name that already carries it.  Building it from
    ``HOSTNAME`` — which is what this did — inherited the loopback pin that only
    exists for the Xiaomi OAuth callback, and a client that resolved the record
    then dialled itself.  One helper because both announce backends (``zeroconf``
    and ``dns-sd -P``) build it, and a per-site choice is how they drift.
    """
    return f"{ALIAS_HOSTNAME}.local.", _announce_address(ALIAS_HOSTNAME, lan)


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
    (``network/__init__.py:85``), and on a machine with a virtual switch that
    first address is the switch, not the LAN.
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
    and on a machine with a NAT switch HA enables only that adapter because its
    224.0.0.251 probe lands there.

    The preferred address is the source the kernel picks for a public target:
    that interface holds the default route, so its address is on the default
    gateway's own segment.  A UDP connect sends nothing, so a black-holed
    223.5.5.5 is fine.  Where HA's adapter list spells that segment differently,
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
    for target in (ROUTE_PROBE_IP, MDNS_TARGET_IP):
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


async def _async_pid_cmdline(pid: int) -> str | None:
    """Full command line of ``pid``, or None when it cannot be read.

    macOS has no /proc, and this path only ever runs there (dns-sd is the macOS
    announcement mechanism), so ``ps`` is the portable answer.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/ps",
            "-o",
            "command=",
            "-p",
            str(pid),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except OSError:
        return None
    return out.decode("utf-8", "replace").strip() or None


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
        cmdline = await _async_pid_cmdline(pid)
        if cmdline is None or not _is_own_dns_sd(cmdline):
            # pgrep matched, but the process is not one we started (or not one
            # we can identify).  Leave it alone.
            _LOGGER.debug("mDNS: not reaping pid %s (%r)", pid, cmdline)
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            _LOGGER.debug("mDNS: reaped stale dns-sd pid %s", pid)
        except OSError:
            pass


async def _async_announce_via_mdnsresponder(
    hass, address: str, port: int, mqtt_port: int | None = None
) -> None:
    """Publish both names, and the broker when there is one (macOS 15+)."""
    await _async_reap_stale_dns_sd()

    # One instance name can carry two records as long as the service types
    # differ, and dns-sd -P registers each as its own child — so the broker
    # rides on the same host record as the name, on its own port.  The broker
    # keeps the box's instance name but not its host record: that one is pinned
    # to loopback on a desktop, which no other machine can dial.
    records = [
        (HOSTNAME, f"{HOSTNAME}.local", _announce_address(HOSTNAME, address),
         SD_TYPE, port),
        (ALIAS_HOSTNAME, f"{ALIAS_HOSTNAME}.local",
         _announce_address(ALIAS_HOSTNAME, address), SD_TYPE, port),
    ]
    if mqtt_port:
        mqtt_host, mqtt_addr = _mqtt_record_target(address)
        records.append((HOSTNAME, mqtt_host, mqtt_addr, SD_MQTT_TYPE, mqtt_port))

    procs = []
    for instance, host, addr, sd_type, rec_port in records:
        try:
            proc = await asyncio.create_subprocess_exec(
                DNS_SD,
                "-P",
                instance,
                sd_type,
                SD_DOMAIN,
                str(rec_port),
                host,
                addr,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as err:
            _LOGGER.error("mDNS: cannot start dns-sd for %s: %s", host, err)
            continue
        procs.append((instance, host, proc, addr, sd_type, rec_port))

    # dns-sd refuses a duplicate name immediately; a rejected registration exits
    # within a second, so a still-running child means the record went live.
    await asyncio.sleep(_DNS_SD_SETTLE_SEC)

    live = []
    for instance, host, proc, addr, sd_type, rec_port in procs:
        if proc.returncode is None:
            live.append(proc)
            _LOGGER.info("mDNS: %s (%s) -> %s:%s (via mDNSResponder)",
                         host, sd_type, addr, rec_port)
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
    224.0.0.251 source probe can mark only a NAT adapter, so the
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


def _mqtt_broker_port(hass) -> int | None:
    """Port of the built-in broker, or None when there is no broker to point at.

    Read from the YAML the CLI generates for the broker instead of parsing cfg
    again: that file is what the broker was actually started with, so the
    announcement cannot drift from the listener.  A missing or unreadable file
    means "do not advertise" — a record naming a port nobody serves is worse
    than no record at all, because the client that finds it believes it.
    """
    try:
        text = (Path(hass.config.config_dir) / MQTT_CONF_NAME).read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return None
    # One `bind: <host>:<port>` line, written by this product — not arbitrary
    # YAML, so a hand parse is the honest amount of parsing to do here.
    for line in text.splitlines():
        entry = line.strip()
        if not entry.startswith("bind:"):
            continue
        host_port = entry[len("bind:"):].strip()
        if ":" not in host_port:
            return None
        try:
            port = int(host_port.rsplit(":", 1)[1])
        except ValueError:
            return None
        return port if 0 < port < 65536 else None
    return None


# ── announcing: conflicts and the retry ladder ───────────────────────
# A name can legitimately be taken on this network: someone started a second
# HomeAnt, or a stopped box's records are still cached elsewhere (A record TTL
# is 120 s).  The answer is bounded — three attempts over 3.5 minutes, which
# outlives that cache but does not fight a real second instance.
_REGISTER_RETRY_DELAYS = (30, 60, 120)


async def _async_register_infos(aio_zc, infos, announced=None, *, quiet=False):
    """Announce every record not announced yet; return ``(conflicts, names)``.

    ``NonUniqueNameException`` gets one line, not a traceback: on a network
    with a second HomeAnt it is the *expected* answer, and three tracebacks
    per start read like a crash in a log the user checks by eye.  Everything
    else keeps the stack — those are the ones worth debugging.

    ``quiet`` drops the conflict line to DEBUG for the retry passes: the first
    pass already said it, and repeating it per attempt turns three lines into
    nine.
    """
    announced = set() if announced is None else announced
    conflicts = []
    for info in infos:
        if info.name in announced:
            continue
        try:
            await aio_zc.async_register_service(info, strict=False)
        except NonUniqueNameException:
            conflicts.append(info)
            if quiet:
                _LOGGER.debug("mDNS: %s is still held", info.server)
            else:
                _LOGGER.warning(
                    "mDNS: %s is already announced on this network — another "
                    "HomeAnt instance, or the records a stopped one left "
                    "behind, holds the name; retrying in the background",
                    info.server,
                )
            continue
        except Exception:  # pragma: no cover - depends on the backend
            _LOGGER.exception("mDNS register failed for %s", info.server)
            continue
        announced.add(info.name)
        _LOGGER.info(
            "mDNS: %s -> %s:%s",
            info.server,
            ", ".join(info.parsed_addresses()),
            # The record's own port.  This said `port` — the web port, still in
            # scope — so the broker's line printed 8123 and read like a correct
            # announcement.
            info.port,
        )
    return conflicts, announced


async def _async_retry_until_announced(hass, aio_zc, infos, announced):
    """Retry the records a neighbour holds; give up loudly, never silently."""
    for delay in _REGISTER_RETRY_DELAYS:
        await asyncio.sleep(delay)
        if getattr(hass, "is_stopping", False):
            return
        # The verdict is `announced`, not the conflicts this pass returned: a
        # retry can fail for a reason that is not a name clash (backend error
        # after the neighbour left), which leaves the conflict list empty while
        # the records are still off the wire.  Reading success off that list
        # printed "every record is announced" over silence.
        _, announced = await _async_register_infos(
            aio_zc, infos, announced, quiet=True)
        pending = [info for info in infos if info.name not in announced]
        if not pending:
            _LOGGER.info("mDNS: every record is announced after the retry")
            return
    _LOGGER.warning(
        "mDNS: %d record(s) still held after %d s — they stay unannounced "
        "until this box restarts. Stop the other HomeAnt on this network, "
        "then run 'homeant stop' and 'homeant start'",
        len(pending),
        sum(_REGISTER_RETRY_DELAYS),
    )


async def async_setup(hass, config):
    """Set up homeant_mdns."""
    address, others = await _async_pick_address(hass)
    if address is None:
        _LOGGER.warning(
            "No IPv4 address to announce — .local names will not resolve via mDNS",
        )
        return True  # not fatal: everything else works without mDNS
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

    # None when no broker is configured: nothing is announced for it then.
    # In the executor: the helper reads mqtt.yaml off the event loop.
    mqtt_port = await hass.async_add_executor_job(_mqtt_broker_port, hass)

    if sys.platform == "darwin":
        await _async_announce_via_mdnsresponder(hass, address, port, mqtt_port)
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
    if mqtt_port:
        # A different service type and its own port, but the *alias* host
        # record: the address is the machine as another device sees it, and the
        # name is the one that already carries that address.  ``HOSTNAME`` here
        # would announce 127.0.0.1 on a desktop (see ``_mqtt_record_target``).
        mqtt_host, mqtt_addr = _mqtt_record_target(address)
        infos.append(
            AsyncServiceInfo(
                MQTT_SERVICE_TYPE,
                name=MQTT_SERVICE_NAME,
                server=mqtt_host,
                parsed_addresses=[mqtt_addr],
                port=mqtt_port,
            )
        )

    async def _when_zeroconf_ready(hass_, component):
        """Register as soon as HA's zeroconf creates the shared instance."""
        aio_zc = await async_get_async_instance(hass_)
        await _async_ensure_announce_interface(aio_zc, address)
        conflicts, announced = await _async_register_infos(aio_zc, infos)
        if conflicts:
            # Bounded retries in the background.  A name another instance holds
            # usually frees up by itself — that instance is stopped, or the
            # records it left behind expire — and a record that stays
            # unannounced is invisible from the LAN until this box restarts.
            hass_.async_create_task(
                _async_retry_until_announced(hass_, aio_zc, infos, announced),
                "homeant_mdns announce retry",
            )

    # Hook into zeroconf's init — guaranteed AFTER HA owns the socket.
    async_when_setup_or_start(hass, ZEROCONF_DOMAIN, _when_zeroconf_ready)

    return True
