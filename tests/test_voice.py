"""D04 — ``_expose_all`` must report a failure instead of swallowing it.

``async_expose_entity`` can no-op silently when the entity is not in the
registry yet (the legacy-store trap the module's docstring describes).  The
self-check after each call notices exactly that — and then the function returned
``True`` regardless, so ``_on_start`` believed it was done, never registered the
registry listener, and the entity stayed unexposed for the life of the process.

The criterion is therefore not the return value but **what ``_on_start`` does
next**: a registry listener present means "will converge later", absent means
"gave up quietly".  That is the observable difference, and it is what the user
feels as a voice bridge that works on one boot and not the next.

No Home Assistant: ``hass`` is a bus recorder and the two HA helpers the module
imported are patched on the module, which is where the closure looks them up.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock


class _Bus:
    def __init__(self) -> None:
        self.once: list[tuple[object, object]] = []
        self.listen: list[tuple[object, object, MagicMock]] = []

    def async_listen_once(self, event, callback):
        self.once.append((event, callback))

    def async_listen(self, event, callback):
        unsub = MagicMock(name="unsub")
        self.listen.append((event, callback, unsub))
        return unsub


class _Hass:
    def __init__(self) -> None:
        self.bus = _Bus()


def _patch(mod, monkeypatch, permits: dict[str, bool]) -> None:
    """Make exposure a no-op whose self-check answers from ``permits``."""
    monkeypatch.setattr(
        mod, "async_expose_entity",
        lambda hass, assistant, entity_id, expose: None,
    )
    monkeypatch.setattr(
        mod, "async_should_expose",
        lambda hass, assistant, entity_id: permits.get(entity_id, True),
    )


def _start(mod) -> _Hass:
    """Run ``async_setup`` then fire the START event the module subscribed to."""
    hass = _Hass()
    asyncio.run(mod.async_setup(
        hass, {mod.DOMAIN: {"expose_entities": ["script.a", "script.b"]}}
    ))
    assert hass.bus.once, "async_setup never subscribed to START"
    _event, on_start = hass.bus.once[0]
    on_start(None)
    return hass


def test_partial_failure_keeps_the_registry_listener(component, monkeypatch):
    mod = component("homeant_voice")
    _patch(mod, monkeypatch, {"script.a": True, "script.b": False})

    hass = _start(mod)

    assert hass.bus.listen, (
        "one entity failed its self-check but no registry listener was "
        "registered: the bridge never retries and the speaker stays invisible "
        "until the next restart"
    )
    assert len(hass.bus.listen) == 1


def test_clean_run_registers_no_listener(component, monkeypatch):
    """Nothing failed, so nothing needs watching — the retry must not become noise."""
    mod = component("homeant_voice")
    _patch(mod, monkeypatch, {"script.a": True, "script.b": True})

    hass = _start(mod)

    assert not hass.bus.listen, (
        f"listener registered although every entity exposed: {hass.bus.listen!r}"
    )


def test_listener_unsubscribes_once_it_converges(component, monkeypatch):
    """The listener exists to converge, so it has to stop once it has."""
    mod = component("homeant_voice")
    permits = {"script.a": True, "script.b": False}
    _patch(mod, monkeypatch, permits)

    hass = _start(mod)
    assert hass.bus.listen, "nothing to converge: no listener was registered"
    _event, on_registry_updated, unsub = hass.bus.listen[0]

    # The registry finally holds script.b: the same callback must now succeed
    # and drop its own subscription.
    permits["script.b"] = True
    on_registry_updated(None)

    assert unsub.called, "listener stayed subscribed after everything exposed"
