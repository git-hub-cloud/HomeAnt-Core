"""Authentication for the broker this product runs.

Two kinds of account are accepted, and nothing else:

* the broker account in Home Assistant's own mqtt config entry — the one
  ``homeant start`` provisions, the one LAN devices are pointed at, and the one
  hass itself connects with;
* any user of this Home Assistant that has a password, verified the way HA
  verifies it (bcrypt over the base64-decoded hash in
  ``.storage/auth_provider.homeassistant``).

Why one plugin instead of FileAuthPlugin plus a second one: the broker ANDs
every authentication plugin's verdict, and FileAuthPlugin answers ``False``
(not ``None``) for a username it has no hash for, so two plugins can never
express "either list".  Measured on amqtt 0.12.1 — ``broker.py``
``_authenticate`` collects the results and takes ``all()``; only ``None`` is
dropped.

Why the entry is read at connection time instead of being frozen into a
password file at start-up: HA's reconfigure form validates the new settings by
connecting to the broker *before* it saves them (``config_flow.py``
``async_validate_broker_settings``).  A broker that only knew the credentials
from start-up would reject that connection, so the password could never be
changed from the UI at all.

Paths here are relative (``.storage``) and resolved against HOMEANT_HA_ROOT,
which ``homeant.py`` sets on the broker it spawns.  mqtt.yaml ships to other
machines — an absolute path baked into it would be wrong on every one of them.

Long-lived access tokens are deliberately NOT accepted.  HA stores only a
token's id and its JWT key (``auth/models.py``: ``token = secrets.token_hex(64)``),
so accepting one means parsing the JWT, looking the id up and verifying the
signature with that key — a second dependency on HA's internals — and the JWT
is far longer than the 64-character password field a device like Tasmota has.
If that is ever wanted, this docstring is where to add it.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import bcrypt

from amqtt.plugins.base import BaseAuthPlugin
from amqtt.session import Session

# Same hash HA compares against when the username does not exist, so a failed
# login costs the same either way.  Taken from HA's own
# `auth/providers/homeassistant.py::validate_login`, which keeps it for exactly
# this reason.
_DUMMY_HASH = b"$2b$12$CiuFGszHx9eNHxPuQcwBWez4CwDTOcLTX5CbOpV6gef2nYuXkY7BO"

_STORAGE_FILES = ("core.config_entries", "auth", "auth_provider.homeassistant")


def _read_json(path: Path) -> dict[str, Any]:
    """Parsed file, or ``{}`` — a gate that cannot read its evidence refuses."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _check_bcrypt(digest: bytes, password: str) -> bool:
    """HA's own comparison, including its truncation.

    bcrypt 5.0 raises on a password longer than 72 bytes, and HA answers that by
    cutting the input to 72 before hashing *and* before checking
    (``hash_password`` / ``validate_login``).  Doing anything else here would
    disagree with HA about a password HA accepted.
    """
    try:
        return bcrypt.checkpw(password.encode()[:72], digest)
    except ValueError:
        return False


@dataclass(frozen=True)
class _Accounts:
    """One snapshot of the two stores, already resolved for lookup."""

    entry_user: str = ""
    entry_password: str = ""
    # casefolded username -> (bcrypt hash, is_active)
    users: dict[str, tuple[bytes, bool]] = field(default_factory=dict)


def _load_accounts(storage: Path) -> _Accounts:
    entry_user = ""
    entry_password = ""
    for entry in _read_json(storage / "core.config_entries").get("data", {}).get("entries") or []:
        if entry.get("domain") != "mqtt":
            continue
        data = entry.get("data") or {}
        entry_user = str(data.get("username") or "")
        entry_password = str(data.get("password") or "")
        break  # the first mqtt entry is the one `start` provisioned

    # The password provider carries only username + hash; whether that user may
    # still log in lives in the auth store, reached through the credential that
    # binds a username to a user_id.  A user with no credential, or a credential
    # whose user is gone, is treated as inactive rather than guessed at.
    auth = _read_json(storage / "auth").get("data", {}) or {}
    active = {u.get("id"): bool(u.get("is_active")) for u in auth.get("users") or []}
    bound: dict[str, str] = {}
    for cred in auth.get("credentials") or []:
        if cred.get("auth_provider_type") != "homeassistant":
            continue
        name = str((cred.get("data") or {}).get("username") or "").strip().casefold()
        if name:
            bound[name] = cred.get("user_id")

    users: dict[str, tuple[bytes, bool]] = {}
    provider = _read_json(storage / "auth_provider.homeassistant").get("data", {}) or {}
    for rec in provider.get("users") or []:
        name = str(rec.get("username") or "").strip().casefold()
        stored = rec.get("password")
        if not name or not stored:
            continue
        try:
            digest = base64.b64decode(stored, validate=True)
        except (ValueError, TypeError):
            continue
        users[name] = (digest, bool(active.get(bound.get(name))))
    return _Accounts(entry_user, entry_password, users)


class HomeAntAuthPlugin(BaseAuthPlugin):
    """Either the broker account from the mqtt entry, or an HA user."""

    def __init__(self, context: Any) -> None:
        super().__init__(context)
        base = str(self._get_config_option("storage_dir", ".storage") or ".storage")
        storage = Path(base)
        if not storage.is_absolute():
            storage = Path(os.environ.get("HOMEANT_HA_ROOT") or os.getcwd()) / storage
        self._storage = storage
        self._cache: tuple[tuple[int, ...], _Accounts] | None = None
        if not storage.is_dir():
            # Refusing everything is the right failure: an empty account table
            # would let nobody in either, but silently — and the operator would
            # read the disconnect as a broker bug.
            self.context.logger.warning(
                "homeant_mqtt_auth: %s is not a directory — every connection "
                "will be refused", storage)

    def _accounts(self) -> _Accounts:
        """Reload when a store changed; three ``stat`` calls otherwise.

        Devices reconnect often and a password change must take effect without
        restarting the broker (see the module docstring), so the file contents
        are the only truth and the cache is keyed on their mtimes.
        """
        key = tuple(_mtime(self._storage / name) for name in _STORAGE_FILES)
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, _load_accounts(self._storage))
            self.context.logger.debug(
                "homeant_mqtt_auth: reloaded %d HA user(s)", len(self._cache[1].users))
        return self._cache[1]

    async def authenticate(self, *, session: Session) -> bool:
        accounts = self._accounts()
        name = session.username or ""
        if not name:
            return False

        if accounts.entry_user and hmac.compare_digest(
                name.encode(), accounts.entry_user.encode()):
            # The account devices use: a plaintext comparison, not a hash — the
            # entry stores the password in clear and hass has to send it as-is.
            return hmac.compare_digest(
                (session.password or "").encode(), accounts.entry_password.encode())

        known = accounts.users.get(name.strip().casefold())
        if known is None:
            # Spend the same time as a real check before refusing, so response
            # timing cannot be used to enumerate usernames.
            await _in_thread(_DUMMY_HASH, session.password or "")
            return False

        digest, is_active = known
        ok = await _in_thread(digest, session.password or "")
        if not is_active:
            self.context.logger.warning(
                "homeant_mqtt_auth: user %r is disabled in Home Assistant — "
                "refusing the broker connection", name)
            return False
        return ok

    # dacite fills this from mqtt.yaml with strict=True: the key there must be
    # spelled `storage_dir` (a dash is an unexpected field and the broker would
    # refuse to start).
    @dataclass
    class Config:
        storage_dir: str = ".storage"


async def _in_thread(digest: bytes, password: str) -> bool:
    """bcrypt at rounds=12 takes ~0.2s — off the loop, or one login stalls MQTT.

    Every other connection waits behind a blocking hash, so a device retry storm
    would stop the broker from answering anything at all.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _check_bcrypt, digest, password)
