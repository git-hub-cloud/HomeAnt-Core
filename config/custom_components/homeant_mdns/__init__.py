"""HomeAnt mDNS custom_component.

Publishes hostnames -> this machine's LAN IPs via HA's **shared** zeroconf
instance. No port conflicts, no config.yaml options.

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

Why a custom_component (not a standalone responder)?
  * Zero port conflict — HA already owns UDP/5353; we reuse the shared
    instance obtained via ``async_get_async_instance(hass)``.
  * Cross-platform — macOS (Bonjour) and Windows (built-in mDNS) resolve
    ``.local`` natively; Linux clients need avahi + nss-mdns.  WSL has no mDNS
    for ``.local`` at all, which is why homeant.py asks for a Windows
    hosts entry carrying both names instead.
"""

from __future__ import annotations

import logging

from homeassistant.components import network
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


async def async_setup(hass, config):
    """Set up homeant_mdns."""
    # Same addresses HA's own zeroconf uses (LAN IPs, no loopback).
    addresses = await network.async_get_announce_addresses(hass)
    # IPv4 only: Windows mDNS stalls ~9s resolving the IPv6 link-local AAAA
    # record (fe80::...%iface); an A-only announcement resolves instantly on
    # every client (Win/macOS/Linux avahi).  The list holds plain strings, not
    # ipaddress objects (network.async_get_announce_addresses str()s them), and
    # an IPv6 literal is the only kind that contains a colon.
    addresses = [a for a in addresses if ":" not in a]
    if not addresses:
        _LOGGER.warning(
            "No IPv4 LAN addresses for mDNS announce — .local names won't resolve via mDNS",
        )
        return True  # not fatal; the WSL hosts entry still works

    try:
        port = hass.http.server_port
    except Exception:
        port = 8123

    infos = [
        AsyncServiceInfo(
            SERVICE_TYPE,
            name=SERVICE_NAME,
            server=f"{HOSTNAME}.local.",
            parsed_addresses=addresses,
            port=port,
        ),
        AsyncServiceInfo(
            SERVICE_TYPE,
            name=ALIAS_SERVICE_NAME,
            server=f"{ALIAS_HOSTNAME}.local.",
            parsed_addresses=addresses,
            port=port,
        ),
    ]

    async def _when_zeroconf_ready(hass_, component):
        """Register as soon as HA's zeroconf creates the shared instance."""
        aio_zc = await async_get_async_instance(hass_)
        for info in infos:
            try:
                await aio_zc.async_register_service(info, strict=False)
            except Exception:  # pragma: no cover
                _LOGGER.exception("mDNS register failed for %s", info.server)
                continue
            _LOGGER.info("mDNS: %s -> %s:%s", info.server, addresses, port)

    # Hook into zeroconf's init — guaranteed AFTER HA owns the socket.
    async_when_setup_or_start(hass, ZEROCONF_DOMAIN, _when_zeroconf_ready)

    return True
