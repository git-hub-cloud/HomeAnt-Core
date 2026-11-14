"""HomeAnt DLNA speaker bridge.

Gives an MCP client a single tool — "play audio on the default speaker" — that
accepts an http(s) URL or a local absolute path (file or directory), with no
file copying and no per-user configuration.

How the file reaches the speaker: we register our own view under
``/api/homeant_dlna/``.  The path is carried as one base64url segment (drive
letters / spaces / CJK all survive byte-for-byte), and the view is gated by
HA's own signed-URL mechanism: ``async_sign_path`` issues a 24h JWT over the
encoded path, and ``HomeAssistantView.requires_auth`` makes HA reject anything
without a valid signature.  The signature is the only credential — exactly like
``/media/`` — so the speaker, which cannot send an Authorization header, can
still fetch the file.

Two gates protect the file read:
  1. signature (no authSig -> 401, one changed byte -> 401);
  2. audio-extension allowlist (a signed path to win.ini is still refused).

The "which files" decision (directory traversal, sequential/random ordering,
multi-source splitting) lives here, not in the agent and not in the speaker:
an MCP tool's schema is static script fields (helpers/llm.py::ActionTool), so
the agent only ever passes one path string, and AVTransport only ever receives
one URI at a time.  That one string may carry a whole playlist ('|' or newline
separated); the queue holds the list, the speaker sees track 1 first.

Configuration (configuration.yaml) — optional:
    homeant_dlna:
      default_dirs:            # used only when the caller gives no path
        - /home/user/Music
      speaker: media_player.x  # explicit; else input_select/input_text pair
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
import logging
import mimetypes
from pathlib import Path
import random
import re
import socket
import time
from typing import Any
from urllib.parse import urlparse

from aiohttp import web
import voluptuous as vol

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.const import EVENT_HOMEASSISTANT_START
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)

DOMAIN = "homeant_dlna"

# Audio only.  A signed path to win.ini must still be refused, and a DLNA audio
# renderer gains nothing from serving video or an executable .svg.  Stricter
# than HA's MEDIA_MIME_TYPES (audio/video/image) on purpose.
AUDIO_EXTS: frozenset[str] = frozenset(
    {
        ".mp3", ".m4a", ".m4b", ".aac", ".flac", ".wav", ".wma", ".ogg",
        ".oga", ".opus", ".aiff", ".aif", ".alac", ".mp2", ".mpga", ".ape",
    }
)

# Signed URL lifetime — same as HA's own content URLs.  The speaker fetches
# once at play time; the long life matters only if it re-buffers.
SIGN_TTL = timedelta(hours=24)

# Script aliases — stable across machines; alias -> entity_id slug is fixed by
# scripts.yaml, same mechanism homeant_voice uses to expose its scripts.
PLAY_SCRIPT_ALIAS = "HomeAntDLNAPlay"
SELECT_SCRIPT_ALIAS = "HomeAntDLNASelect"
RESUME_SCRIPT_ALIAS = "HomeAntDLNAResume"
CANCEL_SCRIPT_ALIAS = "HomeAntDLNACancel"
ASSISTANT = "conversation"

# ── sequential playback (D27) ──────────────────────────────────────────────
# The Xiaomi renderer never emits STOPPED for a local-HTTP single track and
# just loops it, so "which track ended" is decided here from polled position,
# not from a device event.  All of this lives in memory (hass.data); a restart
# drops the queue and stops.
DEFAULT_QUEUE_TTL = 600.0      # seconds of idle before the queue is cleared
QUEUE_TTL_MIN = 30.0
QUEUE_TTL_MAX = 86400.0
POLL_INTERVAL = 2.0            # seconds between AVTransport position polls
TAIL_WATCH = 3.0               # remaining time that switches to the tail poll
TAIL_POLL_INTERVAL = 0.5       # faster poll right before the track ends
END_MARGIN = 0.5               # remaining time below this => track is over
FAIL_GRACE = 6.0               # duration still 0 after this => bad URL, skip
WRAP_DROP = 5.0                # position fell by > this => track wrapped
RESUME_SEEK_MIN = 2.0          # don't seek below this; replay from head instead
# ⛔ No "stalled position" heuristic: the renderer reports position sparsely
# (measured 0->1 then unchanged for ~6s), so a stall counter would end tracks
# early.  Track end is decided only by a real wrap (position drops) or by a
# bad URL (duration stayed 0 past FAIL_GRACE).

# Player states that mean "not currently rendering" (case-insensitive).
IDLE_STATES: frozenset[str] = frozenset(
    {"idle", "off", "paused", "unavailable", "unknown", "none", ""}
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional("default_dirs", default=list): [str],
                vol.Optional("speaker", default=""): str,
                vol.Optional("queue_ttl", default=DEFAULT_QUEUE_TTL): float,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


# ── pure helpers (no HA object needed; unit-testable in isolation) ─────────

def encode_path(path: str) -> str:
    """base64url of the UTF-8 path, without padding — one safe URL segment."""
    return base64.urlsafe_b64encode(path.encode("utf-8")).decode("ascii").rstrip("=")


def decode_path(token: str) -> str | None:
    """Inverse of encode_path; None on anything malformed.

    Re-encode-and-compare rejects non-canonical tokens (stray '=', chars
    outside the base64url alphabet) so one path has exactly one token.
    """
    if not token or re.fullmatch(r"[A-Za-z0-9_-]+", token) is None:
        return None
    try:
        padded = token + "=" * (-len(token) % 4)
        path = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    return path if encode_path(path) == token else None


def is_audio(path: str) -> bool:
    """True when the path has an audio extension we are willing to serve."""
    return Path(path).suffix.lower() in AUDIO_EXTS


def is_loopback_url(url: str) -> bool:
    """True for http(s) URLs pointing back at this machine.

    A DLNA renderer fetches the URL itself, so 127.0.0.1/localhost would
    resolve to *the speaker*, not HomeAnt.  Refuse at the tool edge.
    """
    host = (urlparse(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1", "[::1]"} or host.startswith("127.")


ROUTE_PROBE_IP = "223.5.5.5"


def _route_source_ip() -> str | None:
    """IPv4 address the OS would use toward the default route.

    ``hass.config.api.local_ip`` cannot be trusted here.  HA picks the
    "default" adapter from the mDNS multicast egress, and on a Windows host
    with Hyper-V that egress is the Default Switch (172.28.224.1).  That
    adapter then becomes the only enabled one, the real LAN adapter is left
    out, and ``async_get_source_ip()`` falls back to the virtual address — so
    every URL handed to the speaker is unroutable.  Probe the unicast default
    route instead; 223.5.5.5 is the same target homeant_mdns already uses.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        sock.connect((ROUTE_PROBE_IP, 53))
        candidate = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    try:
        addr = ip_address(candidate)
    except ValueError:
        return None
    if addr.is_loopback or addr.is_link_local or addr.is_unspecified:
        return None
    return candidate


def local_host(fallback: str) -> str:
    """Host the speaker can actually reach, else the caller's fallback."""
    return _route_source_ip() or fallback


def _dir_files(directory: Path) -> list[Path]:
    """Sorted audio files directly inside a directory (no recursion)."""
    return sorted(
        (p for p in directory.iterdir() if p.is_file() and is_audio(p.name)),
        key=lambda p: p.name,
    )


# A multi-source argument arrives as one string, because an MCP tool's schema
# is a static script field (helpers/llm.py::ActionTool) — there is no array
# field to exploit.  '|' and a newline are the two separators that can never
# appear in a legal Windows path or an unencoded http(s) URL, so splitting on
# them is unambiguous.  One source keeps the old single-source behaviour
# byte-for-byte.
SOURCE_SPLIT = re.compile(r"[|\r\n]+")


def split_sources(raw: str) -> list[str]:
    """Split a multi-source argument into its non-empty, stripped parts."""
    return [p.strip() for p in SOURCE_SPLIT.split(raw or "") if p.strip()]


def resolve_sources(
    raw: str,
    default_dirs: list[str],
    mode: str = "sequential",
) -> list[str]:
    """Resolve the tool's path argument to an ordered list of audio URLs.

    A single source is a one-element list; a directory expands to *all* its
    audio files (name-sorted for ``sequential``, shuffled for ``random``).
    ``http(s)`` passes through unchanged.  A multi-source argument (``|`` or
    newline separated) concatenates each part's expansion in the order given,
    so the agent can hand over a whole playlist in one call; parts that
    resolve to nothing are dropped silently.  Empty means "play music" with no
    target: scan ``default_dirs`` in order and expand the first directory
    containing audio.  The caller feeds the whole list to the queue.
    """
    parts = split_sources(raw)
    if len(parts) > 1:
        urls: list[str] = []
        for part in parts:
            # Each part resolves on its own: a bad URL must not swallow the
            # rest, and a directory in the middle keeps its own expansion.
            urls.extend(resolve_sources(part, [], mode))
        return urls

    if not parts:
        for d in default_dirs:
            p = Path(d).expanduser()
            if p.is_dir():
                found = resolve_sources(str(p), [], mode)
                if found:
                    return found
        return []

    raw = parts[0]
    path = Path(raw).expanduser()
    if path.is_file():
        return [str(path)]
    if not path.is_dir():
        scheme = urlparse(raw).scheme
        return [raw] if scheme in {"http", "https"} else []

    files = _dir_files(path)
    if not files:
        return []
    if mode == "random":
        random.shuffle(files)
    return [str(f) for f in files]


# ── sequential queue state machine (pure; no HA object) ───────────────────

def secs_left(snap: dict[str, Any], now: float) -> float | None:
    """Wall-clock seconds left in the current track, or None when unknown.

    ``media_position`` on its own is stale: measured, HA refreshes it only
    about every 10s, so ``duration - position`` alone can place the end of a
    track anywhere inside a 10s window.  Pairing the reading with the moment
    HA captured it (``media_position_updated_at``) turns that into an
    absolute end time — the elapsed-since-capture term keeps decaying in real
    time even while the cached position sits still.

    ``now`` is a wall-clock epoch (``time.time()``), not ``monotonic()``,
    because the anchor is an absolute timestamp.
    """
    dur = snap.get("duration")
    pos = snap.get("position")
    upd = snap.get("updated_at")
    if not isinstance(dur, (int, float)) or dur <= 0:
        return None
    if not isinstance(pos, (int, float)):
        return None
    if not isinstance(upd, datetime):
        return None
    if upd.tzinfo is None:
        upd = upd.replace(tzinfo=timezone.utc)
    age = max(0.0, now - upd.timestamp())
    return float(dur) - float(pos) - age


@dataclass
class QueueState:
    """In-memory sequential playlist for one speaker.

    ``urls`` are the audio URLs in play order (already validated).  The state
    machine is fed player snapshots via :meth:`observe`, which returns one of
    the action strings below; all HA I/O stays in the caller.  Keeping this
    pure makes every branch unit-testable without HA or a speaker.
    """

    urls: list[str]
    speaker: str
    ttl: float = DEFAULT_QUEUE_TTL
    idx: int = 0
    phase: str = "playing"          # playing | idle
    last_pos: float | None = None   # last polled position (for wrap detection)
    pushed_at: float = 0.0          # monotonic time the current URL was pushed
    idle_since: float | None = None
    paused_pos: float | None = None  # position captured when it went idle

    # actions returned by observe()
    NONE = "none"       # keep polling, nothing to do
    NEXT = "next"       # current track ended -> push idx+1
    STOP = "stop"       # last track ended -> media_stop, drop the queue
    IDLE = "idle"       # playback stopped externally -> remember for resume
    EXPIRE = "expire"   # idle longer than ttl -> drop the queue

    @property
    def current(self) -> str:
        return self.urls[self.idx]

    @property
    def is_last(self) -> bool:
        return self.idx >= len(self.urls) - 1

    def mark_pushed(self, now: float) -> None:
        """Call right after pushing ``current`` (initial or next/resume)."""
        self.pushed_at = now
        self.last_pos = None
        self.phase = "playing"
        self.idle_since = None
        self.paused_pos = None

    def observe(
        self, snap: dict[str, Any], now: float, left: float | None = None
    ) -> str:
        """Feed a player snapshot; return the single action to take.

        ``snap`` keys: ``state`` (str), ``position`` (float|None),
        ``duration`` (float|None).  ``left`` is the wall-clock seconds
        remaining (:func:`secs_left`), when it can be computed.  Order of
        checks matters: idle/expire first so a stopped renderer is never
        mistaken for a wrapped track; then a failed URL (duration==0); then
        the wall-clock end; then a real position wrap.
        """
        state = str(snap.get("state") or "").strip().lower()

        if self.phase == "idle":
            if self.idle_since is not None and now - self.idle_since >= self.ttl:
                return self.EXPIRE
            # While idle a spontaneous play (agent resume, app, button) just
            # becomes the new "now" baseline; no track transition.
            if state not in IDLE_STATES:
                self.mark_pushed(now)
            return self.NONE

        if state in IDLE_STATES:
            # External/HA stop: remember where we were, start the TTL clock.
            self.phase = "idle"
            self.idle_since = now
            self.paused_pos = self.last_pos
            return self.IDLE

        duration = snap.get("duration")
        dur = float(duration) if isinstance(duration, (int, float)) else 0.0
        # A signed/expired or copyright-blocked URL: renderer fetched once,
        # state says playing, but duration never becomes real.  Skip it.
        if dur <= 0 and now - self.pushed_at >= FAIL_GRACE:
            return self.NEXT if not self.is_last else self.STOP

        # Wall-clock end: the renderer loops a finished track instead of
        # reporting idle, so stopping a fraction *before* it wraps is the only
        # way to avoid a second of the next loop being audible.  Only trusted
        # while actually playing (a paused position keeps its reading but the
        # clock runs on, which would fake an end) and only when duration is
        # real -- local files have reported a wrong duration, and there the
        # wrap rule below still catches the end.
        if left is not None and left <= END_MARGIN:
            return self.NEXT if not self.is_last else self.STOP

        position = snap.get("position")
        pos = float(position) if isinstance(position, (int, float)) else None
        if pos is not None:
            if self.last_pos is not None and pos + WRAP_DROP < self.last_pos:
                # Real wrap to the beginning => this track finished.
                return self.NEXT if not self.is_last else self.STOP
            self.last_pos = pos
        return self.NONE

    def resume_target(self) -> float | None:
        """Where to seek on resume; None means replay from the head.

        Too-small positions (or a fresh push with no reading yet) aren't worth
        a seek — a head replay reaches that point faster and is more robust.
        """
        pos = self.paused_pos
        if isinstance(pos, (int, float)) and float(pos) >= RESUME_SEEK_MIN:
            return float(pos)
        return None

    def advance(self, now: float) -> bool:
        """Move to the next track; False when there was none (caller stops)."""
        if self.is_last:
            return False
        self.idx += 1
        self.mark_pushed(now)
        return True


# ── view ───────────────────────────────────────────────────────────────────

class DlnaFileView(HomeAssistantView):
    """Serve one signed audio file to a DLNA renderer."""

    url = "/api/homeant_dlna/file/{token}"
    name = "api:homeant_dlna:file"
    requires_auth = True  # HA validates authSig before calling get()

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        path = decode_path(token)
        if path is None or not is_audio(path):
            raise web.HTTPNotFound()
        file = Path(path)
        if not file.is_file():
            raise web.HTTPNotFound()
        mime = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        return web.FileResponse(file, headers={"Content-Type": mime})


# ── setup ──────────────────────────────────────────────────────────────────

async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the DLNA bridge: view, services, script exposure."""
    conf = config.get(DOMAIN) or {}
    default_dirs: list[str] = list(conf.get("default_dirs") or [])
    configured_speaker: str = conf.get("speaker") or ""

    hass.http.register_view(DlnaFileView())

    def _speaker() -> str:
        if configured_speaker:
            return configured_speaker
        for entity_id in (
            "input_select.homeant_dlna_speaker",
            "input_text.homeant_dlna_speaker",
        ):
            st = hass.states.get(entity_id)
            if st and st.state not in ("unset", "unknown", "unavailable", "none", ""):
                return st.state
        return ""

    def _media_id(chosen: str) -> str:
        if chosen.startswith(("http://", "https://")):
            return chosen
        signed = async_sign_path(
            hass,
            f"/api/homeant_dlna/file/{encode_path(chosen)}",
            SIGN_TTL,
            use_content_user=True,
        )
        api = hass.config.api
        base = local_host(api.local_ip if api else "127.0.0.1")
        port = api.port if api else 8123
        return f"http://{base}:{port}{signed}"

    async def play(call) -> dict[str, str]:
        raw = (call.data.get("path") or "").strip()
        mode = (call.data.get("mode") or "sequential").strip()
        if mode not in ("sequential", "random"):
            mode = "sequential"
        # Path resolution stats/iterates the filesystem, so it runs in the
        # executor: doing it inline trips HA's blocking-call detector (scandir)
        # and stalls the event loop on a large or slow directory.
        urls = await hass.async_add_executor_job(
            resolve_sources, raw, default_dirs, mode
        )
        if not urls:
            return {
                "status": "no_file",
                "message": "没有可播放的音频。传 http(s) 地址、音频文件绝对路径或含音频的文件夹；未指定时需配置默认音乐目录。",
            }
        if any(is_loopback_url(u) for u in urls):
            return {
                "status": "no_file",
                "message": "回环地址音箱无法访问。请给局域网可达的 http(s) 地址或本地文件路径。",
            }

        speaker = (call.data.get("speaker") or "").strip() or _speaker()
        if not speaker:
            return {
                "status": "no_speaker",
                "message": "尚未设置默认 DLNA 音箱。调用 HomeAntDLNASelect 选择一台后再播。",
            }

        # play is the only entry point: a single source becomes a one-element
        # queue, a directory becomes the whole expanded list, and a
        # '|'/newline separated argument becomes every source's expansion in
        # order.  Same poll loop, same wrap detection, same end-of-track Stop
        # (D27).  The Xiaomi renderer loops a finished HTTP track forever and
        # never reports idle on its own, so only the queue's wrap rule can end it.
        err = await _begin_queue(urls, speaker, queue_ttl)
        if err:
            return {"status": "error", "message": f"播放失败：{err}"}
        return {
            "status": "ok",
            "message": f"已排入 {len(urls)} 首并开始播放；播完自动切下一首，末首后停止。",
        }

    hass.services.async_register(DOMAIN, "play", play, supports_response=True)

    # ── sequential queue: all state in memory, gone on restart ─────────────
    queue_ttl: float = conf.get("queue_ttl", DEFAULT_QUEUE_TTL)
    if not isinstance(queue_ttl, (int, float)) or not (
        QUEUE_TTL_MIN <= float(queue_ttl) <= QUEUE_TTL_MAX
    ):
        queue_ttl = DEFAULT_QUEUE_TTL
    queue_ttl = float(queue_ttl)

    queue: QueueState | None = None
    poll_task: asyncio.Task | None = None

    def _snapshot(speaker: str) -> dict[str, Any]:
        st = hass.states.get(speaker)
        if st is None:
            return {
                "state": "unavailable", "position": None,
                "duration": None, "updated_at": None,
            }
        return {
            "state": st.state,
            "position": st.attributes.get("media_position"),
            "duration": st.attributes.get("media_duration"),
            # When HA captured that position — secs_left needs it to turn a
            # ~10s-stale reading into an absolute end time.
            "updated_at": st.attributes.get("media_position_updated_at"),
        }

    async def _refresh(speaker: str) -> None:
        """Force HA to re-read the renderer instead of using its cache.

        Measured at 24-52ms per call; only used inside the tail window above,
        where the cached position is too coarse to stop before the loop.
        """
        await hass.services.async_call(
            "homeassistant", "update_entity", {"entity_id": speaker},
            blocking=True,
        )

    async def _push(speaker: str, url: str) -> None:
        await hass.services.async_call(
            "media_player", "play_media",
            {
                "entity_id": speaker,
                "media_content_type": "music",
                "media_content_id": _media_id(url),
            },
            blocking=True,
        )

    async def _stop(speaker: str) -> None:
        await hass.services.async_call(
            "media_player", "media_stop", {"entity_id": speaker}, blocking=True,
        )

    async def _seek(speaker: str, position: float) -> None:
        await hass.services.async_call(
            "media_player", "media_seek",
            {"entity_id": speaker, "seek_position": position},
            blocking=True,
        )

    async def _poll_loop() -> None:
        """Poll the renderer and drive the queue.  Exits when queue is cleared.

        Normal cadence is POLL_INTERVAL and costs nothing — it only reads the
        state HA already holds.  Once the wall clock says the track is within
        TAIL_WATCH of its end, the cadence tightens and every tick forces a
        refresh first: the cached position is ~10s stale, fine for noticing a
        pause, far too coarse to stop *before* the renderer loops the track.
        """
        nonlocal queue
        while queue is not None:
            q = queue
            tail = False
            try:
                snap = _snapshot(q.speaker)
                left = secs_left(snap, time.time())
                if left is not None and left <= TAIL_WATCH:
                    tail = True
                    try:
                        await _refresh(q.speaker)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning("DLNA queue: refresh failed: %s", err)
                    else:
                        snap = _snapshot(q.speaker)
                    left = secs_left(snap, time.time())

                now = time.monotonic()
                action = q.observe(snap, now, left)
                if action == QueueState.NEXT:
                    if q.advance(now):
                        _LOGGER.warning(
                            "DLNA queue: track %d/%d wrapped -> next",
                            q.idx, len(q.urls),
                        )
                        await _push(q.speaker, q.current)
                elif action == QueueState.STOP:
                    _LOGGER.warning("DLNA queue: last track ended -> stop")
                    try:
                        await _stop(q.speaker)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning("DLNA queue: stop failed: %s", err)
                    queue = None
                elif action == QueueState.IDLE:
                    _LOGGER.warning(
                        "DLNA queue: stopped externally, resume available for %.0fs",
                        q.ttl,
                    )
                elif action == QueueState.EXPIRE:
                    _LOGGER.warning("DLNA queue: idle TTL expired -> drop")
                    queue = None
            except Exception as err:  # noqa: BLE001 - never let the loop die
                _LOGGER.warning("DLNA queue poll error: %s", err)
            await asyncio.sleep(TAIL_POLL_INTERVAL if tail else POLL_INTERVAL)

    def _start_poll() -> None:
        nonlocal poll_task
        if poll_task is None or poll_task.done():
            poll_task = hass.async_create_task(_poll_loop())

    async def _begin_queue(
        urls: list[str], speaker: str, ttl: float
    ) -> str | None:
        """Build a queue, push its first track, start polling.

        ``play`` is the only caller: a single source yields a one-element
        list, a directory the whole expanded list, a multi-source argument
        every part's expansion.  Returns an error message, or None on success.
        A failed first push leaves no queue.
        """
        nonlocal queue
        q = QueueState(urls=urls, speaker=speaker, ttl=ttl)
        q.mark_pushed(time.monotonic())
        queue = q
        try:
            await _push(speaker, q.current)
        except Exception as err:  # noqa: BLE001
            queue = None
            return str(err)
        _start_poll()
        return None

    async def resume(call) -> dict[str, str]:
        """Continue a stopped/idle queue: seek to the pause point or replay."""
        nonlocal queue
        q = queue
        if q is None:
            return {
                "status": "no_queue",
                "message": "没有待续播的队列。先调用 HomeAntDLNAPlay 排入列表。",
            }
        now = time.monotonic()
        if q.phase == "idle" and q.idle_since is not None and now - q.idle_since >= q.ttl:
            queue = None
            return {"status": "expired", "message": "已超过空闲时限，队列已清理。请重新排入。"}
        if q.phase != "idle":
            return {"status": "playing", "message": "当前曲目仍在播放，无需续播。"}
        target = q.resume_target()
        try:
            if target is not None:
                # Measured: renderer accepts Seek REL_TIME while playing.  A
                # stopped renderer needs Play first, then Seek to the point.
                await hass.services.async_call(
                    "media_player", "media_play", {"entity_id": q.speaker}, blocking=True,
                )
                await _seek(q.speaker, target)
                msg = f"已从第 {target:.0f} 秒续播。"
            else:
                # Too near the head (or no position reading): replay from start.
                await _push(q.speaker, q.current)
                msg = "已从当前曲目开头续播。"
        except Exception as err:  # noqa: BLE001 - seek unsupported -> replay head
            _LOGGER.warning("DLNA resume seek failed, replay from head: %s", err)
            try:
                await _push(q.speaker, q.current)
                msg = "续播定位失败，已从当前曲目开头播放。"
            except Exception as err2:  # noqa: BLE001
                return {"status": "error", "message": f"续播失败：{err2}"}
        q.mark_pushed(now)
        _start_poll()
        return {"status": "ok", "message": msg}

    async def cancel(call) -> dict[str, str]:
        """Stop and drop the in-memory queue."""
        nonlocal queue
        q = queue
        queue = None
        if q is None:
            return {"status": "no_queue", "message": "没有可取消的队列。"}
        try:
            await _stop(q.speaker)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("DLNA queue cancel stop failed: %s", err)
        return {"status": "ok", "message": "已停止播放并清空播放队列。"}

    hass.services.async_register(DOMAIN, "resume", resume, supports_response=True)
    hass.services.async_register(DOMAIN, "cancel", cancel, supports_response=True)

    @callback
    def _expose_scripts() -> None:
        # Imported here so the module stays importable in isolated unit tests.
        from homeassistant.components.homeassistant import async_should_expose
        from homeassistant.components.homeassistant.exposed_entities import (
            async_expose_entity,
        )

        for entry in er.async_get(hass).entities.values():
            if entry.domain == "script" and entry.original_name in (
                PLAY_SCRIPT_ALIAS, SELECT_SCRIPT_ALIAS, RESUME_SCRIPT_ALIAS,
                CANCEL_SCRIPT_ALIAS,
            ):
                async_expose_entity(hass, ASSISTANT, entry.entity_id, True)
                if not async_should_expose(hass, ASSISTANT, entry.entity_id):
                    _LOGGER.warning("Failed to expose DLNA script %s", entry.entity_id)

    @callback
    def _on_start(_event) -> None:
        _expose_scripts()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _on_start)
    return True
