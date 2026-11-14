"""The speaker-discovery trigger must be a boolean comparison.

TODO D10.  ``platform: template`` triggers are judged through
``result_as_boolean``: anything that is not one of the accepted spellings of
true/false is **false**, and the trigger can then only ever fire on a
false → true edge.  A template that renders a list therefore never fires, no
matter how the list changes — and the comment above it claims the opposite.

Rendering the template here, with stubs for the two HA context objects it
touches, is enough to settle that: the output either is a boolean or it is not.
The live half (``--live``) re-checks the same template through HA's own engine
via ``POST /api/template``, and additionally asserts that the ``options`` of the
``input_select`` really do track the discovered entities.
"""

from __future__ import annotations

import re

import jinja2
import pytest
import yaml

from gate_paths import REPO_ROOT

AUTOMATIONS = REPO_ROOT / "config" / "automations.yaml"
AUTOMATION_ID = "homeant_voice_discover_speakers"
SELECT_ENTITY = "input_select.homeant_voice_speaker"
SPEAKER_SUFFIX = "_play_text_a_"


def trigger_template() -> str:
    """The value_template of the one template trigger in that automation."""
    docs = yaml.safe_load(AUTOMATIONS.read_text(encoding="utf-8"))
    assert isinstance(docs, list), f"{AUTOMATIONS} is not a list of automations"
    match = [a for a in docs if isinstance(a, dict) and a.get("id") == AUTOMATION_ID]
    assert match, f"no automation with id {AUTOMATION_ID!r} in {AUTOMATIONS.name}"
    triggers = [t for t in match[0].get("trigger") or []
                if isinstance(t, dict) and t.get("platform") == "template"]
    assert len(triggers) == 1, (
        f"expected exactly one template trigger, found {len(triggers)}"
    )
    return triggers[0]["value_template"]


def _search(value, find=None):
    """HA's ``search`` *test*, which the template uses via ``select('search', …)``.

    Plain Jinja has no such test (it ships ``match``/``search`` only as filters),
    so without this every render raises "No test named 'search'" and the case
    would be judging the stub instead of the template.  HA's helper is
    ``re.search(find, value)``; the live half, when it exists, is the authority.
    """
    return bool(re.search(find, value))


class _Entity:
    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id


class _States:
    """``states.<domain>`` iterated as the entities of that domain."""

    def __init__(self, entity_ids: list[str]) -> None:
        self._entities = [_Entity(e) for e in entity_ids]

    def __iter__(self):
        return iter(self._entities)


def render(entity_ids: list[str], options: list[str]) -> str:
    """Render the trigger template the way HA would, minus HA itself."""
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.tests["search"] = _search
    ctx = {
        "states": type("S", (), {"notify": _States(entity_ids)})(),
        "state_attr": lambda entity, attr: options if entity == SELECT_ENTITY else None,
    }
    return env.from_string(trigger_template()).render(**ctx)


def speakers(*dids: str) -> list[str]:
    return [f"notify.{d}{SPEAKER_SUFFIX}5_1" for d in dids]


def test_renders_a_boolean_not_a_list() -> None:
    """The whole defect: a list renders as '[…]', which HA reads as false."""
    out = render(speakers("aa", "bb"), options=["unset"])
    assert out.strip() in ("True", "False"), (
        f"trigger renders {out!r} — a template trigger only understands "
        f"true/false, so this is permanently false and the automation never runs"
    )


def test_true_when_the_option_list_is_behind() -> None:
    """A speaker the options do not know about is exactly when it must fire."""
    out = render(speakers("aa", "bb"), options=["unset", "notify.aa" + SPEAKER_SUFFIX + "5_1"])
    assert out.strip() == "True", f"a newly discovered speaker did not enable the trigger: {out!r}"


def test_false_after_the_options_were_written() -> None:
    """set_options writes the same set back, so the edge must fall again.

    Without this the trigger would sit at True forever and never fire a second
    time — the other half of "it can only detect a rising edge".
    """
    found = speakers("aa", "bb")
    out = render(found, options=["unset"] + found)
    assert out.strip() == "False", (
        f"still armed after set_options wrote the same set: {out!r}"
    )


def test_unset_does_not_count_as_a_member() -> None:
    """'unset' is the placeholder the selector carries before discovery."""
    found = speakers("aa")
    out = render(found, options=["unset"] + found)
    assert out.strip() == "False", f"'unset' was compared as a speaker: {out!r}"


def test_detects_a_removed_speaker() -> None:
    """Membership change in the other direction is a change too."""
    found = speakers("aa")
    out = render(found, options=["unset"] + speakers("aa", "bb"))
    assert out.strip() == "True", (
        f"removing a speaker left the trigger disarmed: {out!r} — the comparison "
        "must be between whole sets, not a membership test"
    )
