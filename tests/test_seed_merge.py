"""D06 — a list-root file (``automations.yaml``) has to be merged, not skipped.

``_merge_file`` bails out on ``not isinstance(tpl, dict) or not isinstance(cur,
dict)``.  ``automations.yaml`` is a YAML **list**, so a deployed copy was never
touched again after the first install: every automation added in a later release
reached fresh machines only, and the upgrade reported success either way.  That
is the worst shape a seed bug can have — invisible on the machine that ships it.

The merge itself already exists (``_merge_lists``, identity by ``id`` then by
content); what was missing was routing list roots to it.  So the criteria here
are the ones a user would notice:

* the new element appears, and the existing one keeps *its* values;
* running it twice adds nothing (an upgrade path may run on every start);
* the file's own text survives — comments included, because this is a file the
  user may have hand-written and HA's UI rewrites on save;
* the first write leaves ``.bak`` behind, and only the first;
* the whole deploy path (``_deploy_config_files``) shows the same thing, which
  is the code that actually runs on upgrade.
"""

from __future__ import annotations

import json

import yaml

from gate_paths import REPO_ROOT

# The file that actually ships: asserted on directly, because a synthetic
# fixture cannot prove the real seed's elements carry an `id` — and if they did
# not, every `start` would append a duplicate.
SHIPPED_SEED = REPO_ROOT / "config" / "automations.yaml"

# What the user's machine holds: one automation, edited by hand (alias MINE),
# plus a comment that a YAML round-trip would destroy.
USER_FILE = """\
# keep my comment - a YAML round-trip would eat it
- id: keep
  alias: MINE
  trigger: []
"""

SEED_ONE = """\
- id: keep
  alias: THEIRS
  trigger: []
"""

SEED_TWO = """\
- id: keep
  alias: THEIRS
  trigger: []
- id: added
  alias: HomeAnt Voice Discover Speakers
  trigger: []
  action: []
"""


def _ids(path) -> list[str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, list), f"{path.name} is not a list root: {type(data)}"
    return [str(entry["id"]) for entry in data]


def _trees(tmp_path):
    seed_dir = tmp_path / "seed"
    live_dir = tmp_path / "live"
    seed_dir.mkdir()
    live_dir.mkdir()
    seed = seed_dir / "automations.yaml"
    live = live_dir / "automations.yaml"
    return seed, live


def test_new_element_is_appended_and_existing_values_win(cli, tmp_path):
    seed, live = _trees(tmp_path)
    seed.write_text(SEED_TWO, encoding="utf-8")
    live.write_text(USER_FILE, encoding="utf-8")

    assert cli._merge_file(seed, live) is True, (
        "a list-root file was reported as nothing to merge: the deployed "
        "automations.yaml keeps the content of the release that first created it"
    )

    assert _ids(live) == ["keep", "added"]
    data = yaml.safe_load(live.read_text(encoding="utf-8"))
    assert data[0]["alias"] == "MINE", (
        f"the user's value was overwritten by the seed: {data[0]!r}"
    )


def test_existing_text_survives_the_merge(cli, tmp_path):
    seed, live = _trees(tmp_path)
    seed.write_text(SEED_TWO, encoding="utf-8")
    live.write_text(USER_FILE, encoding="utf-8")

    cli._merge_file(seed, live)

    text = live.read_text(encoding="utf-8")
    assert "# keep my comment" in text, (
        "the merge re-serialised the file and dropped the user's comments; "
        "appending the missing entries leaves the rest byte-identical"
    )


def test_second_run_is_a_no_op(cli, tmp_path):
    seed, live = _trees(tmp_path)
    seed.write_text(SEED_TWO, encoding="utf-8")
    live.write_text(USER_FILE, encoding="utf-8")

    cli._merge_file(seed, live)
    first = live.read_text(encoding="utf-8")

    assert cli._merge_file(seed, live) is False, "merge is not idempotent: it rewrote an up-to-date file"
    assert live.read_text(encoding="utf-8") == first
    assert _ids(live) == ["keep", "added"]


def test_first_write_leaves_one_backup(cli, tmp_path):
    seed, live = _trees(tmp_path)
    seed.write_text(SEED_TWO, encoding="utf-8")
    live.write_text(USER_FILE, encoding="utf-8")

    cli._merge_file(seed, live)
    bak = live.with_name(live.name + ".bak")
    assert bak.is_file(), "no .bak next to a file that is about to be rewritten"
    assert bak.read_text(encoding="utf-8") == USER_FILE

    # A second upgrade must not clobber the original with the already-merged file.
    seed.write_text(SEED_TWO + "- id: third\n  alias: Later\n  trigger: []\n",
                    encoding="utf-8")
    cli._merge_file(seed, live)
    assert bak.read_text(encoding="utf-8") == USER_FILE, ".bak was overwritten on a later merge"


def test_dict_root_still_merges(cli, tmp_path):
    """The list branch must not change how mapping files merge."""
    seed = tmp_path / "seed.yaml"
    live = tmp_path / "live.yaml"
    seed.write_text("a: 1\nextra: 2\n", encoding="utf-8")
    live.write_text("a: 9\n", encoding="utf-8")

    assert cli._merge_file(seed, live) is True
    assert yaml.safe_load(live.read_text(encoding="utf-8")) == {"a": 9, "extra": 2}


def test_the_shipped_seed_is_a_no_op_on_an_up_to_date_copy(cli, tmp_path):
    """The real seed, against a live file that already has it plus one of the user's.

    A false positive here is expensive: `start` merges on every run, so a merge
    that always thinks something is new would rewrite the user's automations
    (and drop their comments) on every boot.
    """
    live = tmp_path / "automations.yaml"
    live.write_text(
        SHIPPED_SEED.read_text(encoding="utf-8")
        + "\n- id: mine\n  alias: User's own automation\n  trigger: []\n",
        encoding="utf-8",
    )
    before = live.read_bytes()

    assert cli._merge_file(SHIPPED_SEED, live) is False, (
        "a deployed file that already contains every shipped automation was "
        "rewritten anyway"
    )
    assert live.read_bytes() == before


def test_deploy_path_picks_up_a_new_automation(cli, tmp_path, monkeypatch):
    """The upgrade path end to end, with HA_ROOT redirected to a temp dir."""
    config = tmp_path / "config"
    config.mkdir()
    seed = config / "automations.yaml"
    seed.write_text(SEED_ONE, encoding="utf-8")
    ha_root = tmp_path / "homeant"
    ha_root.mkdir()

    monkeypatch.setattr(cli, "_CONFIG_DIR", config)
    monkeypatch.setattr(cli, "HA_ROOT", ha_root)

    cli._deploy_config_files()
    live = ha_root / "automations.yaml"
    assert _ids(live) == ["keep"], "a fresh deploy should copy the seed verbatim"

    # Next release ships one more automation; the existing install must get it.
    seed.write_text(SEED_TWO, encoding="utf-8")
    cli._deploy_config_files()

    assert _ids(live) == ["keep", "added"], (
        "an installed machine never receives automations added by an upgrade"
    )


# The value an older release shipped: a rendered *list*, which a
# template trigger reads as false, forever.  An install that already holds it
# never sees the fix, because the merge only appends and this element's `id`
# already exists.
LEGACY_TRIGGER = (
    "{{ states.notify | map(attribute='entity_id') "
    "| select('search', '_play_text_a_') | list }}"
)

# What such an install has on disk: the defective line, the same expression
# inside `variables:` (legitimate there — it feeds the action), and a comment a
# YAML round-trip would destroy.
LEGACY_FILE = (
    "# my note - must survive\n"
    "- id: homeant_voice_discover_speakers\n"
    "  alias: HomeAnt Voice Discover Speakers\n"
    "  trigger:\n"
    "    - platform: homeassistant\n"
    "      event: start\n"
    "    - platform: template\n"
    f'      value_template: "{LEGACY_TRIGGER}"\n'
    "  action:\n"
    "    - variables:\n"
    "        speakers: >-\n"
    "          {{ states.notify | map(attribute='entity_id')\n"
    "             | select('search', '_play_text_a_') | list }}\n"
)


def test_a_value_that_shipped_broken_is_repaired_in_place(cli, tmp_path):
    """A deployed line the release has since fixed must be corrected, not kept.

    An append-only merge cannot reach it: the automation's `id` matches, so
    nothing is added, and the machine keeps the always-false trigger for the
    life of the install — measured on this machine after D10 shipped.
    """
    live = tmp_path / "automations.yaml"
    live.write_text(LEGACY_FILE, encoding="utf-8")

    assert cli._merge_file(SHIPPED_SEED, live) is True, (
        "the merge reported nothing to do for a file that still carries the "
        "known-bad trigger: an existing install never receives the fix"
    )

    text = live.read_text(encoding="utf-8")
    assert f'value_template: "{LEGACY_TRIGGER}"' not in text, (
        "the defective value is still there"
    )
    fixed = yaml.safe_load(text)[0]["trigger"][1]["value_template"]
    assert fixed != LEGACY_TRIGGER and "!=" in fixed, (
        f"the trigger was not replaced with the boolean comparison: {fixed!r}"
    )
    assert "# my note - must survive" in text, "the user's comment was lost"

    # The identical expression inside `variables:` is legitimate — it feeds the
    # action.  A whole-file text replace would have broken it here.
    action = yaml.safe_load(text)[0]["action"]
    speakers = action[0]["variables"]["speakers"]
    assert "| list" in speakers and "!=" not in speakers, (
        f"the action's own template was rewritten: {speakers!r}"
    )


def test_the_repair_is_idempotent(cli, tmp_path):
    """`start` merges on every run, so the repair has to settle after one pass."""
    live = tmp_path / "automations.yaml"
    live.write_text(LEGACY_FILE, encoding="utf-8")

    assert cli._merge_file(SHIPPED_SEED, live) is True
    settled = live.read_bytes()
    assert cli._merge_file(SHIPPED_SEED, live) is False, (
        "the repair fires again on a file it already fixed — every boot would "
        "rewrite the user's automations"
    )
    assert live.read_bytes() == settled


# A released scripts.yaml drops one script and fixes another's value.  The
# deployed copy also holds a script the user created in HA's UI, which must
# survive both changes.
SEED_SCRIPTS_BEFORE = """\
script_a:
  alias: OLD
  sequence: []
script_b:
  alias: GOING AWAY
  sequence: []
"""
SEED_SCRIPTS_AFTER = """\
script_a:
  alias: FIXED
  sequence: []
"""
LIVE_SCRIPTS = """\
script_a:
  alias: OLD
  sequence: []
script_b:
  alias: GOING AWAY
  sequence: []
my_ui_script:
  alias: mine
  sequence: []
"""


def test_a_dropped_seed_key_is_removed_but_ui_scripts_survive(cli, tmp_path):
    """A removed script is a deletion, not an orphan the user must clean up."""
    seed = tmp_path / "seed" / "scripts.yaml"
    live = tmp_path / "live" / "scripts.yaml"
    seed.parent.mkdir()
    live.parent.mkdir()
    seed.write_text(SEED_SCRIPTS_AFTER, encoding="utf-8")
    live.write_text(LIVE_SCRIPTS, encoding="utf-8")
    (live.parent / ".seed-keys.json").write_text(
        '{"scripts.yaml": ["script_a", "script_b"]}', encoding="utf-8")

    assert cli._merge_file(seed, live) is True

    data = yaml.safe_load(live.read_text(encoding="utf-8"))
    assert data["script_a"]["alias"] == "FIXED", "the surviving script was not updated"
    assert "script_b" not in data, "the script this release dropped is still deployed"
    assert data["my_ui_script"]["alias"] == "mine", (
        "a script the seed never shipped was deleted as collateral"
    )


def test_the_removal_is_idempotent(cli, tmp_path):
    """`start` deploys on every boot; a settled file must be a no-op."""
    seed = tmp_path / "seed" / "scripts.yaml"
    live = tmp_path / "live" / "scripts.yaml"
    seed.parent.mkdir()
    live.parent.mkdir()
    seed.write_text(SEED_SCRIPTS_AFTER, encoding="utf-8")
    live.write_text(LIVE_SCRIPTS, encoding="utf-8")
    (live.parent / ".seed-keys.json").write_text(
        '{"scripts.yaml": ["script_a", "script_b"]}', encoding="utf-8")

    assert cli._merge_file(seed, live) is True
    settled = live.read_bytes()
    assert cli._merge_file(seed, live) is False, (
        "the stale-key removal fires again on every start"
    )
    assert live.read_bytes() == settled


def test_bootstrap_snapshot_on_an_install_that_predates_it(cli, tmp_path, monkeypatch):
    """A healthy install never re-deploys; start must still seed the snapshot."""
    config = tmp_path / "config"
    config.mkdir()
    seed = config / "scripts.yaml"
    seed.write_text(SEED_SCRIPTS_AFTER, encoding="utf-8")
    ha_root = tmp_path / "homeant"
    ha_root.mkdir()
    (ha_root / "scripts.yaml").write_text(LIVE_SCRIPTS, encoding="utf-8")
    monkeypatch.setattr(cli, "_CONFIG_DIR", config)
    monkeypatch.setattr(cli, "HA_ROOT", ha_root)
    monkeypatch.setattr(cli, "_seed_ready", lambda: True)

    cli._ensure_seed()

    snapshot = ha_root / ".seed-keys.json"
    assert snapshot.is_file(), "a start on an up-to-date install left no snapshot"
    assert set(json.loads(snapshot.read_text(encoding="utf-8"))
               ["scripts.yaml"]) == {"script_a"}


def test_deploy_path_records_and_removes_seed_keys(cli, tmp_path, monkeypatch):
    """The path upgrades actually use: snapshot written, stale key removed."""
    config = tmp_path / "config"
    config.mkdir()
    seed = config / "scripts.yaml"
    seed.write_text(SEED_SCRIPTS_BEFORE, encoding="utf-8")
    ha_root = tmp_path / "homeant"
    ha_root.mkdir()
    monkeypatch.setattr(cli, "_CONFIG_DIR", config)
    monkeypatch.setattr(cli, "HA_ROOT", ha_root)

    cli._deploy_config_files()
    live = ha_root / "scripts.yaml"
    snapshot = ha_root / ".seed-keys.json"
    assert yaml.safe_load(live.read_text(encoding="utf-8")) == yaml.safe_load(
        SEED_SCRIPTS_BEFORE)
    assert set(json.loads(snapshot.read_text(encoding="utf-8"))
               ["scripts.yaml"]) == {"script_a", "script_b"}

    # The user adds a script; the next release drops script_b.
    text = live.read_text(encoding="utf-8") + "my_ui_script:\n  alias: mine\n  sequence: []\n"
    live.write_text(text, encoding="utf-8")
    seed.write_text(SEED_SCRIPTS_AFTER, encoding="utf-8")
    cli._deploy_config_files()

    data = yaml.safe_load(live.read_text(encoding="utf-8"))
    assert data["script_a"]["alias"] == "FIXED"
    assert "script_b" not in data
    assert data["my_ui_script"]["alias"] == "mine"
    assert set(json.loads(snapshot.read_text(encoding="utf-8"))
               ["scripts.yaml"]) == {"script_a"}


# configuration.yaml is user-owned, but several top-level keys in it are the
# seed's.  It cannot go through the dict merge at all: ``!include`` makes
# ``safe_load`` raise, so it merges at text level — which means the removal has
# to happen there too, or a retired integration keeps its key (and stays
# enabled) in every deployed copy.
SEED_CONFIG = """\
homeassistant:
  language: zh-Hans
homeant_voice:
input_select:
  homeant_voice_speaker:
    name: HomeAnt Voice Speaker
automation: !include automations.yaml
"""

# What the machine holds: one seed key this release retires (homeant_dlna), one
# seed key the user renamed, one dropdown the user added, and the include that
# wires automations.yaml — losing that would take every automation with it.
LIVE_CONFIG = """\
homeassistant:
  language: en
homeant_dlna:
  enabled: true
input_select:
  homeant_voice_speaker:
    name: renamed by me
  my_own_dropdown:
    name: mine
automation: !include automations.yaml
"""


def _top_keys(text: str) -> set[str]:
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    assert isinstance(root, yaml.MappingNode), f"not a mapping: {root!r}"
    return {k.value for k, _ in root.value if isinstance(k, yaml.ScalarNode)}


def _config_trees(tmp_path):
    seed = tmp_path / "seed" / "configuration.yaml"
    live = tmp_path / "live" / "configuration.yaml"
    seed.parent.mkdir()
    live.parent.mkdir()
    seed.write_text(SEED_CONFIG, encoding="utf-8")
    live.write_text(LIVE_CONFIG, encoding="utf-8")
    (live.parent / ".seed-keys.json").write_text(
        json.dumps({"configuration.yaml": [
            "homeassistant", "homeant_dlna", "input_select"]}),
        encoding="utf-8")
    return seed, live


def test_a_retired_top_level_key_leaves_an_include_bearing_config(cli, tmp_path):
    """The key the seed dropped must go — add-only left it in every install."""
    seed, live = _config_trees(tmp_path)

    assert cli._merge_file(seed, live) is True

    text = live.read_text(encoding="utf-8")
    assert "homeant_dlna" not in text, (
        "the retired integration key is still in the deployed configuration.yaml"
    )
    assert "homeant_voice" in text, "the key this release added was not spliced in"
    assert "!include automations.yaml" in text, (
        "the include wiring was destroyed with it — that takes every automation"
    )
    assert _top_keys(text) == {
        "homeassistant", "homeant_voice", "input_select", "automation"}
    # Neither the user's rename nor their own dropdown may be collateral.
    assert "renamed by me" in text, "a template value replaced the user's own"
    assert "my_own_dropdown" in text, "the user's dropdown was removed with the seed's"


def test_the_include_removal_is_idempotent(cli, tmp_path):
    """`start` deploys on every boot; a settled file must not be rewritten."""
    seed, live = _config_trees(tmp_path)

    assert cli._merge_file(seed, live) is True
    settled = live.read_bytes()
    assert cli._merge_file(seed, live) is False, (
        "the removal fires again on a file it already fixed"
    )
    assert live.read_bytes() == settled


def test_configuration_yaml_is_owned_but_never_replaced(cli):
    """Ownership and replacement are different rights — conflating them wipes data.

    A replace-meaning entry for configuration.yaml would swap the whole
    input_select: block for the template's, taking the user's dropdowns with it.
    """
    assert "configuration.yaml" in cli._SEED_ENTRY_OWNED_FILES
    assert "configuration.yaml" not in cli._SEED_ENTRY_REPLACE_FILES
    assert "scripts.yaml" in cli._SEED_ENTRY_REPLACE_FILES


def test_deploy_records_keys_for_an_include_bearing_file(cli, tmp_path, monkeypatch):
    """safe_load cannot construct !include, so the snapshot needs compose."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "configuration.yaml").write_text(SEED_CONFIG, encoding="utf-8")
    ha_root = tmp_path / "homeant"
    ha_root.mkdir()
    monkeypatch.setattr(cli, "_CONFIG_DIR", config)
    monkeypatch.setattr(cli, "HA_ROOT", ha_root)

    cli._deploy_config_files()

    snapshot = json.loads(
        (ha_root / ".seed-keys.json").read_text(encoding="utf-8"))
    assert set(snapshot["configuration.yaml"]) == {
        "homeassistant", "homeant_voice", "input_select", "automation"}


def test_bootstrap_records_every_owned_file(cli, tmp_path, monkeypatch):
    """One snapshot holds every owned file; a per-file check must not skip any."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "scripts.yaml").write_text(SEED_SCRIPTS_AFTER, encoding="utf-8")
    (config / "configuration.yaml").write_text(SEED_CONFIG, encoding="utf-8")
    ha_root = tmp_path / "homeant"
    ha_root.mkdir()
    (ha_root / "scripts.yaml").write_text(LIVE_SCRIPTS, encoding="utf-8")
    (ha_root / "configuration.yaml").write_text(LIVE_CONFIG, encoding="utf-8")
    monkeypatch.setattr(cli, "_CONFIG_DIR", config)
    monkeypatch.setattr(cli, "HA_ROOT", ha_root)
    monkeypatch.setattr(cli, "_seed_ready", lambda: True)

    cli._ensure_seed()

    snapshot = json.loads(
        (ha_root / ".seed-keys.json").read_text(encoding="utf-8"))
    assert set(snapshot) == {"scripts.yaml", "configuration.yaml"}, (
        f"bootstrap skipped a file and the next release cannot prune it: {snapshot}"
    )

