"""The broker's account table: HA's mqtt entry plus HA's own users.

The plugin is the only thing standing between 1883 and every device on the LAN,
so what it accepts is a security contract, not a feature.  Four claims are
asserted here:

* the account in HA's mqtt config entry works, and its exact password is the
  only one that does;
* an HA user is verified the way HA verifies it — base64-decoded bcrypt over the
  first 72 bytes, so a password HA accepted is a password this accepts;
* a user that is disabled in HA, or that has no password at all, is refused;
* the account table follows the files (mtime), so changing the password in the
  UI needs no broker restart — which is the whole reason the entry is read per
  connection instead of frozen into a password file at start-up.

``amqtt`` is stubbed rather than imported: the gate runs in a throwaway
environment (see ``verify.pytest_cmd``) and pulling amqtt's dependency tree in
just to get one base class would make the gate slower and network-dependent.
``bcrypt`` is a real dependency of the test, because the thing most likely to
break is the *encoding* HA uses, and a fake hash verifies nothing.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import bcrypt
import pytest

from gate_paths import REPO_ROOT

PLUGIN_PATH = REPO_ROOT / "config" / "runtime" / "homeant_mqtt_auth.py"


# ── amqtt, reduced to the two names the plugin imports ──────────────────────


def _install_amqtt_stub() -> None:
    if "amqtt.plugins.base" in sys.modules:
        return

    pkg = types.ModuleType("amqtt")
    pkg.__path__ = []                                   # type: ignore[attr-defined]
    plugins = types.ModuleType("amqtt.plugins")
    plugins.__path__ = []                               # type: ignore[attr-defined]
    base = types.ModuleType("amqtt.plugins.base")

    class BaseAuthPlugin:
        """The slice of amqtt's base class this plugin actually uses."""

        def __init__(self, context: object) -> None:
            self.context = context

        def _get_config_option(self, name: str, default: object = None) -> object:
            return getattr(self.context.config, name.replace("-", "_"), default)

        async def authenticate(self, *, session: object) -> bool:
            return True

    base.BaseAuthPlugin = BaseAuthPlugin

    session_mod = types.ModuleType("amqtt.session")

    class Session:                                     # pragma: no cover - typing only
        pass

    session_mod.Session = Session

    sys.modules["amqtt"] = pkg
    sys.modules["amqtt.plugins"] = plugins
    sys.modules["amqtt.plugins.base"] = base
    sys.modules["amqtt.session"] = session_mod


def _load_plugin() -> types.ModuleType:
    _install_amqtt_stub()
    spec = importlib.util.spec_from_file_location("homeant_mqtt_auth_under_test",
                                                  PLUGIN_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: dataclasses resolves annotations through
    # sys.modules[cls.__module__], and an unregistered module makes that None.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def auth() -> types.ModuleType:
    return _load_plugin()


# ── a Home Assistant .storage tree, small enough to read ────────────────────


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def debug(self, *a: object, **k: object) -> None:
        pass

    def info(self, *a: object, **k: object) -> None:
        pass

    def warning(self, msg: str, *a: object, **k: object) -> None:
        self.warnings.append(msg % a if a else msg)


class _Context:
    def __init__(self, storage_dir: str) -> None:
        self.config = types.SimpleNamespace(storage_dir=storage_dir)
        self.logger = _Logger()


def _hash(password: str, *, rounds: int = 4) -> str:
    """What HA's provider stores: base64 of a bcrypt hash, cut to 72 bytes."""
    raw = bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt(rounds=rounds))
    return base64.b64encode(raw).decode()


@dataclass
class _User:
    name: str
    password: str | None
    active: bool = True


@dataclass
class _Tree:
    root: Path
    entry_user: str = ""
    entry_password: str = ""

    def rewrite(self, *, entry_user: str = "", entry_password: str = "",
                users: list[_User] | None = None) -> None:
        """Write all three stores, the way HA would have them on disk."""
        users = users if users is not None else []
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "core.config_entries").write_text(json.dumps({
            "version": 1,
            "data": {"entries": [{
                "domain": "mqtt",
                "data": {"username": entry_user, "password": entry_password},
            }]},
        }), encoding="utf-8")

        auth_users = []
        credentials = []
        provider_users = []
        for i, user in enumerate(users, start=1):
            uid = f"user{i}"
            auth_users.append({"id": uid, "is_active": user.active,
                               "name": user.name})
            if user.password is None:
                # A user that only exists in another provider: no credential in
                # the password provider, so nothing to log in with here.
                continue
            credentials.append({
                "id": f"cred{i}",
                "user_id": uid,
                "auth_provider_type": "homeassistant",
                "auth_provider_id": None,
                "data": {"username": user.name},
            })
            provider_users.append({"username": user.name,
                                   "password": _hash(user.password)})
        (self.root / "auth").write_text(json.dumps({
            "version": 1, "data": {"users": auth_users, "groups": [],
                                   "credentials": credentials,
                                   "refresh_tokens": []},
        }), encoding="utf-8")
        (self.root / "auth_provider.homeassistant").write_text(json.dumps({
            "version": 1, "data": {"users": provider_users},
        }), encoding="utf-8")


@pytest.fixture()
def tree(tmp_path: Path) -> _Tree:
    t = _Tree(root=tmp_path / ".storage")
    t.rewrite(entry_user="mqtt", entry_password="s3cret",
              users=[_User("admin", "admin-pw")])
    return t


def _ask(auth_module: types.ModuleType, storage: Path, *,
         username: str, password: str) -> bool:
    plugin = auth_module.HomeAntAuthPlugin(_Context(str(storage)))
    session = types.SimpleNamespace(username=username, password=password)
    return asyncio.run(plugin.authenticate(session=session))


# ── the account every device uses ───────────────────────────────────────────


def test_the_entry_account_is_accepted(auth, tree):
    assert _ask(auth, tree.root, username="mqtt", password="s3cret") is True


def test_the_entry_account_needs_the_exact_password(auth, tree):
    assert _ask(auth, tree.root, username="mqtt", password="s3cret ") is False
    assert _ask(auth, tree.root, username="mqtt", password="") is False


def test_an_empty_entry_account_does_not_match_an_empty_login(auth, tree):
    """A provisioned-but-empty entry must not become anonymous access."""
    tree.rewrite(entry_user="", entry_password="", users=[])
    assert _ask(auth, tree.root, username="", password="") is False


# ── HA's own users ──────────────────────────────────────────────────────────


def test_an_ha_user_is_verified_the_way_ha_stores_it(auth, tree):
    """base64 → bcrypt, exactly HA's encoding (provider: ``hash_password``)."""
    assert _ask(auth, tree.root, username="admin", password="admin-pw") is True
    assert _ask(auth, tree.root, username="admin", password="admin") is False


def test_the_username_is_normalised_like_ha_does(auth, tree):
    """HA compares ``strip().casefold()`` — a device typing `Admin` works."""
    assert _ask(auth, tree.root, username="  ADMIN ", password="admin-pw") is True


def test_a_long_password_is_cut_at_72_bytes_like_ha_does(auth, tree):
    """HA hashes ``password[:72]``; anything else disagrees with HA itself."""
    long_pw = "p" * 100
    tree.rewrite(entry_user="mqtt", entry_password="s3cret",
                 users=[_User("long", None)])
    # Write the provider by hand: _hash already truncates, and this test is
    # about the *check* side agreeing with it.
    (tree.root / "auth_provider.homeassistant").write_text(json.dumps({
        "version": 1, "data": {"users": [{"username": "long",
                                          "password": _hash(long_pw)}]},
    }), encoding="utf-8")
    (tree.root / "auth").write_text(json.dumps({
        "version": 1, "data": {
            "users": [{"id": "u1", "is_active": True, "name": "long"}],
            "groups": [],
            "credentials": [{"id": "c1", "user_id": "u1",
                             "auth_provider_type": "homeassistant",
                             "auth_provider_id": None,
                             "data": {"username": "long"}}],
            "refresh_tokens": [],
        },
    }), encoding="utf-8")
    assert _ask(auth, tree.root, username="long", password=long_pw) is True
    # bcrypt only ever looked at the first 72 bytes, so the tail is irrelevant —
    # the same field is what makes this agree with HA rather than merely work.
    assert _ask(auth, tree.root, username="long", password="p" * 72 + "x") is True


def test_a_disabled_user_is_refused_even_with_the_right_password(auth, tree):
    tree.rewrite(entry_user="mqtt", entry_password="s3cret",
                 users=[_User("gone", "gone-pw", active=False)])
    assert _ask(auth, tree.root, username="gone", password="gone-pw") is False


def test_a_user_without_a_password_cannot_log_in(auth, tree):
    """Users from another provider have no credential here — no way in."""
    tree.rewrite(entry_user="mqtt", entry_password="s3cret",
                 users=[_User("token-only", None)])
    assert _ask(auth, tree.root, username="token-only", password="anything") is False


def test_an_unknown_user_is_refused(auth, tree):
    assert _ask(auth, tree.root, username="nobody", password="s3cret") is False


# ── the table follows the files, not the process ────────────────────────────


def test_a_changed_password_takes_effect_without_a_restart(auth, tree):
    """The UI changes the entry; the broker picks it up on the next connect.

    This is the reason the account is read per connection: HA's reconfigure form
    connects with the new credentials *before* saving, so a start-up snapshot
    would make the change impossible to make at all.
    """
    plugin = auth.HomeAntAuthPlugin(_Context(str(tree.root)))
    old = types.SimpleNamespace(username="mqtt", password="s3cret")
    assert asyncio.run(plugin.authenticate(session=old)) is True

    tree.rewrite(entry_user="mqtt", entry_password="rotated",
                 users=[_User("admin", "admin-pw")])
    new = types.SimpleNamespace(username="mqtt", password="rotated")
    assert asyncio.run(plugin.authenticate(session=new)) is True
    stale = types.SimpleNamespace(username="mqtt", password="s3cret")
    assert asyncio.run(plugin.authenticate(session=stale)) is False


def test_a_missing_storage_tree_refuses_everything(auth, tmp_path):
    """Fail closed: no table means no access, never "no authentication"."""
    plugin = auth.HomeAntAuthPlugin(_Context(str(tmp_path / "absent")))
    session = types.SimpleNamespace(username="mqtt", password="")
    assert asyncio.run(plugin.authenticate(session=session)) is False
    assert plugin.context.logger.warnings, "a missing store must say so in the log"


def test_a_relative_storage_dir_follows_the_root_the_spawner_set(auth, tree,
                                                                monkeypatch):
    """mqtt.yaml ships to other machines, so it cannot carry an absolute path."""
    monkeypatch.setenv("HOMEANT_HA_ROOT", str(tree.root.parent))
    assert _ask(auth, Path(".storage"), username="mqtt", password="s3cret") is True


# ── what homeant.py hands the broker ────────────────────────────────────────


def test_mqtt_yaml_mounts_one_plugin_and_no_absolute_path(cli):
    """AND semantics + fail-closed: exactly one plugin, and a portable path.

    amqtt ANDs every authentication plugin and FileAuthPlugin answers ``False``
    (not ``None``) for an unknown username, so a second plugin could never
    express "either list" — mounting both would lock every user out.
    """
    text = cli._mqtt_conf_text()
    assert "homeant_mqtt_auth.HomeAntAuthPlugin:" in text
    assert "FileAuthPlugin" not in text
    assert "storage_dir: .storage" in text
    # The portability claim, asserted rather than assumed: no absolute path.
    assert str(cli.HA_ROOT) not in text
    assert str(REPO_ROOT) not in text
    assert ":" not in text.split("storage_dir:")[1].splitlines()[0], (
        "storage_dir must not carry a drive letter or an absolute path"
    )


def test_broker_child_env_puts_the_plugin_on_the_path(cli, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["/existing"]))
    env = cli._broker_child_env()
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(cli._RUNTIME_DIR)
    assert parts[1] == "/existing"
    assert env["HOMEANT_HA_ROOT"] == str(cli.HA_ROOT)
    assert (Path(parts[0]) / "homeant_mqtt_auth.py").is_file()


def test_the_plugin_ships_with_the_program_tree(cli):
    """config/ is inside the release zip, and runtime/ is not deployed anywhere.

    A plugin only in the repo would work here and fail on every installed box:
    the broker imports it through PYTHONPATH pointing at the *program* tree.
    """
    assert (cli._RUNTIME_DIR / "homeant_mqtt_auth.py").is_file()
    pack = (REPO_ROOT / "scripts" / "dist-pack.py").read_text(encoding="utf-8")
    assert '"config/",' in pack, "config/ is no longer a zip include path"
