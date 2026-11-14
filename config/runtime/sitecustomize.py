"""Windows-only: give ``ProactorEventLoop`` the reader/writer hooks it lacks.

Why this file exists
--------------------
Home Assistant's MQTT client drives paho *through the event loop*: every packet
after the CONNACK is read by ``loop.add_reader(sock, _async_reader_callback)``
(``core/homeassistant/components/mqtt/client.py:683``) and a blocked write waits
on ``loop.add_writer`` (:728).  ``ProactorEventLoop`` — the default on Windows —
implements neither, so the reader is never registered.

What that looks like from outside (measured 11-15 on this machine): the CONNACK
does arrive, because the integration reads it once by hand (:688) and paho's
``connect()`` runs in an executor; the entry reports success; and then nothing
is ever read again — a retained discovery topic produced 0 entities out of 482,
``Connected to MQTT`` was never logged, and every attempt left two
``Error doing job`` tracebacks (`NotImplementedError` at :683).

Switching the whole process to ``SelectorEventLoop`` is not an option: HA core
calls ``asyncio.create_subprocess_exec`` in ~15 places (shell_command, ping,
tts, esphome, go2rtc, command_line auth) and the selector loop does not
implement it on Windows.

So this shim adds the two missing primitives to the loop *class*, in the shape
asyncio defines them on POSIX: an fd -> callback registry, one ``select()``
thread per loop, and ``call_soon_threadsafe`` to run the callback on the loop.
Measured 11-15 with a paho client wired exactly like HA's: CONNACK, SUBACK,
PUBLISH and ``on_message`` all arrive through this path.

How it is loaded
----------------
``homeant start`` puts this directory on the child's ``PYTHONPATH`` and sets
``HOMEANT_SOFT_SELECTOR=1`` (see ``_hass_child_env`` in ``scripts/homeant.py``).
Python imports ``sitecustomize`` from ``sys.path`` before any application code,
which is early enough to patch the class rather than one already-built loop.
The marker is popped *before* installing, so processes HA spawns later —
``shell_command``, ``command_line`` auth — never inherit this.

Nothing here is HomeAssistant-specific: it is asyncio plumbing, and any Windows
program that needs ``add_reader`` gets the same behaviour.  POSIX is untouched:
``install()`` refuses to run off ``win32``.
"""

from __future__ import annotations

import os
import select
import sys
import threading

MARKER = "HOMEANT_SOFT_SELECTOR"

# select() blocks this long before re-checking the close flag.  Not a poll
# interval: a readable fd wakes the thread immediately, this only bounds how
# long an idle loop takes to notice it is shutting down.
_SELECT_TIMEOUT = 0.2


def _as_fd(obj) -> int:
    """Return an int fd for a socket object or an int.

    asyncio passes whatever the caller registered (HA passes the socket), so
    both spellings have to work.
    """
    return obj if isinstance(obj, int) else obj.fileno()


class SoftSelector:
    """An fd -> callback registry driven by one background ``select()`` thread."""

    def __init__(self, loop):
        self._loop = loop
        self._readers = {}
        self._writers = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._closing = False
        self._thread = threading.Thread(
            target=self._run, name="homeant-soft-selector", daemon=True
        )
        self._thread.start()

    # ── the four asyncio primitives ────────────────────────────────────────
    def add_reader(self, fd, callback, *args) -> None:
        self._register(self._readers, fd, callback, args)

    def remove_reader(self, fd) -> bool:
        return self._unregister(self._readers, fd)

    def add_writer(self, fd, callback, *args) -> None:
        self._register(self._writers, fd, callback, args)

    def remove_writer(self, fd) -> bool:
        return self._unregister(self._writers, fd)

    def close(self) -> None:
        self._closing = True
        self._wake.set()

    # ── registry ──────────────────────────────────────────────────────────
    def _register(self, table, fd, callback, args) -> None:
        try:
            fd = _as_fd(fd)
        except (OSError, ValueError):
            return
        if fd < 0:
            return
        with self._lock:
            table[fd] = (callback, args)
        self._wake.set()

    def _unregister(self, table, fd) -> bool:
        try:
            fd = _as_fd(fd)
        except (OSError, ValueError):
            return False
        with self._lock:
            return table.pop(fd, None) is not None

    def _snapshot(self):
        with self._lock:
            return list(self._readers), list(self._writers)

    # ── the thread ────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._closing:
            if self._loop.is_closed():
                break
            rfds, wfds = self._snapshot()
            if not rfds and not wfds:
                if self._wake.wait(0.5):
                    self._wake.clear()
                continue
            try:
                ready_r, ready_w, _ = select.select(rfds, wfds, [], _SELECT_TIMEOUT)
            except (OSError, ValueError):
                # A socket closed under us is the normal case: every reconnect
                # does it.  A closed socket stays "ready"
                # forever and the next select() raises 10038, so dropping the
                # dead fd is what keeps this thread out of a busy spin.
                self._drop_dead(frozenset(rfds) | frozenset(wfds))
                continue
            for fd in ready_r:
                self._dispatch(self._readers, fd)
            for fd in ready_w:
                self._dispatch(self._writers, fd)

    def _dispatch(self, table, fd) -> None:
        with self._lock:
            item = table.get(fd)
        if item is None:
            return
        callback, args = item
        try:
            self._loop.call_soon_threadsafe(callback, *args)
        except RuntimeError:
            # Loop already closed: nothing left to deliver to.
            self._closing = True

    def _drop_dead(self, candidates) -> None:
        dead = []
        for fd in candidates:
            try:
                select.select([fd], [], [], 0)
            except (OSError, ValueError):
                dead.append(fd)
        if not dead:
            # select() failed for a reason that cannot be pinned on one fd.
            # Dropping everything is the honest answer: paho re-registers on the
            # next connect, and spinning is worse than one lost wakeup.
            dead = list(candidates)
        with self._lock:
            for fd in dead:
                self._readers.pop(fd, None)
                self._writers.pop(fd, None)


def _selector(loop) -> SoftSelector:
    sel = getattr(loop, "_homeant_soft_selector", None)
    if sel is None:
        sel = SoftSelector(loop)
        loop._homeant_soft_selector = sel
    return sel


# Module-level functions, not bound methods: they are assigned onto the loop
# class, so ``self`` is the loop instance that asyncio passes in.
def _add_reader(loop, fd, callback, *args) -> None:
    _selector(loop).add_reader(fd, callback, *args)


def _remove_reader(loop, fd) -> bool:
    return _selector(loop).remove_reader(fd)


def _add_writer(loop, fd, callback, *args) -> None:
    _selector(loop).add_writer(fd, callback, *args)


def _remove_writer(loop, fd) -> bool:
    return _selector(loop).remove_writer(fd)


def install(loop_class=None) -> bool:
    """Patch the Proactor loop class in place; True if it was patched now.

    ``loop_class`` exists for the tests: they hand in a stand-in so the registry
    logic is exercised on any platform, without touching the real asyncio class.
    """
    if loop_class is None:
        if sys.platform != "win32":
            return False
        from asyncio.windows_events import ProactorEventLoop as loop_class  # noqa: PLC0415

    if getattr(loop_class, "_homeant_soft_selector_patched", False):
        return False

    loop_class.add_reader = _add_reader
    loop_class.remove_reader = _remove_reader
    loop_class.add_writer = _add_writer
    loop_class.remove_writer = _remove_writer
    loop_class._homeant_soft_selector_patched = True
    return True


# site.py imports this module during interpreter start-up, before any
# application code, when and only when homeant put us on PYTHONPATH.  Popping
# the marker here is deliberate: grandchildren must start unpatched.
if os.environ.pop(MARKER, None) == "1":
    installed = False
    try:
        installed = install()
    except Exception:  # noqa: BLE001 - a broken shim must not break `python`
        installed = False
    if installed:
        # One line on hass's own stderr, because the alternative is a home that
        # silently reads no MQTT and no log line that says why.
        sys.stderr.write("[homeant] read shim: ProactorEventLoop patched\n")
        sys.stderr.flush()
    if log_path := os.environ.get("HOMEANT_SOFT_SELECTOR_LOG"):
        # Diagnostic switch: the shim runs before any logger exists, so a file
        # is the only place a start-up fact can land.  Off unless asked for.
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(
                    f"pid={os.getpid()} patched={installed} "
                    f"argv0={sys.argv[0]!r} exe={sys.executable!r}\n"
                )
        except OSError:
            pass
