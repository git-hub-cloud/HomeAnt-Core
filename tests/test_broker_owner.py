"""D23 — the broker belongs to the supervised process, not to a short-lived CLI.

C9 was never a wrong kill command.  ``cmd_start`` used to spawn the broker
itself and then hand hass to the supervisor, so the broker's parent was a
command that exits seconds later: nothing owned the listener, ``stop`` had to
guess with a pid file that names a trampoline (C2), and 1883 stayed bound.

The fix has two halves and this case pins both:

* **owner** — under a supervisor only ``start --foreground`` spawns the broker.
  That process lives exactly as long as the home, and it already carries the
  "whoever starts it stops it" rule at its exit.
* **crash** — ``stop`` runs code; a crash does not.  A Job Object with
  ``KILL_ON_JOB_CLOSE`` moves that last case into the kernel: the supervisor
  holds one handle, the foreground process holds a nested one, and dropping
  either takes the children down.  The handle has to live at module level —
  a local dies with its frame, which would kill hass and the broker on the way
  out of the very function that armed it.

Nesting and the outer→inner teardown were measured on this box (Windows 10,
pywin32 ``win32job``): assigning an already-jobbing process to a second job
returns success, and closing the outer handle terminates the inner members.
"""

from __future__ import annotations

import inspect


def _src(cli) -> str:
    return inspect.getsource(cli)


def _between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


def test_a_supervisor_host_does_not_spawn_the_broker(cli):
    """cmd_start must leave the spawn to --foreground, and still run the gate."""
    blk = _between(
        _src(cli),
        'if svc in ("systemd-user", "service", "launchd"):\n        err = _mqtt_gate()',
        'else:\n        mqtt_state = _mqtt_ensure_running(replace=True)',
    )
    assert "_mqtt_gate()" in blk, (
        "a broken cfg must still fail before anything is launched"
    )
    assert "_mqtt_ensure_running(" not in blk, (
        "cmd_start spawned the broker under a supervisor — that process exits "
        "and the listener is orphaned (C9)"
    )


def test_foreground_is_the_process_that_owns_it(cli):
    src = _src(cli)
    i = src.index("def _run_foreground(")
    blk = src[i:src.index("\ndef ", i + 10)]
    assert "_mqtt_ensure_running(replace=True)" in blk, (
        "_run_foreground must bring the broker up: it is the only process whose "
        "lifetime equals the home's"
    )


def test_running_pass_reports_the_half_state_instead_of_spawning(cli):
    """`start` while hass is up must not become another short-lived owner."""
    blk = _between(
        _src(cli),
        'if svc in ("systemd-user", "service", "launchd"):\n                # This pass ends',
        'elif _mqtt_ensure_running() == "failed":',
    )
    assert "_mqtt_pid() is None" in blk
    assert 'log_state("FAIL"' in blk, (
        "a home with MQTT configured and no listener is not success"
    )
    assert "_mqtt_ensure_running(" not in blk, (
        "this pass returns immediately; spawning here re-creates the orphan"
    )


def test_the_job_handle_is_kept_alive_at_module_level(cli):
    src = _src(cli)
    assert "_WIN_JOBS: dict[str, object] = {}" in src, (
        "the job handle needs module-level storage: dropping the last handle is "
        "what terminates hass and the broker"
    )
    i = src.index("def _win_job_arm(scope")
    blk = src[i:src.index("\ndef ", i + 10)]
    assert "_WIN_JOBS[scope] = job" in blk, "the armed handle is not retained"


def test_both_layers_arm_a_kill_on_close_job(cli):
    src = _src(cli)
    assert '_win_job_arm("service")' in src, "the service host must hold the outer job"
    assert '_win_job_arm("foreground")' in src, "the foreground child needs its own job"
    i = src.index("def _win_job_arm(scope")
    blk = src[i:src.index("\ndef ", i + 10)]
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in blk
    assert "AssignProcessToJobObject" in blk


def test_arming_degrades_quietly_off_windows(cli, monkeypatch):
    """Hardening must never be the reason a start fails."""
    monkeypatch.setattr(cli, "IS_WIN", False)
    assert cli._win_job_arm("d23-never-windows") is None
