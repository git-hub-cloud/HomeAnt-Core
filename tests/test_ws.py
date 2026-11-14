"""D05 — ``_WSConn.recv`` must see a closed socket and understand control frames.

Three failures, one root cause: the reader treated the stream as "bytes until a
JSON object appears", so anything that is not a text frame either hangs it or
crashes it.

* ``recv()`` returning ``b""`` is EOF.  Appending it to the buffer does not
  change the length, so every ``while len(buf) < …`` loop spins at 100% CPU for
  as long as the caller waits — this is what ``status``/``start`` hit whenever
  hass restarts while they are talking to it.
* a close frame (``0x8``) leaves a zero-length body, so the JSON parse gets
  ``b""`` and raises ``JSONDecodeError`` — a truthy-looking crash from a
  perfectly normal shutdown.
* a ping (``0x9``) is the peer asking for a pong; unread, the peer eventually
  drops the connection.  Parsed as JSON, it raises the same way.

The EOF case runs in a subprocess on purpose: a busy loop cannot be judged
in-process without a thread that keeps burning a core after the test ends, and
``subprocess.run(timeout=…)`` turns "still spinning after 10s" into an exit code
and a killed child.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

GATE_DIR = Path(__file__).resolve().parent


class _Sock:
    """A socket whose reads come from a script, and which records writes."""

    def __init__(self, *chunks: bytes) -> None:
        self._chunks = list(chunks)
        self.sent = b""

    def recv(self, _n: int) -> bytes:
        assert self._chunks, "recv() called after the script ran out of chunks"
        return self._chunks.pop(0)

    def sendall(self, data: bytes) -> None:
        self.sent += data


def _text_frame(payload: bytes) -> bytes:
    assert len(payload) < 126, "the helper only builds short frames"
    return bytes([0x81, len(payload)]) + payload


def test_close_frame_is_a_closed_connection(cli):
    ws = cli._WSConn(_Sock(b"\x88\x00"))
    with pytest.raises(ConnectionError):
        ws.recv()


def test_ping_is_answered_and_the_next_frame_is_returned(cli):
    body = b'{"type": "auth_required"}'
    sock = _Sock(b"\x89\x00", _text_frame(body))
    ws = cli._WSConn(sock)

    frame = ws.recv()

    assert frame == {"type": "auth_required"}
    assert sock.sent[:2] == b"\x8a\x80", (
        f"ping was not answered with a masked pong: sent {sock.sent[:4]!r}"
    )


def test_a_frame_split_across_reads_still_parses(cli):
    """Regression guard for the reader rewrite: the header may arrive in pieces."""
    frame = _text_frame(b'{"type": "result", "success": true}')
    ws = cli._WSConn(_Sock(*[frame[i:i + 1] for i in range(len(frame))]))
    assert ws.recv() == {"type": "result", "success": True}


def test_eof_raises_instead_of_spinning(cli):
    """A closed socket must end the read, not become a hot loop."""
    cli_path = Path(cli.__file__).resolve()
    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(GATE_DIR)!r})\n"
        f"sys.path.insert(0, {str(cli_path.parent)!r})\n"
        "from gate_cli import cli\n"
        "class Sock:\n"
        "    def recv(self, n): return b''\n"
        "    def sendall(self, data): pass\n"
        "try:\n"
        "    cli()._WSConn(Sock()).recv()\n"
        "except ConnectionError:\n"
        "    print('CLOSED'); raise SystemExit(0)\n"
        "except BaseException as exc:\n"
        "    print('WRONG', type(exc).__name__); raise SystemExit(3)\n"
        "print('RETURNED'); raise SystemExit(4)\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(
            "recv() never returned on a closed socket: b'' is appended to the "
            "buffer, the length never grows, and the loop spins at 100% CPU"
        ) from None

    out = (proc.stdout or "").strip()
    assert proc.returncode == 0 and out == "CLOSED", (
        f"EOF handling is wrong (rc={proc.returncode}, out={out!r}, "
        f"err={(proc.stderr or '').strip()[-300:]!r})"
    )
