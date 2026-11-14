"""D09 — Windows `stop` asks the core first, and only then kills it.

The defect is not that ``taskkill`` is wrong; it is that it is the *only* thing
that ever happens on Windows.  hass runs detached, so no signal can reach it,
and the old docstring read that as "there is no graceful path" — true about
signals, false about the shutdown itself.  ``homeassistant.stop`` is a service
call the core handles: shutdown jobs, integration teardown, the final .storage
write, close.  Exactly what a supervisor's SIGTERM would have done.

Both Windows stop paths are covered, because each install shape only reaches
one of them — ``_stop_pid`` for an unsupervised tree, ``SvcStop._kill_child``
for the service.  A fix in one and not the other stays green on whichever
machine happens to use the other, which is how this survived.
"""

from __future__ import annotations

import inspect


class _Result:
    """What ``subprocess.run`` would return for a successful taskkill."""

    def __init__(self, rc: int = 0) -> None:
        self.returncode = rc
        self.stdout = b""
        self.stderr = b""


def _token(tmp_path, text: str = "cached-token"):
    path = tmp_path / ".mcp-token"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_home_is_asked_before_it_is_killed(cli, monkeypatch, tmp_path):
    """A 200 from the stop service means the core is closing itself down."""
    monkeypatch.setattr(cli, "MCP_TOKEN_FILE", _token(tmp_path))
    seen: dict[str, str] = {}

    def fake_post(url, json_body=None, headers=None, timeout=10.0):
        seen["url"] = url
        seen["auth"] = (headers or {}).get("Authorization", "")
        return 200, []

    monkeypatch.setattr(cli, "_http_post", fake_post)
    monkeypatch.setattr(cli, "pid_exists", lambda pid: False)

    assert cli._win_graceful_stop(4242) == "gone"
    assert seen["url"].endswith("/api/services/homeassistant/stop"), (
        f"the stop service was never called: {seen['url']}"
    )
    assert seen["auth"] == "Bearer cached-token", (
        "the cached MCP token is what authenticates a stop; nothing may mint "
        f"credentials on the way out (got {seen['auth']!r})"
    )


def test_no_cached_token_does_not_invent_one(cli, monkeypatch, tmp_path):
    """No token means no request — a stop must never log in."""
    monkeypatch.setattr(cli, "MCP_TOKEN_FILE", tmp_path / "absent")

    def refuse(*a, **k):
        raise AssertionError("a stop path tried to reach the network without a token")

    monkeypatch.setattr(cli, "_http_post", refuse)
    assert cli._win_graceful_stop(1) == "unavailable"


def test_a_rejected_token_is_not_a_graceful_stop(cli, monkeypatch, tmp_path):
    """A 401/403 has to fall through to force, not be read as success."""
    monkeypatch.setattr(cli, "MCP_TOKEN_FILE", _token(tmp_path))
    monkeypatch.setattr(cli, "_http_post", lambda *a, **k: (401, {}))
    assert cli._win_graceful_stop(1, timeout=0.2) == "unavailable"


def test_a_core_that_ignores_the_request_times_out(cli, monkeypatch, tmp_path):
    """The window is bounded: a wedged home must still be stoppable."""
    monkeypatch.setattr(cli, "MCP_TOKEN_FILE", _token(tmp_path))
    monkeypatch.setattr(cli, "_http_post", lambda *a, **k: (200, []))
    monkeypatch.setattr(cli, "pid_exists", lambda pid: True)

    assert cli._win_graceful_stop(9001, timeout=0.3) == "timeout"


def test_stop_uses_force_only_after_the_window_expires(cli, monkeypatch, capsys):
    """Timeout ⇒ one WARN line, then taskkill.  Silence would hide the hang."""
    monkeypatch.setattr(cli, "IS_WIN", True)
    monkeypatch.setattr(cli, "_win_graceful_stop", lambda pid: "timeout")
    kills: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, *a, **k: kills.append(list(cmd)) or _Result())
    monkeypatch.setattr(cli, "pid_exists", lambda pid: False)

    assert cli._stop_pid(777) is True
    out = capsys.readouterr().out
    assert "graceful stop timed out" in out, (
        "the fallback was taken without saying why — that reads as a normal stop"
    )
    assert any("taskkill" in cmd for cmd in kills), "hass was never actually killed"


def test_an_unavailable_core_goes_straight_to_force(cli, monkeypatch, capsys):
    """No token ⇒ no warning about a window that never opened."""
    monkeypatch.setattr(cli, "IS_WIN", True)
    monkeypatch.setattr(cli, "_win_graceful_stop", lambda pid: "unavailable")
    kills: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "run",
                        lambda cmd, *a, **k: kills.append(list(cmd)) or _Result())
    monkeypatch.setattr(cli, "pid_exists", lambda pid: False)

    assert cli._stop_pid(778) is True
    out = capsys.readouterr().out
    assert "timed out" not in out
    assert any("taskkill" in cmd for cmd in kills)


def test_the_service_stop_path_asks_first_too(cli):
    """The SCM path is separate code; it must make the same call, in order.

    ``_service_host_main`` defines the service class inside the function (pywin32
    is imported there), so this is a source-level check rather than an
    instance-level one.  It is still a comparison, not an eyeball: the request
    has to appear before the taskkill it replaces.
    """
    src = inspect.getsource(cli._service_host_main)
    assert "_win_graceful_stop" in src, (
        "the service stop path never asks the core — on a machine where homeant "
        "is installed as a service, D09 is completely unfixed"
    )
    # Scoped to `_kill_child`: other code in this function legitimately mentions
    # taskkill (the reap path, and the docstring that explains it), so the
    # comparison has to start where the stop decision is made.
    kill_body = src[src.index("def _kill_child"):]
    # `["taskkill"` is the call itself; the docstring above it mentions
    # `taskkill /T` in prose, and that must not be mistaken for the call.
    assert kill_body.index("_win_graceful_stop") < kill_body.index('["taskkill"'), (
        "the service kill comes before the graceful request"
    )


def test_the_service_stop_runs_once_under_two_threads(cli):
    """Two threads enter `_kill_child`; only one of them may do the killing.

    The ordinary stop starts it twice: SvcStop from the control thread, and
    SvcDoRun's loop the moment `stopping` or the stop event is seen.  Before the
    latch the second pass asked an already-exited hass over HTTP — event log
    11-14, one second after "hass stopped gracefully": "graceful stop: request
    failed (10061)" — and then ran taskkill on a tree the first pass had just
    ended politely, i.e. a graceful stop forced by its own second hand.  It also
    cost ~1-2 s on every stop.

    Source-level for the same reason as the test above: the class is defined with
    pywin32, inside `_service_host_main`.
    """
    src = inspect.getsource(cli._service_host_main)
    kill_body = src[src.index("def _kill_child"):src.index("def SvcDoRun")]
    assert "kill_lock" in kill_body and "self.killed" in kill_body, (
        "no latch: both stop threads run the whole kill"
    )
    assert kill_body.count("_win_graceful_stop") == 1, (
        "the graceful request must appear once, under the branch that owns it"
    )
    assert "kill_done.wait(" in kill_body, (
        "the pass that does not own the kill has to wait for the one that does, "
        "otherwise SvcDoRun reports STOPPED while the tree is still coming down"
    )
