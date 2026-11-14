#!/usr/bin/env python3
"""Acceptance harness — one machine-checkable entry for every open TODO item.

    python verify.py <ID>               # 0 PASS, 1 FAIL, 2 SKIP
    python verify.py <ID> --expect-red  # 0 = the defect is still visible
    python verify.py --all              # every case; rc=1 if any FAILs
    python verify.py --list             # ids, titles, implemented?
    python verify.py --compile          # syntax-check every .py we own
    python verify.py D01 --self-test    # proves the exit-code contract

Run it from the repo root (or set ``HOMEANT_ROOT``); ``gate_paths`` resolves the
tree.  This file lives in ``tests/`` and is version-managed, but it must not
assume a fixed depth in the tree — ``gate_paths`` owns that answer.

Three rules this file exists to enforce, straight from TODO.md §0:

1. **Red first.**  ``--expect-red`` turns "the criterion fails" into an exit
   code, so a green run that never saw the defect cannot be mistaken for a fix.
2. **SKIP is not PASS.**  A case that cannot run here returns 2 and has to be
   registered in TODO §6; ``--all`` reports it but does not hide it.
3. **No soft criteria.**  Every case ends in a comparison — an exit code, a
   rendered value, a counted line.  Nothing here reads a human's eyeballs.

Cases are plain functions: return a detail string on PASS, raise ``Skip`` to
return 2, raise anything else to return 1.  The traceback's last frame is
appended to the message, so a failure names the file and line that judged it.

``__probe_pass`` / ``__probe_fail`` are hidden cases used by --self-test: they
run through the real dispatch path in a real subprocess, so what gets verified
is the contract, not a mock of it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from gate_paths import GATE_DIR, REPO_ROOT

PASS, FAIL, SKIP = 0, 1, 2

# Every id that must be dispatchable.  Order is the TODO's execution order.
# Adding a row here is deliberate: --list and --all read this table, so a case
# that exists in TODO.md but not here is invisible, and one here but nowhere
# else is a dead entry nobody will ever run.
EXPECTED: tuple[tuple[str, str], ...] = (
    ("D01", "harness"),
    ("D02", "cfg reads drop a UTF-8 BOM (3 sites)"),
    ("D03", "dns-sd reap spares a foreign _mqtt._tcp"),
    ("D04", "_expose_all reports a failed entity"),
    ("D05", "_WSConn.recv handles EOF and control frames"),
    ("D06", "automations.yaml list root gets merged"),
    ("D07", "homeant.cfg.default comments match the code"),
    ("D08", "_ws_purge_normal gets the bearer, not the LLAT"),
    ("D09", "Windows stop is graceful before it is forceful"),
    ("D10", "speaker-discovery template trigger is boolean"),
    ("D11", "_mqtt._tcp is announced with a LAN address"),
    ("D16", "installer static contract is enforced by check_drift"),
    ("D15", "installer matches the runtime (uv / digest / 1883)"),
    ("D17", "service environment carries its working dirs"),
    ("D20", "a dropped component is reinstalled, a stale one is not"),
    ("D22", "installer --upgrade redeploys the source tree it stands in"),
    ("D23", "the broker is owned by the supervised process"),
    ("D24", "preflight checks 8123 and 1883, and spares our own broker"),
)

# The harness's own parts, all relative to GATE_DIR.  A missing one means the
# gate is incomplete, and every later case would fail for the wrong reason.
REQUIRED_FILES = (
    "gate_paths.py",
    "gate_cli.py",
    "gate_ha.py",
    "verify.py",
    "conftest.py",
    "test_harness.py",
    "test_mdns_reap.py",
    "test_voice.py",
    "test_ws.py",
    "test_seed_merge.py",
    "test_cfg_text.py",
    "test_speaker_trigger.py",
    "test_installer_digest.py",
    "test_stop_ws.py",
    "test_service_env.py",
    "check.sh",
    "check.ps1",
)

CASES: dict[str, Callable[[], str | None]] = {}


class Skip(Exception):
    """rc=2: the criterion cannot run here (wrong platform, no instance, …)."""


def case(case_id: str):
    def register(fn: Callable[[], str | None]) -> Callable[[], str | None]:
        CASES[case_id] = fn
        return fn

    return register


# ── helpers ─────────────────────────────────────────────────────────────────


def uv_bin() -> Path | None:
    """The uv this repo ships with, else one on PATH.

    The bundled one wins on purpose: it is the version the installers download,
    so a gate run here exercises what a user will get.
    """
    for rel in (".tools/uv/uv.exe", ".tools/uv/uv"):
        candidate = REPO_ROOT / rel
        if candidate.is_file():
            return candidate
    found = shutil.which("uv")
    return Path(found) if found else None


def pytest_cmd(*args: str) -> list[str] | None:
    """A pytest invocation that cannot touch .venv.

    ``--no-project`` is the load-bearing flag: with it, uv builds a throwaway
    environment that holds exactly pytest (+ PyYAML, which ``_merge_file``
    imports) and never syncs, prunes or installs into the project venv — the HA
    runtime pinned by uv.lock.  ``uv run`` without it would be free to do
    exactly that, which is the drift this project has already been bitten by.
    ``bcrypt`` is here for the broker's auth plugin: the encoding HA stores is
    the thing most likely to break, and a fake hash would verify nothing.

    The two flags are here rather than at the call sites because there are two
    call sites (one file, and the whole directory) and they had already drifted:

    * ``-p no:cacheprovider`` keeps ``.pytest_cache`` out of the repo — the gate
      is required to leave nothing behind (TODO §0, "验收只读").
    * ``-o tmp_path_retention_count=200`` is not about tidiness.  pytest's
      default is 3, so every session starts by deleting its older ``tmp_path``
      roots in ``%TEMP%\\pytest-of-<user>`` — and this machine's safe-delete
      guard refuses a bulk delete past 50 files, which is what made D17 report
      FAIL with a ``SAFE_DELETE_BULK_CONFIRM_REQUIRED`` payload instead of a
      test result (measured 11-14: 5 tests passed, the case still went red).
      Keeping the roots means nothing is deleted, so the guard never runs.
    """
    flags = ("-p", "no:cacheprovider", "-o", "tmp_path_retention_count=200")
    uv = uv_bin()
    if uv is not None:
        return [
            str(uv), "run", "--no-project",
            "--with", "pytest", "--with", "pyyaml", "--with", "voluptuous",
            "--with", "jinja2", "--with", "bcrypt",
            "pytest", *flags, *args,
        ]
    if subprocess.run(
        [sys.executable, "-c", "import pytest, yaml, voluptuous"],
        capture_output=True, cwd=REPO_ROOT,
    ).returncode == 0:
        return [sys.executable, "-m", "pytest", *flags, *args]
    return None


def _tail(text: str, lines: int = 12) -> str:
    kept = [ln for ln in text.strip().splitlines() if ln.strip()]
    return " | ".join(kept[-lines:])


def pytest_file(filename: str, *, timeout: int = 300) -> str:
    """Run one gate test file and turn its exit code into a case result.

    Cases needing PyYAML, voluptuous or the HA stub tree live in a pytest file
    instead of here: this script runs under the plain system python, which has
    none of them, and inventing a second way to import them per case is exactly
    the drift a harness must not have.

    ``pytest_cmd`` carries the two flags every run needs (no cache dir, and a
    ``tmp_path`` root old enough that pytest never deletes it); this only names
    the file.
    """
    cmd = pytest_cmd(str(GATE_DIR / filename), "-q")
    if cmd is None:
        raise Skip("no pytest runner: neither .tools/uv nor an importable pytest")
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT,
                          timeout=timeout)
    out = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise AssertionError(f"{filename}: {_tail(out)}")
    summary = [ln for ln in out.strip().splitlines() if ln.strip()]
    return f"{filename}: {summary[-1].strip() if summary else 'rc=0'}"


def _fail_text(exc: BaseException) -> str:
    """One line: the message, then where it was raised."""
    where = ""
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    if tb is not None:
        where = f" ({Path(tb.tb_frame.f_code.co_filename).name}:{tb.tb_lineno})"
    return f"{str(exc) or exc.__class__.__name__}{where}"


def _neutralize_env():
    """Take the harness's network environment out of the way; return the undo.

    Two artifacts of the session this gate runs in, neither of which a user's
    ``homeant`` meets:

    * A service proxy in ``HTTP(S)_PROXY``.  Deleting the variables is not
      enough on Windows — urllib then falls back to the WinINET registry, which
      also names a proxy — so ``no_proxy=*`` goes in to make
      ``proxy_bypass()`` answer True for every host.
    * ``localhost`` resolves to ``::1`` first, and this host *accepts* a TCP
      connect on ``::1:8123`` (nothing is listening there per netstat) and then
      never answers, so every ``http://localhost:8123`` request burns its full
      timeout instead of falling back.  The product asks for ``localhost``; the
      gate hands the same server over IPv4.

    Both are measured environment defects on this machine, recorded as
    candidates in TODO §5 and deliberately not repaired here: a case has to
    measure the product, not the harness's own surprise.
    """
    import os
    import socket

    saved: dict[str, str | None] = {}
    for key in [k for k in os.environ
                if k.lower() in ("http_proxy", "https_proxy", "all_proxy")]:
        saved[key] = os.environ.pop(key)
    for key in ("no_proxy", "NO_PROXY"):
        saved[key] = os.environ.pop(key, None)
        os.environ[key] = "*"

    real_connect = socket.create_connection

    def ipv4_loopback(address, *args, **kwargs):
        host, port = address[0], address[1]
        return real_connect(("127.0.0.1" if host == "localhost" else host, port),
                            *args, **kwargs)

    socket.create_connection = ipv4_loopback
    return saved, real_connect


def _restore_env(state) -> None:
    import os
    import socket

    saved, real_connect = state
    socket.create_connection = real_connect
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def run_case(case_id: str) -> tuple[int, str]:
    fn = CASES.get(case_id)
    if fn is None:
        return SKIP, "case not implemented yet"
    saved_env = _neutralize_env()
    try:
        detail = fn()
    except Skip as exc:
        return SKIP, str(exc)
    except Exception as exc:  # noqa: BLE001 - any raise is a FAIL by contract
        return FAIL, _fail_text(exc)
    finally:
        _restore_env(saved_env)
    return PASS, detail or ""


# ── the harness's own case ──────────────────────────────────────────────────


def self_test_failures() -> list[str]:
    """Run the four probe combinations as subprocesses; return what disagreed.

    Each probe is a separate process because the thing under test is the exit
    code the shell sees.  Calling ``run_case`` in-process would test a return
    value and prove nothing about ``sys.exit``.
    """
    script = str(Path(__file__).resolve())
    probes: tuple[tuple[str, list[str], int], ...] = (
        ("probe pass            -> 0", ["__probe_pass"], PASS),
        ("probe fail            -> 1", ["__probe_fail"], FAIL),
        ("probe pass --expect-red -> 1", ["__probe_pass", "--expect-red"], FAIL),
        ("probe fail --expect-red -> 0", ["__probe_fail", "--expect-red"], PASS),
    )
    failures: list[str] = []
    for label, args, want in probes:
        proc = subprocess.run(
            [sys.executable, script, *args],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        if proc.returncode != want:
            failures.append(f"{label}: got rc={proc.returncode}, want {want}")
    return failures


@case("D01")
def _case_d01() -> str:
    problems: list[str] = []

    # Root resolution is the one thing that can fail silently: every case would
    # then judge the wrong tree and still print PASS.
    if not (REPO_ROOT / "scripts" / "homeant.py").is_file():
        problems.append(f"REPO_ROOT={REPO_ROOT} has no scripts/homeant.py")

    for name in REQUIRED_FILES:
        if not (GATE_DIR / name).is_file():
            problems.append(f"missing {name} in {GATE_DIR}")

    ids = [cid for cid, _ in EXPECTED]
    if len(ids) != len(set(ids)):
        problems.append("duplicate id in EXPECTED")
    # 18 rows: D01-D11 (11), D15, D16, D17, D20, D22, D23, D24.  A stale count
    # here means a row was dropped, which would make that case unrunnable and
    # unlisted.  D21 left with the WSL support (11-17).
    if len(EXPECTED) != 18:
        problems.append(f"EXPECTED has {len(EXPECTED)} ids, TODO lists 18")
    for cid in CASES:
        if not cid.startswith("__") and cid not in set(ids):
            problems.append(f"case {cid} registered but absent from EXPECTED")

    conftest = GATE_DIR / "conftest.py"
    if conftest.is_file():
        try:
            compile(conftest.read_text(encoding="utf-8"), str(conftest), "exec")
        except SyntaxError as exc:
            problems.append(f"conftest.py does not compile: {exc}")

    problems.extend(self_test_failures())

    # Last and slowest: collection proves conftest imports under a real pytest
    # (the fixtures above are only syntax-checked until something asks for
    # them).  A collection error is exactly the failure that makes every later
    # case unrunnable, so it belongs in D01 rather than in D02.
    cmd = pytest_cmd(str(GATE_DIR), "--collect-only", "-q")
    if cmd is None:
        problems.append("no pytest runner: neither .tools/uv nor an importable pytest")
    else:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT,
                              timeout=600)
        if proc.returncode != 0:
            problems.append(
                f"pytest collection failed (rc={proc.returncode}): "
                f"{_tail(proc.stdout + proc.stderr)}"
            )

    if problems:
        raise AssertionError("; ".join(problems))
    return f"repo={REPO_ROOT.name}, gate={GATE_DIR.name}"


@case("D02")
def _case_d02() -> str:
    """A cfg written by a BOM-adding editor must parse like any other.

    The defect is silent: the BOM sticks to the first key, so the dict holds
    ``\\ufeffUSERNAME`` while the code asks for ``USERNAME`` and
    ``cf("USERNAME", "admin")`` quietly returns the default.  Nothing raises
    and nothing logs, so only a value assertion can see it — hence both the
    control and the BOM file are asserted here, in one case.
    """
    from gate_cli import cli as cli_module  # lazy: --list/--compile must not run it

    cli = cli_module()
    body = "USERNAME=alice\nPASSWORD=s3cret\n"
    original = cli.CONFIG_FILE
    try:
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain.cfg"
            bom = Path(tmp) / "bom.cfg"
            plain.write_text(body, encoding="utf-8")
            bom.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))

            cli.CONFIG_FILE = plain
            control = cli.load_cfg()
            cli.CONFIG_FILE = bom
            with_bom = cli.load_cfg()

            # Same root cause, two more readers of the same file.  _cfg_values
            # feeds _reconcile_cfg, where a BOM-prefixed key looks missing and
            # gets appended a second time; _cfg_set rewrites one KEY= line and
            # cannot match the first one.  Judged on the live file, because
            # that is where both read.
            rewrite = Path(tmp) / "rewrite.cfg"
            rewrite.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
            cli.CONFIG_FILE = rewrite
            rewrote = cli._cfg_set("USERNAME", "bob")
            after = rewrite.read_bytes()
    finally:
        cli.CONFIG_FILE = original

    assert control.get("USERNAME") == "alice", (
        f"control case broken - a BOM-free cfg parsed as {control!r}"
    )
    if with_bom.get("USERNAME") != "alice":
        stray = sorted(k for k in with_bom if k != "USERNAME" and k.endswith("USERNAME"))
        raise AssertionError(
            "BOM leaked into the first key: 'USERNAME' is absent"
            + (f", stored as {stray!r}" if stray else "")
            + f"; keys={sorted(with_bom)!r}"
        )
    assert with_bom.get("PASSWORD") == "s3cret", f"later keys broken: {with_bom!r}"
    assert rewrote is True, (
        "BOM makes the first KEY= line unmatchable, so _cfg_set cannot rewrite it"
    )
    assert b"\xef\xbb\xbf" not in after, f"rewrite left the BOM in place: {after[:24]!r}"
    assert after.startswith(b"USERNAME=bob"), f"first line not rewritten: {after[:24]!r}"
    return "load_cfg reads it, _cfg_set rewrites it (BOM dropped on write)"


@case("D03")
def _case_d03() -> str:
    """The reaper's selector and its pre-kill confirmation.

    Criterion: another app's ``dns-sd -P Other _mqtt._tcp`` must not match, our
    own records must, and the pid's full command line must be re-checked before
    SIGTERM (pgrep -f matches any line containing the argv).
    """
    return pytest_file("test_mdns_reap.py")


@case("D04")
def _case_d04() -> str:
    """``_expose_all`` reports failure through ``_on_start``'s behaviour.

    Criterion: with one entity failing its self-check, a registry listener is
    registered (the bridge converges later); with everything exposed, none is.
    """
    return pytest_file("test_voice.py")


@case("D05")
def _case_d05() -> str:
    """``_WSConn.recv`` on EOF and on control frames.

    Criterion: ``b""`` raises within the budget instead of spinning, close frame
    raises, ping is answered with a pong and the next text frame still parses.
    """
    return pytest_file("test_ws.py")


@case("D06")
def _case_d06() -> str:
    """A list-root seed file gets the elements a newer release added.

    Criterion: existing elements keep the deployed values, the new one appears,
    a second run changes nothing, the user's text survives, and
    ``_deploy_config_files`` (the upgrade path) shows the same end to end.
    """
    return pytest_file("test_seed_merge.py")


@case("D07")
def _case_d07() -> str:
    """The cfg template describes the shipped provisioning, not a manual path.

    Criterion: no "does NOT update it", no ``change_password`` instruction, the
    authority rule stated, and the promised password length equals ``PW_LEN``.
    """
    return pytest_file("test_cfg_text.py")


def repo_module(rel: str, name: str):
    """Load a repo script by path.

    ``scripts/dist-pack.py`` cannot be imported by name — the dash makes it an
    invalid identifier — and shelling out to it would run the whole packager,
    which writes to dist/.  Loading the module from its file gives the gate the
    function without the side effect.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@case("D16")
def _case_d16() -> str:
    """Four groups of "must agree" strings get a check before a release.

    None of them has one today, and each of them can only ever surface on a
    user's machine: the uv release (four spellings, one of which is a
    directory), the digest literal (must still be the placeholder in the tree —
    dist/ carries the stamped copy), the preflight port list (two copies, with a
    comment claiming they are one list) and the width of the digest prefix that
    is cut into the release file name — one hex char of disagreement and that
    platform fetches a name the host never has.

    Criterion: inject each drift separately into a copy of the tree and require
    ``check_drift`` to report it, naming the file; the untouched copy must
    report nothing.  Judged on the returned list, not on ``grep``'s exit code —
    an exit code cannot tell "the check is missing" from "the check passed".
    """
    import re

    pack = repo_module("scripts/dist-pack.py", "dist_pack_under_test")

    copied = ("scripts/install.sh", "scripts/install.ps1", "scripts/homeant.py",
              "pyproject.toml")
    sh_ps1 = ("scripts/install.sh", "scripts/install.ps1")

    def substitute(root: Path, rel: str, pattern: str, repl: str) -> None:
        """Rewrite one line in place, bytes in and out (a CRLF/LF rewrite here
        would make the port regex the thing under test)."""
        path = root / rel
        text = path.read_bytes().decode("utf-8")
        new, hits = re.subn(pattern, repl, text, count=1, flags=re.M)
        assert hits == 1, f"injection missed in {rel}: {pattern!r} not found"
        path.write_bytes(new.encode("utf-8"))

    # One injection per round, each restored before the next: three separate
    # one-cause verdicts beat one three-cause list, which cannot say which
    # check failed to notice what.
    drift_cases = (
        ("uv version bumped on one side", "scripts/install.ps1",
         r'^\$UvVersion\s*=\s*"[^"]+"', '$UvVersion             = "0.12.23"',
         "install.ps1"),
        ("a real digest where the placeholder belongs", "scripts/install.sh",
         r'^HOMEANT_ZIP_SHA256="[^"]*"',
         'HOMEANT_ZIP_SHA256="%s"' % ("ab" * 32),
         "HOMEANT_ZIP_SHA256"),
        ("a port added to one list only", "scripts/install.sh",
         r'^PREFLIGHT_PORTS="[^"]*"',
         # a real newline, not ``\n``: preflight() splits the list on one, so a
         # space-separated fake would be a single entry at runtime and this case
         # would be asserting against a shape the script never sees.  1883 is
         # now in BOTH lists, so the injected drift must be a port in neither.
         'PREFLIGHT_PORTS="8123|Home Assistant HTTP\n1883|HomeAnt MQTT\n9999|Probe"',
         "9999"),
        # The release file is named after the prefix each installer cuts, so a
        # width that slips on one side produces install.sh: 404 while the .ps1
        # path keeps working — the kind of half-broken release no test on either
        # side alone can see.
        ("digest prefix widened on one side", "scripts/install.ps1",
         r"\$HOMEANT_ZIP_SHA256\.Substring\(0, 8\)",
         "$HOMEANT_ZIP_SHA256.Substring(0, 12)",
         "install.ps1"),
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir()
        for rel in copied:
            (root / rel).write_bytes((REPO_ROOT / rel).read_bytes())
        # check 7 asks for the CDN directory, so the copy needs it to be clean
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        uv_ver = re.search(r'"uv==([^"]+)"', pyproject)
        assert uv_ver, "pyproject.toml no longer pins uv== — this case cannot judge"
        (root / "uv" / uv_ver.group(1)).mkdir(parents=True)

        version = pack.read_version(root / "pyproject.toml")
        baseline = pack.check_drift(root, version)
        originals = {rel: (root / rel).read_bytes() for rel in sh_ps1}

        findings: list[str] = []
        for label, rel, pattern, repl, must_name in drift_cases:
            for name, blob in originals.items():
                (root / name).write_bytes(blob)
            substitute(root, rel, pattern, repl)
            problems = pack.check_drift(root, version)
            if not problems:
                findings.append(f"{label}: check_drift reports nothing")
            elif not any(must_name in p for p in problems):
                findings.append(
                    f"{label}: reported {problems!r}, none naming {must_name}"
                )

    if baseline:
        raise AssertionError(
            f"the untouched copy is already drifted: {baseline}"
        )
    if findings:
        raise AssertionError("; ".join(findings))
    return (f"4/4 static contracts enforced (uv {uv_ver.group(1)}, digest, "
            f"ports, tag width)")


# Whether a case runs its "needs a live instance" half.  Set from --live in
# main().  The default is the offline half: the gate has to be runnable on a
# machine with no instance at all, so the expensive half is opt-in.
LIVE = False


def _d08_live(cli) -> str:
    """Count `normal` refresh tokens across three token-rebuild runs.

    Each run deletes the cached MCP token, which forces the login → LLAT →
    cleanup path.  Left unfixed, every run of `start` leaves one more ~90-day
    admin refresh token in .storage/auth; fixed, the count is flat.  Nothing is
    restored by hand: the last run puts `.mcp-token` back the way it was.
    """
    import json
    import time

    auth_file = cli.HA_ROOT / ".storage" / "auth"
    if not auth_file.is_file():
        raise Skip(f"{auth_file} does not exist — nothing provisioned on this machine")
    # CFG is module state that the entry point fills in; a module loaded straight
    # from the file has it empty, so every cf() falls back to its default — and
    # for PASSWORD that default is an empty string, i.e. a guaranteed login
    # failure that looks like a product bug.  Do what main() does.
    cli.CFG = cli.load_cfg()
    # An API route, not `/`: the frontend answers `/` with HTML and _http_get
    # parses every 200 body as JSON, so probing the root fails on a healthy
    # instance.  401 is the answer we want — something is serving HA's API.
    status, _ = cli._http_get(f"http://127.0.0.1:{cli.PORT}/api/core/state",
                             timeout=5.0)
    if status not in (200, 401):
        raise Skip(f"port {cli.PORT} answered HTTP {status} — nothing to count, run start")

    def count_normal() -> int:
        # The store calls the field `token_type`; the websocket API calls the
        # same thing `type`.  Reading the store means using the store's name —
        # counting `.get("type")` here would count nothing, forever, and pass.
        data = json.loads(auth_file.read_text(encoding="utf-8"))
        return sum(1 for t in data["data"]["refresh_tokens"]
                   if t.get("token_type") == "normal")

    def settle() -> None:
        """Wait for the store to stop changing before it is counted.

        Auth writes go through a delay-save, so a count taken immediately after
        a rebuild can predate the run it is meant to judge — the one race that
        would hide exactly the drift this case is about.
        """
        last = auth_file.stat().st_mtime_ns
        quiet_since = time.monotonic()
        while time.monotonic() - quiet_since < 1.5:
            time.sleep(0.25)
            now = auth_file.stat().st_mtime_ns
            if now != last:
                last, quiet_since = now, time.monotonic()

    settle()
    before = count_normal()
    for round_no in range(1, 4):
        # The rebuild itself, not `start`: login → mint LLAT → revoke the
        # login's own token is the whole sequence under test, and `start` wraps
        # it in a service-state gate that has nothing to do with it (unelevated,
        # that gate refuses before a single token is minted).
        cli.MCP_TOKEN_FILE.unlink(missing_ok=True)
        token = cli._mcp_token()
        if not token:
            raise AssertionError(f"round {round_no}: _mcp_token() returned nothing")
        settle()
    after = count_normal()
    if after != before:
        raise AssertionError(
            f"normal refresh tokens went {before} -> {after} over three rebuilds: "
            "each run leaves the login's own token behind"
        )
    return (f"3 rebuilds, normal refresh tokens flat at {before} "
            f"(offline half covered by the plain run)")


@case("D08")
def _case_d08() -> str:
    """The login's own refresh token is revoked with the login's own bearer.

    ``_mcp_token`` logs in (minting a ~90-day `normal` refresh token), mints an
    LLAT, then revokes the normal one so repeated runs do not pile them up.  It
    hands that cleanup the LLAT: authenticating with an LLAT makes
    ``is_current`` name the long-lived token, never the normal one, so the
    selector matches nothing, the function returns having done nothing, and
    nothing anywhere says so.

    Criterion (offline): with login / LLAT / purge replaced by recorders, the
    argument the purge receives must be the bearer ``_login`` returned.
    Criterion (--live): three forced rebuilds leave the count of `normal`
    refresh tokens unchanged.
    """
    from gate_cli import cli as cli_module

    cli = cli_module()
    if LIVE:
        return _d08_live(cli)

    bearer, llat = "bearer-from-login", "llat-token"
    seen: list[str] = []
    saved = (cli.MCP_TOKEN_FILE, cli.cf, cli._login, cli._ws_llat,
             cli._ws_purge_normal)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            cli.MCP_TOKEN_FILE = Path(tmp) / ".mcp-token"   # a cache miss
            cli.cf = lambda key, default=None: default
            cli._login = lambda user, password: bearer
            cli._ws_llat = lambda b, timeout=10.0: llat
            cli._ws_purge_normal = lambda arg, timeout=10.0: seen.append(arg)
            token = cli._mcp_token()
        finally:
            (cli.MCP_TOKEN_FILE, cli.cf, cli._login,
             cli._ws_llat, cli._ws_purge_normal) = saved

    assert token == llat, f"_mcp_token returned {token!r}, expected the LLAT"
    if not seen:
        raise AssertionError(
            "the cleanup never ran — the login's normal refresh token stays behind"
        )
    if seen != [bearer]:
        raise AssertionError(
            f"cleanup was handed {seen!r}; it must get the bearer from _login "
            f"({bearer!r}).  With an LLAT, is_current names the long-lived "
            "token, so a `normal` selector never matches and nothing is revoked"
        )
    return "cleanup receives the login bearer, never the LLAT"


def _d09_live(cli) -> str:
    """`stop` has to ask hass to leave before it forces it — measured by exit code.

    The observable is hass's *own* exit status, and nothing else will do.  What
    was tried first and rejected:

    * `home-assistant.log`: HA writes **zero** bytes on a shutdown (measured
      11-14 on 2026.11.1: a REST `homeassistant.stop` returns 200, the process
      is gone in 1.16 s, the log has no new byte).  TODO's proposed
      ``Home Assistant (stopping|stopped)`` regex does not exist in this tree.
    * `.storage/*` mtimes: written periodically, so an unchanged file proves
      nothing and a changed one proves nothing about *why*.
    * anything the product prints: a stop that says "graceful" about itself is
      self-certifying, which is exactly the trap this gate exists to avoid.

    The exit code is none of those.  ``async_stop(exit_code=0)`` makes
    ``main()`` return 0, so a hass that shut itself down reports 0 — and a
    process ended by ``taskkill /F`` cannot: it reports 1.  That number comes
    from the operating system, not from us.

    Getting at it needs a parent, so the instance is started with
    ``--foreground`` (the same entry point every supervisor runs) and `stop` is
    then asked from a second process, exactly as a user would.  `finally` puts
    a normal detached instance back, because the caller of this gate is a
    working machine, not CI.
    """
    if not sys.platform.startswith("win"):
        raise Skip("Windows-only: POSIX stop already signals, the /F path is Windows'")
    python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = REPO_ROOT / ".venv" / "bin" / "python"
    if not python.is_file():
        raise Skip("no .venv interpreter — this tree cannot run start")
    script = REPO_ROOT / "scripts" / "homeant.py"
    cmd = [str(python), str(script)]

    def up() -> bool:
        """Does HA answer HTTP yet?  Not "does the CLI's JSON client parse it".

        ``cli._http_get`` decodes every response as JSON, so against ``/`` — the
        landing page, HTML — it raises ``JSONDecodeError``.  Catching that as
        "not up" is how this case waited out its whole 300 s window against a
        perfectly healthy instance (measured 11-14: ``curl`` answered 200 while
        ``_http_get`` raised).  A bare TCP connect is not the answer either:
        HA binds its sockets before it accepts on them (see
        ``_hass_serving``), and a stop landing in that gap would be judged as a
        forced one.  So: a real request, and only the status code is read.
        """
        import urllib.error
        import urllib.request

        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{cli.PORT}/", timeout=3.0) as resp:
                return resp.status == 200
        except urllib.error.HTTPError:
            return False  # bound, but not serving what `start` waits for
        except OSError:
            return False

    def stop_now(timeout: int = 180) -> subprocess.CompletedProcess:
        return subprocess.run(cmd + ["stop"], capture_output=True, text=True,
                              cwd=REPO_ROOT, timeout=timeout)

    # A clean stage: `--foreground` refuses to start beside a live instance, and
    # the instance here belongs to whoever ran `start` last.  Clearing it is not
    # part of the criterion, so nothing about this run is judged.
    if cli._ha_instance() or up():
        stop_now()
    for _ in range(120):
        if not up() and not cli._ha_instance():
            break
        time.sleep(0.5)

    work = Path(tempfile.mkdtemp(dir=GATE_DIR, prefix="d09-"))
    fg_log = work / "foreground.log"
    handle = open(fg_log, "wb")
    fg = subprocess.Popen(cmd + ["start", "--foreground"], cwd=REPO_ROOT,
                          stdout=handle, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            if up():
                break
            if fg.poll() is not None:
                handle.close()
                raise AssertionError(
                    "start --foreground exited before serving:\n"
                    + _tail(fg_log.read_text(encoding="utf-8", errors="replace"))
                )
            time.sleep(1.0)
        else:
            handle.flush()
            handle.close()
            raise AssertionError(
                "instance never answered on its port within 300s; foreground said:\n"
                + _tail(fg_log.read_text(encoding="utf-8", errors="replace"))
            )

        # HTTP 200 on `/` is not "started": the http component serves while
        # integrations are still being imported, and a stop landing in that gap
        # makes the core wait for its own startup to finish before it can shut
        # down.  The one 30.46 s timeout in this case was a stop
        # issued seconds after `/` first answered 200, while four stops against
        # a RUNNING core (event log) finished in 1-2 s.  Waiting for RUNNING
        # measures the path a user takes — `start` returns only past this point.
        handle.flush()
        if not cli._wait_for_ha_running(timeout=180):
            raise AssertionError(
                "core never reported RUNNING within 180s; foreground said:\n"
                + _tail(fg_log.read_text(encoding="utf-8", errors="replace"))
            )

        started = time.monotonic()
        proc = stop_now()
        elapsed = round(time.monotonic() - started, 2)
        try:
            rc_fg = fg.wait(timeout=180)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"stop returned but hass was still running after 180s "
                f"(stop rc={proc.returncode}, {elapsed}s)"
            ) from None
    finally:
        if fg.poll() is None:
            # `.venv\Scripts\python.exe` is a trampoline: killing it leaves the
            # real interpreter (a child) alive, still holding the port and still
            # retrying whatever it was doing.  A plain kill()
            # here leaked a foreground run that then kept the venv busy.  /T ends
            # the tree, which is what `stop` itself does.
            subprocess.run(["taskkill", "/PID", str(fg.pid), "/F", "/T"],
                           capture_output=True)
            try:
                fg.wait(timeout=60)
            except subprocess.TimeoutExpired:
                pass
        handle.close()
        if not os.environ.get("GATE_KEEP"):
            # GATE_KEEP exists for one situation: this case hanging for 300 s
            # and the log it captured being the only evidence of why.  Deleting
            # it unconditionally makes the failure unreproducible.
            shutil.rmtree(work, ignore_errors=True)
        # Put the host back the way it was found.  This gate runs on a machine
        # somebody is using, not in CI: a case that leaves it with no instance
        # has broken the thing it was measuring.
        subprocess.run(cmd + ["start", "--no-upgrade"], capture_output=True,
                       text=True, cwd=REPO_ROOT, timeout=900)

    output = proc.stdout + proc.stderr
    if proc.returncode != 0:
        raise AssertionError(f"stop rc={proc.returncode}: " + _tail(output))
    if elapsed >= 30:
        raise AssertionError(f"stop took {elapsed}s — the graceful window is 30s")
    if "graceful stop timed out" in output:
        raise AssertionError(
            "stop fell through to force-kill: the graceful window expired\n"
            + _tail(output)
        )
    if rc_fg != 0:
        raise AssertionError(
            f"hass exited with {rc_fg}, not 0 — it was killed, not asked.  A "
            "graceful stop calls homeassistant.stop and lets main() return 0; "
            f"taskkill /F cannot produce 0.  stop said:\n{_tail(output)}"
        )
    detail = f"foreground: stop in {elapsed}s, hass exited 0 (shutdown was its own)"
    return f"{detail}; {_d09_live_service(cli)}"


def _homeant_events(since: float) -> list[str]:
    """Every line the product wrote to the Application event log after `since`.

    Reading that log is allowed to an ordinary user; writing it is not, which is
    both why the service (LocalSystem) is the only writer that matters here and
    why this works from an unelevated shell.  ``TimeGenerated`` comes back as a
    naive *local* datetime, so ``.timestamp()`` and ``time.time()`` are the same
    clock — comparing them is exact rather than approximate.

    Returns [] when pywin32 is missing: the caller decides whether that is a
    skip or a failure, and for the stop case it is a failure (an unreadable log
    cannot witness a graceful shutdown).
    """
    try:
        import win32evtlog
    except ImportError:
        return []
    found: list[tuple[float, str]] = []
    handle = win32evtlog.OpenEventLog(None, "Application")
    flags = (win32evtlog.EVENTLOG_BACKWARDS_READ
             | win32evtlog.EVENTLOG_SEQUENTIAL_READ)
    scanned = 0
    try:
        while scanned < 4000:
            records = win32evtlog.ReadEventLog(handle, flags, 0)
            if not records:
                break
            scanned += len(records)
            oldest = None
            for rec in records:
                when = rec.TimeGenerated.timestamp()
                oldest = when if oldest is None else min(oldest, when)
                if (rec.SourceName or "").lower() != "homeant":
                    continue
                if when < since:
                    continue
                found.append((when, (rec.StringInserts or [""])[0]))
            # One screen of entries older than the window is as far back as this
            # ever needs to walk; the loop above is bounded anyway, so this only
            # saves the last few reads on a busy log.
            if oldest is not None and oldest < since - 300:
                break
    finally:
        win32evtlog.CloseEventLog(handle)
    found.sort()
    return [msg for _, msg in found]


def _d09_live_service(cli) -> str:
    """The second Windows stop path: the SCM host owns hass, not the CLI.

    `_d09_live` above measures the *unsupervised* shape, where `stop` finds the
    pid itself and ends it.  On a machine with the service registered that shape
    is the rare one — the ordinary `stop` goes SCM → ``SvcStop`` →
    ``_kill_child``, a different function with its own graceful request and its
    own history (11-14: it ran twice, from two threads, and force-killed a tree
    it had just stopped politely).  ``test_stop_ws.py`` pins the *shape* of that
    code; only a real stop can say the core was asked rather than killed.

    The witness is ``child.returncode``, reported by the service host to the
    event log as ``service: hass exited with 0``.  That number comes from the
    operating system: a graceful stop lets ``main()`` return 0, and ``taskkill
    /F`` cannot produce it.  The window is bounded by `since` so an earlier run's
    lines cannot make this green.

    Skipped when no service is registered — an unsupervised host has nothing to
    exercise here, and `_d09_live` already covered what it does have.
    """
    state = cli._win_service_state()
    if state == "not installed":
        return "service: skipped (no registration on this host)"
    if state != "running":
        raise AssertionError(
            f"service is '{state}' after `start --no-upgrade` — the instance the "
            "two stop paths are supposed to share is not up")

    python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
    cmd = [str(python), str(REPO_ROOT / "scripts" / "homeant.py")]
    if not cli._wait_for_ha_running(timeout=180):
        raise AssertionError("the service instance never reached RUNNING")
    if not _homeant_events(0.0):
        raise Skip("cannot read the Application event log — no witness available")

    # Two seconds of slack: the log's second-resolution timestamps and this
    # clock do not have to agree to the millisecond, and a line written by the
    # previous `start` must not be mistaken for one written by this `stop`.
    since = time.time() - 2.0
    started = time.monotonic()
    proc = subprocess.run(cmd + ["stop"], capture_output=True, text=True,
                          cwd=REPO_ROOT, timeout=300)
    elapsed = round(time.monotonic() - started, 2)
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if cli._win_service_state() != "running":
                break
            time.sleep(0.5)
        else:
            raise AssertionError(
                f"`stop` returned {proc.returncode} but the SCM still reports the "
                "service as running after 90s")

        output = proc.stdout + proc.stderr
        if proc.returncode != 0:
            raise AssertionError(f"stop rc={proc.returncode}: " + _tail(output))
        if elapsed >= 30:
            raise AssertionError(
                f"service stop took {elapsed}s — the graceful window is 30s")

        lines = _homeant_events(since)
        joined = "\n".join(lines)
        if not lines:
            raise AssertionError(
                "the service wrote nothing to the event log — the witness for "
                "this case is missing, so it cannot pass")
        if "service: hass exited with 0" not in joined:
            raise AssertionError(
                "no 'service: hass exited with 0' in the window — hass was "
                "killed, not asked, or its code never came back.  Log said:\n"
                + joined)
        if "service: hass stopped gracefully" not in joined:
            raise AssertionError(
                "the service never recorded a graceful stop:\n" + joined)
        # Both are regressions this path has actually had, not hypotheticals:
        # a second HTTP stop aimed at an already-exited hass, and the 30 s
        # window expiring into a force-kill.  One request, one outcome.
        bad = [line for line in lines
               if "graceful stop: request failed" in line
               or "graceful stop: HTTP" in line
               or "graceful stop timed out" in line
               or "exited with None" in line]
        if bad:
            raise AssertionError(
                "the service stop was not a single graceful request:\n"
                + "\n".join(bad))
    finally:
        # The host belongs to whoever was using it before this case ran.
        subprocess.run(cmd + ["start", "--no-upgrade"], capture_output=True,
                       text=True, cwd=REPO_ROOT, timeout=900)
    return f"service: stop in {elapsed}s, hass exited 0"


@case("D09")
def _case_d09() -> str:
    """Windows ``stop`` is graceful before it is forceful.

    The old ``_stop_pid`` docstring argues — correctly — that there is no
    *signal* path to preserve: hass runs with ``DETACHED_PROCESS``, so it has no
    console to receive Ctrl+C.  That argument does not reach this defect, which
    goes over HTTP instead (``homeassistant.stop``).  Both facts are written
    into the function so the next reader does not mistake one for the other.

    Criterion (--live, Windows): both shapes, because Windows has two stop
    paths and a machine only ever exercises one of them.  Unsupervised: a
    ``--foreground`` instance is stopped from a second process; ``stop`` must
    return 0 inside 30 s and the instance must exit **0**.  Registered: the
    service is stopped through the SCM and the service host must record
    ``service: hass exited with 0`` — see ``_d09_live`` and
    ``_d09_live_service`` for why the exit code is the only honest signal here.
    """
    from gate_cli import cli as cli_module

    cli = cli_module()
    if LIVE:
        return _d09_live(cli)
    return pytest_file("test_stop_ws.py")


def _d10_live(cli) -> str:
    """Render the shipped trigger on a real instance and compare the two sets.

    The offline half proves the *string* is a boolean comparison; this proves
    the instance agrees — same template engine, same entity registry, same
    states.  A template can be boolean against stubs and still be wrong here
    (an attribute that does not exist, an entity that is not there).

    Neither side is hand-copied: the template comes out of the shipped
    automations.yaml, and the option list out of HA itself.
    """
    import re

    token_file = cli.HA_ROOT / ".mcp-token"
    if not token_file.is_file():
        raise Skip("no cached MCP token — run start once")
    tok = token_file.read_text(encoding="utf-8").strip()

    seed = (REPO_ROOT / "config" / "automations.yaml").read_text(encoding="utf-8")
    match = re.search(r'^\s*value_template:\s*"(\{\{ \(states\.notify.*?\}\})"\s*$',
                      seed, re.MULTILINE)
    if match is None:
        raise AssertionError(
            "config/automations.yaml has no boolean speaker trigger — the "
            "template this case exists to check is not the one being shipped"
        )
    template = match.group(1)

    base = f"http://127.0.0.1:{cli.PORT}"
    auth = {"Authorization": f"Bearer {tok}"}
    # `POST /api/template` answers text/plain, not JSON — `_http_post` parses a
    # body as JSON and would raise on the answer this very case exists to read.
    import json
    import urllib.error as _ue
    import urllib.request as _ur

    req = _ur.Request(
        f"{base}/api/template",
        data=json.dumps({"template": template}).encode("utf-8"),
        headers={**auth, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _ur.urlopen(req, timeout=15.0) as resp:
            rendered = resp.read().decode("utf-8").strip()
    except _ue.HTTPError as exc:
        raise AssertionError(
            f"POST /api/template answered {exc.code}: "
            f"{exc.read().decode('utf-8', 'replace')[:200]}"
        ) from None
    if rendered not in ("True", "False"):
        raise AssertionError(
            f"the trigger rendered {rendered[:80]!r}, not True/False — a "
            "template trigger reads non-booleans as false, so it can never fire"
        )

    status, state = cli._http_get(f"{base}/api/states/input_select.homeant_voice_speaker",
                                  headers=auth, timeout=10.0)
    if status != 200:
        raise Skip(f"input_select.homeant_voice_speaker is missing (HTTP {status})")
    options = [o for o in (state.get("attributes", {}).get("options") or [])
               if o != "unset"]

    status, states = cli._http_get(f"{base}/api/states", headers=auth, timeout=10.0)
    if status != 200:
        raise AssertionError(f"GET /api/states answered {status}")
    discovered = sorted(s["entity_id"] for s in states
                        if s.get("entity_id", "").startswith("notify.")
                        and "_play_text_a_" in s["entity_id"])

    if sorted(options) != discovered:
        raise AssertionError(
            f"options {sorted(options)} do not match the discovered speakers "
            f"{discovered}; the automation has not written them back"
        )
    return (f"trigger renders {rendered}; {len(discovered)} speaker(s) "
            f"match the option list")


@case("D11")
def _case_d11() -> str:
    """The broker record must name an address another machine can dial.

    Offline half: the record the component builds for ``_mqtt._tcp`` — its SRV
    target and its address — asserted on both announce backends (``zeroconf``
    here, ``dns-sd -P`` on macOS, which builds the same decision in a second
    place).  That is the whole defect: the address came out 127.0.0.1 because
    the record reused the name pinned to loopback for the Xiaomi OAuth callback.

    Live half: the same fact as another device sees it —
    ``avahi-browse -rtp _mqtt._tcp`` on Linux, ``dns-sd -L HomeAnt _mqtt._tcp
    local`` on macOS.  Neither tool exists on this host, so ``--live`` here is a
    skip rather than a pass; TODO §6 carries the row and the commands.
    """
    if LIVE:
        raise Skip("D11 live needs darwin/linux with avahi or dns-sd — TODO §6")
    return pytest_file("test_mdns_mqtt.py")


@case("D10")
def _case_d10() -> str:
    """The speaker-discovery trigger compares booleans, not lists.

    A ``template`` trigger is judged by ``result_as_boolean``: a rendered list is
    not a boolean, so it is false — permanently — and the trigger can never fire
    on a membership change, whatever its comment says.

    Criterion (offline): render the shipped template with stubs for the two HA
    context objects it uses and require the output to be ``True``/``False``, plus
    the edge behaviour: true while the option list lags, false once
    ``set_options`` wrote the same set, and true again when a speaker is removed.

    The live half renders the same value through ``POST /api/template`` on a
    running instance and then compares ``options`` with the entities HA reports
    — a string can be boolean against stubs and still be wrong against the real
    registry.
    """
    from gate_cli import cli as cli_module

    if LIVE:
        return _d10_live(cli_module())
    return pytest_file("test_speaker_trigger.py")


@case("D15")
def _case_d15() -> str:
    """The digest hand-over: what the packer stamps vs what the helpers answer.

    Two halves that fail on opposite sides: the packer must leave no placeholder
    in dist/ while the source tree keeps its own, and each runtime helper must
    answer 0 / 1 / 2 — verified, mismatch, could not check — without ever
    folding the third into the other two.

    Criterion: the helpers are lifted out of the real installers and run by the
    real shell / PowerShell, so this cannot drift from the file users download.

    The release-side checks (read the uploaded installer's digest back and
    recompute the uploaded zip) need a published release — TODO §6.
    """
    if LIVE:
        raise Skip("post-release read-back needs a published release — TODO §6")
    return pytest_file("test_installer_digest.py")


@case("D17")
def _case_d17() -> str:
    """The service environment carries the working dirs, not just the profile.

    Measured defect (11-14): HA installs component requirements at run time
    with ``uv pip install``.  With uv's default cache under SYSTEM's private
    ``%LOCALAPPDATA%`` the files it moves into site-packages keep SYSTEM's
    ACEs instead of inheriting the venv's: 16995 of 26805 files in ``.venv``
    became unreadable to the user who owns the install (``hass_frontend``
    15504, ``numpy`` 912; ``/`` answered 500), and neither ``Get-Acl`` nor
    ``icacls /reset`` could repair them because that user is not their owner.

    Pointing ``UV_CACHE_DIR`` / ``TMP`` / ``TEMP`` inside HA_ROOT fixed it:
    a service-installed ``numpy`` then produced 912 files, 0 unreadable.

    What this case guards is the *code*, not the machine: the fix must survive
    a re-registration (``_win_service_home_write`` used to write a one-element
    list) and must not break the non-elevated hot path of an old install.
    """
    return pytest_file("test_service_env.py")


@case("D20")
def _case_d20() -> str:
    """A component deleted from HA_ROOT survives a `start` — until it does not.

    The seed gate answers one question — "is the deployed seed the one this
    release ships" — by comparing a digest of ``config/`` against
    ``.seed-state``.  That is a statement about the release, and it stayed true
    while ``~/.homeant/custom_components/<domain>`` was missing, so nothing put
    the directory back: HA booted without the integration and every line of
    output read like success.

    ``_seed_ready`` already asks the same question of the vendored domains
    (``vendor/.../custom_components/<domain>``), which ``start`` copies from
    ``vendor/``; the shipped domains, copied out of ``config/custom_components``
    by ``_deploy_config_files``, had no such check.  The case pins the missing
    half without loosening the other: a matching marker must still leave a
    *stale* ``.py`` alone, because ``_merge_file`` has no merge semantics for
    ``.py`` and rewriting it from ``start`` would erase a user's edit.

    Criterion (offline, tmp HA_ROOT): deleting a shipped domain and calling
    ``_ensure_seed`` restores the file this release ships; a stale domain is
    byte-identical afterwards; the domain list the gate checks is the list the
    copier walks.
    """
    return pytest_file("test_seed_deploy.py")


@case("D22")
def _case_d22() -> str:
    """`--upgrade` in a source tree: correct refusal, useless message.

    The mode only accepts a source tree as its target, and a source tree is by
    definition never stamped — the packager stamps a copy.  So the version guard
    was reached on every such run and answered "installer is not stamped with a
    version", which describes a broken download; the comment above it even
    claimed the opposite ("this means not inside a source tree").  Measured
    11-14: both installers, nothing actionable.

    Supporting the mode is the wrong repair — it would replace the developer's
    tree with the published zip.  What is pinned is the refusal plus the three
    commands that do deploy local changes, in both files, in the same words.

    Criterion: the branch sits between the version guard and the generic
    message in each installer, the hint names stop → remove domain → start in
    that order, and neither hints at running the installer again.
    """
    return pytest_file("test_installer_tree.py")


@case("D23")
def _case_d23() -> str:
    """C9: the broker's lifetime was bound to a CLI that exits.

    `cmd_start` spawned the broker and only then handed hass to the supervisor,
    so the listener's parent was a command that finished seconds later.  Nothing
    owned it afterwards: `stop` guessed with a pid file that names a trampoline
    (C2), and 1883 stayed bound across a stop.

    Two halves are pinned here.  Ownership: under a supervisor only
    `start --foreground` spawns the broker, and the "hass already running" pass
    reports the half state instead of becoming another short-lived owner.
    Crash safety: both the service host and the foreground process arm a Job
    Object with `KILL_ON_JOB_CLOSE`, held at module level because dropping the
    handle is what terminates the children.

    Criterion: static contract in `test_broker_owner.py` (6 cases), plus the
    measured semantics — nested assignment succeeds and the outer close takes
    the inner members down (Windows 10, pywin32 `win32job`, 11-15).
    """
    return pytest_file("test_broker_owner.py")


@case("D24")
def _case_d24() -> str:
    """D15 action 3: the installer must refuse a foreign listener on 1883.

    Adding 1883 to the port list is not the whole fix.  The broker's command
    line has no ``--config`` (measured: ``python -m amqtt.scripts.broker_script
    -c <HA_ROOT>/mqtt.yaml``), so the PowerShell side used to call our own
    broker a stranger and hard-stop an upgrade on a running install.  The
    sh side cannot name an owner at all: procfs carries no pid, so it asks
    the service manager.

    Criterion: both lists contain 8123 and 1883, and the real shell /
    PowerShell helpers are lifted out of the installers and run - both ports
    probed, our own service spared, a foreign 1883 refused, and a foreign
    amqtt broker never claimed just because it uses the same module.
    """
    return pytest_file("test_installer_ports.py")


@case("__probe_pass")
def _probe_pass() -> str:
    return "always passes"


@case("__probe_fail")
def _probe_fail() -> str:
    raise AssertionError("always fails")


# ── entry point ─────────────────────────────────────────────────────────────


def visible_cases() -> list[tuple[str, str]]:
    return [(cid, title) for cid, title in EXPECTED if not cid.startswith("__")]


def print_list() -> int:
    width = max(len(cid) for cid, _ in EXPECTED)
    for cid, title in EXPECTED:
        state = "case" if cid in CASES else "pending"
        print(f"{cid:<{width}}  {state:<7}  {title}")
    return PASS


def run_one(case_id: str, expect_red: bool) -> int:
    if case_id not in CASES and case_id not in dict(EXPECTED) \
            and not case_id.startswith("__"):
        print(f"unknown case: {case_id} (try --list)")
        return FAIL
    title = dict(EXPECTED).get(case_id, case_id)
    rc, detail = run_case(case_id)

    if expect_red:
        if rc == FAIL:
            print(f"RED  {case_id} {title}" + (f" — {detail}" if detail else ""))
            return PASS
        if rc == PASS:
            print(f"FAIL {case_id} {title} — case passes already, so the "
                  f"criterion cannot see the defect")
        else:
            print(f"FAIL {case_id} {title} — SKIP cannot be red: {detail}")
        return FAIL

    label = {PASS: "PASS", FAIL: "FAIL", SKIP: "SKIP"}[rc]
    print(f"{label} {case_id} {title}" + (f" — {detail}" if detail else ""))
    return rc


def run_all() -> int:
    counts = {PASS: 0, FAIL: 0, SKIP: 0}
    failed: list[str] = []
    skipped: list[str] = []
    for cid, title in visible_cases():
        rc, detail = run_case(cid)
        counts[rc] += 1
        label = {PASS: "PASS", FAIL: "FAIL", SKIP: "SKIP"}[rc]
        print(f"{label} {cid} {title}" + (f" — {detail}" if detail else ""))
        if rc == FAIL:
            failed.append(cid)
        elif rc == SKIP:
            skipped.append(cid)
    print()
    print(f"{counts[PASS]} passed, {counts[FAIL]} failed, {counts[SKIP]} skipped")
    if skipped:
        print(f"skipped: {', '.join(skipped)} — register each in TODO §6")
    if failed:
        print(f"failed:  {', '.join(failed)}")
        return FAIL
    return PASS


def run_self_test() -> int:
    failures = self_test_failures()
    if failures:
        for line in failures:
            print(f"FAIL self-test {line}")
        return FAIL
    print("PASS self-test exit-code contract (4/4 probes)")
    return PASS


# Directories whose .py files are not ours to gate: the HA submodule, the
# vendored integration, the runtime env, the packaged output.  A syntax error in
# any of them is not this project's finding.
COMPILE_SKIP = {"core", "vendor", ".venv", ".tools", "dist", ".git",
                "__pycache__", "node_modules"}


def compile_all() -> int:
    """Syntax-check every .py we own, in memory.

    Two roots: the repo (recursively, minus the vendored subtrees) and this gate
    directory.  When the gate sits inside the tree the two overlap, so targets
    are de-duplicated below.  Leaving the gate out would mean the harness never
    compiles itself.

    In memory, not ``py_compile``: that writes ``__pycache__`` next to each
    source, so a gate run would leave artifacts behind and the next run would
    have to clean them.  One implementation shared by check.sh and check.ps1 —
    two copies of this walker would drift exactly the way D16 exists to prevent.
    """
    targets: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if COMPILE_SKIP & set(rel.parts):
            continue
        targets.append(path)
    targets.extend(sorted(GATE_DIR.glob("*.py")))
    # Once the gate lives inside the tree, REPO_ROOT.rglob() already saw these
    # files; compile each source once instead of reporting a doubled count.
    targets = list(dict.fromkeys(targets))

    bad: list[str] = []
    for path in targets:
        label = (path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT)
                 else Path("gate") / path.name)
        try:
            compile(path.read_text(encoding="utf-8"), str(label), "exec")
        except SyntaxError as exc:
            bad.append(f"{label}:{exc.lineno}: {exc.msg}")
        except UnicodeDecodeError as exc:
            bad.append(f"{label}: not utf-8 ({exc})")
    for line in bad:
        print(f"FAIL compile {line}")
    if bad:
        return FAIL
    print(f"PASS compile {len(targets)} files (repo {REPO_ROOT.name} + gate)")
    return PASS


def run_pytest() -> int:
    """Run the whole gate test directory — step 2 of check.sh / check.ps1.

    One implementation for both shells: the uv discovery and the ``--with`` list
    live in ``pytest_cmd``, and a copy of that plumbing in each script is exactly
    the drift D16 exists to prevent.  Output is not captured — this is the step a
    human reads when a change is being called done.
    """
    cmd = pytest_cmd(str(GATE_DIR), "-q")
    if cmd is None:
        print("FAIL no pytest runner: neither .tools/uv nor an importable pytest")
        return FAIL
    return subprocess.run(cmd, cwd=REPO_ROOT).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Machine-checkable acceptance harness (TODO.md §0).",
        epilog="rc: 0 PASS / 1 FAIL / 2 SKIP",
    )
    parser.add_argument("case", nargs="?", help="case id, e.g. D05")
    parser.add_argument("--all", action="store_true", help="run every case")
    parser.add_argument("--list", action="store_true", help="list ids and titles")
    parser.add_argument("--self-test", action="store_true",
                        help="prove the exit-code contract with two probe cases")
    parser.add_argument("--compile", action="store_true",
                        help="syntax-check every .py we own (the gate's step 1)")
    parser.add_argument("--pytest", action="store_true",
                        help="run the whole test directory (the gate's step 2)")
    parser.add_argument("--expect-red", action="store_true",
                        help="expect FAIL (the defect is still there)")
    parser.add_argument("--live", action="store_true",
                        help="run the half of a case that needs a running instance")
    args = parser.parse_args(argv)

    global LIVE
    LIVE = args.live

    if args.list:
        return print_list()
    if args.compile:
        return compile_all()
    if args.pytest:
        return run_pytest()
    if args.self_test:
        return run_self_test()
    if args.all:
        if args.expect_red:
            parser.error("--expect-red applies to one case, not --all")
        return run_all()
    if not args.case:
        parser.print_help()
        return SKIP
    return run_one(args.case, args.expect_red)


if __name__ == "__main__":
    sys.exit(main())
