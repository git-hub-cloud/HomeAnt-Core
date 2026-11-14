"""D07 — the comment in ``homeant.cfg.default`` must match what ``start`` does.

The template told users the opposite of the shipped behaviour, three times in one
paragraph:

* "Provisioning is append-only: changing PASSWORD here later does NOT update it"
  — the code reconciles the HA account to ``PASSWORD`` on every ``start``
  (``_ensure_credential`` → ``provider.async_change_password``), which is the
  whole point of F4;
* it then pointed at ``hass --script auth change_password``, a path that is not
  needed and that the CLI would undo on the next start;
* "prints it exactly once" — the value is deliberately never echoed; the log
  says the password *was reset* and nothing more, because a journal line is the
  wrong place for a secret.

A comment that contradicts the code is worse than no comment: the user follows
it, their UI password change gets reverted, and the log they were told to expect
never appears.

The assertions are on the template's text but tied to the code where a number is
involved, so a change on either side alone fails the case.
"""

from __future__ import annotations

import pytest

from gate_paths import REPO_ROOT


@pytest.fixture(scope="module")
def text() -> str:
    return (REPO_ROOT / "homeant.cfg.default").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def admin_block(text) -> str:
    """Just the admin-password paragraph, up to the ``PASSWORD=`` line.

    Scoped on purpose: the MQTT paragraph two sections down used to promise a
    one-time print; now neither credential is echoed, both are only written
    back into this file.  Judge each credential's wording against its own code
    path, not against the other one.
    """
    start = text.index("# admin = HA owner")
    return text[start:text.index("PASSWORD=", start)]


def test_no_append_only_claim(admin_block):
    """The opposite of the truth: cfg is the authority, not an append-only log."""
    assert "does NOT update it" not in admin_block, (
        "the template still claims editing PASSWORD here has no effect; `start` "
        "reconciles the account to it on every run"
    )
    assert "append-only" not in admin_block


def test_no_manual_hass_script_instruction(admin_block):
    """Pointing at the manual path is what sends users around the real one."""
    assert "change_password" not in admin_block, (
        "the template still tells users to run `hass --script auth "
        "change_password`; editing the line and re-running `start` is the "
        "supported path, and the manual one is undone by the next start"
    )


def test_says_the_file_is_the_authority(admin_block):
    assert "authority" in admin_block.lower(), (
        "the template never states the rule that makes every other sentence "
        "true: what PASSWORD holds here is what the HA account ends up with"
    )


def test_names_the_pass_that_applies_it(admin_block):
    assert "start" in admin_block, (
        "the template must name the command that applies the value"
    )


def test_generated_length_matches_the_code(cli, admin_block):
    """A promise about the password's shape is a promise about ``_gen_password``."""
    assert f"{cli.PW_LEN}-character" in admin_block, (
        f"the template does not promise a {cli.PW_LEN}-character password, so "
        f"either the wording was dropped or the generated length changed"
    )


def test_does_not_promise_the_password_is_printed(admin_block):
    assert "exactly once" not in admin_block, (
        "the admin value is never echoed; promising a one-time print leaves the "
        "user waiting for a line that does not exist"
    )
    assert "prints it" not in admin_block


@pytest.fixture(scope="module")
def mqtt_block(text) -> str:
    start = text.index("# ── MQTT broker")
    return text[start:text.index("MQTT_LISTEN=", start)]


def test_mqtt_password_is_not_promised_as_printed(mqtt_block):
    """Same rule as the admin value: the generated broker password is stored,
    not echoed.  The template said "prints it once" long after `start` stopped
    printing anything, which sent users hunting a line that never appears."""
    assert "prints it" not in mqtt_block, mqtt_block
    assert "never printed" in mqtt_block, mqtt_block
