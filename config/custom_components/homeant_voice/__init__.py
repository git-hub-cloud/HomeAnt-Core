"""HomeAnt voice custom_component.

Auto-exposes the HomeAntVoice script (config/scripts.yaml) to Assist / LLM
APIs, so any MCP client (WorkBuddy / Trae / dsh ...) gets a "speak through the
Xiaomi speaker" tool via HA's native script->MCP bridge
(core/homeassistant/components/script/llm.py). No new API, no new token.

Why a custom_component instead of clicking "Expose" in the UI?
  * ``async_expose_entity()`` is a plain @callback with NO permission check
    (homeassistant/exposed_entities.py:508); the @require_admin lives only on
    the WS command layer (:396). Calling it from inside HA needs no token.
  * HA's ``homeassistant:`` YAML schema has no expose option
    (core_config.py CORE_CONFIG_SCHEMA) - core offers no file-based path, but
    our own component can: one line in configuration.yaml, versioned with the
    rest of the seed and applied by the normal reconcile.
  * Idempotent: async_set_assistant_option returns early when the value is
    unchanged, so re-exposing on every boot also self-heals a manual
    un-expose in the UI.

Timing: exposing before the entity is registered writes the LEGACY store
(.storage/homeassistant.exposed_entities), which async_should_expose never
reads back (exposed_entities.py:252) - a silent no-op. script entities are
registered during setup, so we hook EVENT_HOMEASSISTANT_START (fired after
all setups complete: bootstrap.py:541 -> core.py:525) and fall back to
entity-registry events if the script shows up later.

Configuration (configuration.yaml):

    homeant_voice:
      expose_entities:            # optional; default = discover by alias
        - script.homeant_voice
"""

from __future__ import annotations

import logging

from homeassistant.components.homeassistant import async_should_expose
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.const import EVENT_HOMEASSISTANT_START
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED
import voluptuous as vol

_LOGGER = logging.getLogger(__name__)

DOMAIN = "homeant_voice"

# Product contract, NOT environment data: config/scripts.yaml's alias.
# alias -> entity_id slug is stable across machines (script.homeant_voice),
# unlike the device dids baked into notify.* entity_ids.
SCRIPT_ALIAS = "HomeAntVoice"
SELECT_SCRIPT_ALIAS = "HomeAntVoiceSelect"

# Assist surface used by the MCP server (components/mcp_server builds its
# LLMContext with assistant="conversation").
ASSISTANT = "conversation"

_OPTIONS_SCHEMA = vol.Schema(
    {
        # Optional explicit entity_ids; default = discover scripts whose
        # names match SCRIPT_ALIASES (zero-config for end users).
        vol.Optional("expose_entities", default=list): [str],
    }
)

# The section value is None when written as a bare "homeant_voice:" key in
# configuration.yaml - accept that (homeant_mdns ships no schema at all).
CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Any(_OPTIONS_SCHEMA, None)},
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass, config):
    """Set up homeant_voice."""
    conf = config.get(DOMAIN) or {}
    explicit = conf.get("expose_entities") or []

    @callback
    def _find_targets():
        if explicit:
            return list(explicit)
        ent_reg = er.async_get(hass)
        return [
            entry.entity_id
            for entry in ent_reg.entities.values()
            if entry.domain == "script"
            and (
                SCRIPT_ALIAS in (entry.original_name, entry.name)
                or SELECT_SCRIPT_ALIAS in (entry.original_name, entry.name)
            )
        ]

    @callback
    def _expose_all():
        targets = _find_targets()
        if not targets:
            return False
        for entity_id in targets:
            async_expose_entity(hass, ASSISTANT, entity_id, True)
            # Self-check: async_set_assistant_option can silently no-op when
            # the entity is not in the registry yet (the legacy-store trap).
            if not async_should_expose(hass, ASSISTANT, entity_id):
                _LOGGER.warning(
                    "Exposed %s but self-check failed (not in registry yet?)",
                    entity_id,
                )
        _LOGGER.info("Voice bridge exposed to Assist: %s", ", ".join(targets))
        return True

    @callback
    def _on_start(_event):
        if _expose_all():
            return
        # Script not registered at START (or renamed after boot): watch the
        # registry so a late/renamed script still converges.
        _LOGGER.warning(
            "No script named %r found; voice bridge inactive. Check "
            "config/scripts.yaml alias, or set %s.expose_entities",
            SCRIPT_ALIAS,
            DOMAIN,
        )

        @callback
        def _on_registry_updated(_event):
            if _expose_all():
                unsub()

        unsub = hass.bus.async_listen(
            EVENT_ENTITY_REGISTRY_UPDATED, _on_registry_updated
        )

    # START is fired after every component's setup has completed - by then
    # yaml platform entities (script.*) are guaranteed to be registered.
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _on_start)

    return True
