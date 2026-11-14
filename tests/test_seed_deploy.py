"""D20 — a component directory dropped from HA_ROOT has to come back on `start`.

The seed gate decides whether ``_ensure_seed`` runs by comparing a digest of
``config/`` with ``.seed-state``.  That answer is about the *release*, not about
what is on disk, so a deployed component could be missing while the marker still
matched — and then nothing put it back.  Measured shape of the bug: delete
``~/.homeant/custom_components/homeant_mdns`` and run ``start``; the gate says
"satisfied", the directory stays gone, and HA comes up without the integration
while every line of output reads like success.

``_seed_ready`` already guards the vendored side — it asks whether each
``vendor/.../custom_components/<domain>`` landed — and the shipped side, which
``_deploy_config_files`` copies out of ``config/custom_components``, had no such
check.  This case holds both halves of that asymmetry: the missing domain must
be reinstalled, and a *stale* one must still be left alone (overwriting a live
``.py`` from a matching marker would make every start a rewrite, and a user's
own edit to a deployed component would be erased on boot).

The second fact the case pins down is what ``start`` cannot do by design: a
component whose bytes changed in the tree is not updated while its marker still
matches.  That is why a source-tree iteration needs the delete (see the repair
hint the failure prints), and why ``install`` — not ``start`` — owns versions.
"""

from __future__ import annotations

import json

import pytest

from gate_paths import REPO_ROOT

SHIPPED_CC = REPO_ROOT / "config" / "custom_components"
VENDORED_CC = REPO_ROOT / "vendor" / "ha_xiaomi_home" / "custom_components"

# One real domain, so the case asserts on the file that actually ships rather
# than on a synthetic tree whose copying rules could differ.
DOMAIN = "homeant_mdns"
STALE = b"# an old build of this integration\n"


@pytest.fixture
def deployed(cli, tmp_path, monkeypatch):
    """A minimal HA_ROOT holding one stale component and a matching marker.

    ``HA_ROOT``, ``_CONFIG_DIR`` and ``SEED_MARKER`` are module-level constants
    resolved at import; the tests have to redirect all three or the deploy would
    walk the real ``config/`` and write the user's own ``~/.homeant``.
    """
    root = tmp_path / "ha"
    cc = root / "custom_components"
    (cc / DOMAIN).mkdir(parents=True)
    (cc / DOMAIN / "__init__.py").write_bytes(STALE)
    # _seed_ready also asks whether the vendored domains landed; without this
    # the marker never matches and the case would be measuring the wrong branch.
    if VENDORED_CC.is_dir():
        for d in VENDORED_CC.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                (cc / d.name).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(cli, "HA_ROOT", root)
    monkeypatch.setattr(cli, "SEED_MARKER", root / ".seed-state")
    # _CONFIG_DIR stays pointed at the real config/: the case asserts that what
    # gets copied is what this release ships, so redirecting the source would
    # make it prove nothing about the shipped tree.
    # The marker has to carry the digest the real config/ has right now, i.e.
    # "this release is already deployed" — the state every running install is in.
    (root / ".seed-state").write_text(
        json.dumps({"seed": cli._seed_digest()}), encoding="utf-8")
    return root


def test_a_dropped_domain_is_reinstalled_by_start(cli, deployed):
    """Delete the domain, run the gate's own entry point, get the shipped copy.

    ``_ensure_seed`` is what ``start`` calls; nothing here touches the config
    files, so a passing run also proves the repair is not a side effect of the
    YAML merge path.
    """
    target = deployed / "custom_components" / DOMAIN
    for f in target.iterdir():
        f.unlink()
    target.rmdir()
    assert not target.exists()

    cli._ensure_seed()

    assert target.is_dir(), (
        "start left a deleted integration deleted: the seed gate said "
        '"satisfied" while the directory was gone'
    )
    shipped = (SHIPPED_CC / DOMAIN / "__init__.py").read_bytes()
    assert (target / "__init__.py").read_bytes() == shipped, (
        "the directory came back with something other than the shipped file"
    )


def test_a_stale_domain_is_not_rewritten(cli, deployed):
    """A matching marker means the bytes on disk win — including a user's edit.

    This is the half that must NOT change: ``_merge_file`` has no semantics for
    ``.py``, and a start that rewrote them would erase whatever the user
    changed.  Updating a component is ``install``'s job, not ``start``'s.
    """
    cli._ensure_seed()
    assert (deployed / "custom_components" / DOMAIN / "__init__.py").read_bytes() == STALE


def test_the_gate_and_the_copier_agree_on_the_domain_list(cli, deployed):
    """Both sides read the same directory, so the two lists cannot drift.

    ``_seed_ready`` learned the domain list from ``config/custom_components``;
    ``_deploy_config_files`` walks the same tree.  A hand-kept second list is how
    a new integration ends up checked for but never copied.
    """
    domains = {d.name for d in SHIPPED_CC.iterdir()
               if d.is_dir() and not d.name.startswith("_")}
    assert DOMAIN in domains, "the case's domain is not shipped any more"

    for name in domains:
        target = deployed / "custom_components" / name
        if target.is_dir():
            for f in target.iterdir():
                if f.is_file():
                    f.unlink()
            for sub in ("brand",):
                p = target / sub
                if p.is_dir():
                    for f in p.iterdir():
                        f.unlink()
                    p.rmdir()
            target.rmdir()

    cli._ensure_seed()

    for name in domains:
        assert (deployed / "custom_components" / name / "__init__.py").is_file(), (
            f"{name} was checked for but never copied back"
        )


def test_deploy_reports_each_new_domain_without_claiming_a_rewrite(
        cli, tmp_path, monkeypatch):
    """A redeploy shows every domain; an existing domain is not "installed".

    ``_install_custom_components()`` logs one line per vendored domain, so
    ``_deploy_config_files()`` must log the config/ domains too -- otherwise a
    source-tree ``--upgrade`` that removed three domains prints exactly one
    ``installed`` line and reads like it lost two integrations.

    The word "installed" is reserved for a domain that was actually created.
    Where the directory already exists, the deploy only merges YAML/JSON and
    ``_merge_file`` never rewrites a .py; claiming an install there would
    promise a component update that did not happen.  Both halves use a fresh
    HA_ROOT so neither measures a second merge of the same files.
    """
    domains = sorted(d.name for d in SHIPPED_CC.iterdir()
                     if d.is_dir() and not d.name.startswith("_"))
    assert DOMAIN in domains, "the case's domain is not shipped any more"
    lines: list[str] = []
    monkeypatch.setattr(cli, "log", lines.append)
    monkeypatch.setattr(cli, "SEED_MARKER", tmp_path / "unused.seed")

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.setattr(cli, "HA_ROOT", fresh)
    cli._deploy_config_files()
    for name in domains:
        assert f"installed custom_components/{name}" in lines, (
            f"{name} was copied by the redeploy but never reported"
        )

    lines.clear()
    existing = tmp_path / "existing"
    existing.mkdir()
    for name in domains:
        (existing / "custom_components" / name).mkdir(parents=True)
    monkeypatch.setattr(cli, "HA_ROOT", existing)
    cli._deploy_config_files()
    assert any(line.startswith("deploying config →") for line in lines)
    assert not any(line.startswith("installed custom_components/") for line in lines), (
        "a merge over existing domains reported an install: that claims a .py "
        "rewrite _merge_file does not perform"
    )
