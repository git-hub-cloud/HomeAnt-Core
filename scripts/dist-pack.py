#!/usr/bin/env python3
"""dist-pack.py - HomeAnt one-click packager

Reads version from pyproject.toml and produces three kinds of artifact:

  dist/HomeAnt-{version}-{tag}.zip  source package, unpacked by the installer
                                    and consumed by `homeant start`.  `{tag}` is
                                    the first 8 hex of the archive's own sha256,
                                    so two builds of one version can coexist on
                                    the release host instead of overwriting.
  dist/install.sh / install.ps1     online installers, stamped with the version
                                    and the zip digest, uploaded to the release
                                    host under their fixed names
  dist/install-{version}-{tag}.{sh,ps1}
                                    the same bytes under an immutable name, so a
                                    release can be pinned and audited even after
                                    the fixed entry point moves on

The installers are NOT packed into the zip. install.* is the online entry point
fetched straight from the release host; the zip is the source package for
local use
(`./homeant start`). Nothing inside the zip ever runs install.*.

Before any of that, seven contracts shared with install.sh / install.ps1 /
homeant.py are compared, and packing is refused when they disagree — see
check_drift(). The installer trio cannot be exercised from here, so it is
checked instead: the download path, the archive name, the digest-prefix width
cut inside each installer, the temp-file name, the flag name, the version-stamp
variable and the `start` invocation are exactly the things that break silently
when one file is edited and its siblings are not.

Before packing, four things are reconciled in ONE run — never two invocations:

  1. constraints   the `constraint-dependencies` block in pyproject.toml is
                   regenerated from core/homeassistant/package_constraints.txt.
                   Idempotent: identical content means the file is not touched.
  2. pins          the homeassistant pin we hand-write is cross-checked against
                   core/const.py. HA's constraints file never pins Home Assistant
                   itself, so this comparison has no other backstop. The
                   integrations we pre-install need no such check: aiodiscover,
                   aiousbwatcher and serialx are also pinned in HA's constraints
                   file, so a stale copy contradicts the generated block and
                   `uv lock` fails outright. `mcp` is the one remaining gap —
                   mcp_server's requirement is not in that file, so a future
                   HA bump will only surface at boot.
  3. lock          `uv lock --check`, and `uv lock` only when it disagrees.
                   Packing a zip whose uv.lock contradicts pyproject.toml is
                   worse than not packing: the installer runs `uv sync`, sees
                   the mismatch, and silently re-resolves — exactly the version
                   drift the lock exists to prevent.
  4. output        dist/ is wiped right before writing, so the directory only
                   ever holds artifacts from THIS run. It is gitignored build
                   output; a stale zip carrying the same version number is
                   otherwise indistinguishable from a fresh one.

The zip itself is a function of the tree, not of the machine that packed it:
timestamps come from SOURCE_DATE_EPOCH — defaulting to the last commit's epoch,
and a tree with no commit is refused — entries are sorted, and mode bits are
written explicitly. Same sources ⇒ same bytes ⇒ same digest ⇒ same file name,
whether the tree was cloned today or last month, on Windows or on Linux.
See entry_epoch().

Usage:
    python scripts/dist-pack.py      # no arguments

    # to date the build by something other than HEAD:
    SOURCE_DATE_EPOCH=315532800 python scripts/dist-pack.py
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

# ---- online installers: stamped into dist/ for upload, never zipped ----
# Source lives in scripts/ at the repo root; only the basename is written to
# dist/ so the upload set stays flat (core/install.sh, core/install.ps1).
STAMP_INSTALL_SCRIPTS = ["scripts/install.ps1", "scripts/install.sh"]

# The literal both installers carry until this packager replaces it. Named once
# so the stamp and the drift check cannot disagree about what to look for.
VERSION_PLACEHOLDER = "__HOMEANT_VERSION_PLACEHOLDER__"

# Same idea for the digest of the zip these installers download. Stamped after
# the zip is final, never fetched at install time: a digest that travels inside
# the script cannot be replaced on the CDN independently of the zip, so the pair
# can never agree on the wrong bytes. The installers treat an unstamped literal
# as "could not check", never as a match — see their digest helpers.
ZIP_SHA256_PLACEHOLDER = "__HOMEANT_ZIP_SHA256_PLACEHOLDER__"

# The release file is NAMED after its own digest: `HomeAnt-<version>-<tag>.zip`.
# Two consequences, both wanted.  The URL is immutable - repacking one version
# publishes a different file instead of replacing one, so no CDN entry can hand
# yesterday's bytes to a client asking for today's - and the operator can tell
# two builds of one version apart without opening either.
#
# TAG_LEN = 8 hex = 32 bits, which is plenty for the number of builds one
# project will ever publish.  It stays 8 rather than 64 because the name is an
# identifier, not the check: the installers compare the downloaded bytes against
# the full 64 hex stamped into them, so the short form can be wrong in a URL and
# the download still stops at the checksum.  Keep it equal to `cut -c1-N` in
# install.sh and `Substring(0, N)` in install.ps1 - check_drift() refuses to
# pack when they disagree, because one hex char of difference 404s a platform.
TAG_LEN = 8
ARC_NAME = "HomeAnt-{version}-{tag}.zip"


def arc_name(version: str, digest: str) -> str:
    """Release file name for ``version``, tagged with ``digest``'s prefix."""
    return ARC_NAME.format(version=version, tag=digest[:TAG_LEN])


# The installers get the same treatment as the zip: the fixed name
# (`install.sh`) is the entry point documentation and `curl | sh` point at, and
# it has to keep moving; the versioned copy (`install-0.1.1-588ba06a.sh`) is the
# immutable record of what that release actually shipped.  Same bytes, two names
# — because "the installer changed" is the one failure the digest inside it
# cannot report: the script holding the checksum is exactly the script you would
# have to trust.
#
# `{tag}` is the same 8 hex as the zip in the same release (one `digest[:TAG_LEN]`
# feeds both), so a versioned installer pairs with the archive it installs by
# name, and a stale download is visible without opening either file.
VERSIONED_INSTALL = "install-{version}-{tag}{suffix}"


def versioned_name(version: str, digest: str, suffix: str) -> str:
    """Immutable copy name for ``version``, tagged like the zip."""
    return VERSIONED_INSTALL.format(
        version=version, tag=digest[:TAG_LEN], suffix=suffix)

# ---- what to include in the zip ----
INCLUDE_PATHS = [
    "homeant",
    "homeant.cmd",
    # Template only. The live homeant.cfg is generated by `start` and must
    # never be zipped: it carries passwords and per-machine paths, and shipping
    # it would let an upgrade overwrite the user's own settings.
    "homeant.cfg.default",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "TERMS.md",
    # Runtime file only. The rest of scripts/ is build tooling: dist-pack.py
    # never runs on a user's machine, and the two installers are upload
    # artifacts — they are stamped into dist/, not shipped inside the tree.
    "scripts/homeant.py",
    # uv bootstrap installers, one per platform. Shipped inside the zip so the
    # online installers never have to make a second homeant.cn request. The
    # names are FIXED -- no version in the path. The uv version lives only in
    # each file's embedded UVLOCK, so a bump replaces the file contents and
    # this pack list never changes. The rest of scripts/ is build tooling.
    "scripts/uvinstall.sh",
    "scripts/uvinstall.ps1",
    "config/",
    "core/homeassistant/",
    "core/pyproject.toml",
    # setuptools reads these while building the editable wheel for core/, and
    # core/pyproject.toml declares readme = "README.rst" plus
    # license-files = ["LICENSE*", ...] — omit them and the installer's
    # `uv sync` fails to resolve ./core.
    "core/README.rst",
    "core/LICENSE.md",
    "vendor/ha_xiaomi_home/custom_components/",
]

# things to strip inside included dirs
EXCLUDE_GLOBS = [
    "__pycache__/",
    "*.pyc",
    ".venv/",
    ".tools/",
    ".git/",
    ".github/",
    ".agent/",
    ".gemini/",
    ".vscode/",
    "tests/",
    "test/",
    "benchmarks/",
    "mypy_plugins/",
    "pylint/",
    "stubs/",
    "script/hassfest/",
    "script/scaffold/",
]

# ---- version reconciliation sources ----
# HA's own runtime pins, generated by gen_requirements_all.py inside core/.
CONSTRAINTS_REL = "core/homeassistant/package_constraints.txt"
HA_CONST_REL = "core/homeassistant/const.py"

# The `constraint-dependencies` block is machine-owned. These two markers make
# its extent unambiguous, so regeneration never has to guess where it starts
# (a regex anchored on the comment prose would silently stop matching the day
# someone reworded the comment).
GENERATED_BEGIN = "# >>> generated from core/homeassistant/package_constraints.txt"
GENERATED_END = "# <<< end generated"

# Integrations whose requirements HA installs at startup are pre-installed, so
# no in-flight install can replace a file the running process still holds
# (Windows: os error 5 on delete, which aborts the whole install). Their pins
# live in [project] dependencies; only homeassistant needs a drift check, since
# the other three are pinned in HA's constraints file as well.


def read_version(pyproject: Path) -> str:
    """Extract version from pyproject.toml [project] table."""
    text = pyproject.read_text(encoding="utf-8")
    m = re.search(r'(?m)^\s*version\s*=\s*"([^"]+)"', text)
    if not m:
        sys.exit("ERROR: version not found in pyproject.toml")
    return m.group(1)


def read_ha_version(repo_root: Path) -> str:
    """Read the HA version vendored in core/ (falls back to 'unknown')."""
    const = repo_root / HA_CONST_REL
    if not const.exists():
        return "unknown"
    text = const.read_text(encoding="utf-8")
    parts: list[str] = []
    for key in ("MAJOR", "MINOR", "PATCH"):
        m = re.search(rf'(?m)^{key}_VERSION[^=\n]*=\s*"?([0-9]+)"?', text)
        if not m:
            return "unknown"
        parts.append(m.group(1))
    return ".".join(parts)


def sync_constraints(repo_root: Path, ha_version: str) -> tuple[int, bool]:
    """Regenerate the constraint block from HA's constraint file.

    Returns (pin_count, changed). When nothing differs the file is not written
    at all, so an idempotent run leaves pyproject.toml byte-identical — no
    timestamp churn, no spurious diffs in the repo.
    """
    src = repo_root / CONSTRAINTS_REL
    if not src.exists():
        sys.exit(f"ERROR: {CONSTRAINTS_REL} not found")

    entries: list[str] = []
    for raw in src.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    if not entries:
        sys.exit(f"ERROR: no pins found in {CONSTRAINTS_REL}")

    new_block = "\n".join(
        [
            f"{GENERATED_BEGIN} (HA {ha_version}, {len(entries)} pins) — do not edit",
            "constraint-dependencies = [",
            *[f'    "{e}",' for e in entries],
            "]",
            GENERATED_END,
        ]
    )

    pyproject = repo_root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")

    begin = text.find(GENERATED_BEGIN)
    if begin >= 0:
        end = text.find(GENERATED_END, begin)
        if end < 0:
            sys.exit("ERROR: constraint block has a begin marker but no end marker")
        end += len(GENERATED_END)
        if text[begin:end] == new_block:
            return len(entries), False
        text = text[:begin] + new_block + text[end:]
    else:
        # First run after the block became machine-owned: adopt the hand-written
        # block together with the comment header directly above it, leaving
        # everything else in [tool.uv] untouched.
        m = re.search(r"(?ms)^constraint-dependencies = \[.*?^\]", text)
        if not m:
            sys.exit("ERROR: constraint-dependencies block not found in pyproject.toml")
        head = text[: m.start()].splitlines(keepends=True)
        while head and head[-1].lstrip().startswith("#"):
            head.pop()
        text = "".join(head) + new_block + text[m.end() :]

    pyproject.write_text(text, encoding="utf-8")
    return len(entries), True


def verify_pins(pyproject_text: str, ha_version: str) -> list[str]:
    """Cross-check the hand-written homeassistant pin against core/const.py.

    `uv lock` cannot catch this one: the pinned version simply exists, so the
    resolution succeeds and the drift only surfaces at boot, as a runtime
    install that replaces files the running process still holds. Nothing else
    compares these two numbers either — HA's constraints file pins every
    integration requirement but never Home Assistant itself.
    """
    m = re.search(r'"homeassistant==([^"]+)"', pyproject_text)
    if m and ha_version != "unknown" and m.group(1) != ha_version:
        return [
            f"homeassistant=={m.group(1)} pinned in pyproject.toml, "
            f"but core/ is {ha_version} ({HA_CONST_REL})"
        ]
    return []


# ---- drift checks: one contract, three files ----
# This script is the only place that has all three implementations in hand at
# once, so it is the only place that can refuse to ship when they disagree.
# Every check below is a static read: nothing is downloaded and no installer is
# run, so a mismatch costs no upload and never touches dist/.
#
# The comparisons are on *shapes*, not copies of a literal. Variable names are
# expected to differ per language ($HOMEANT_SERVER in sh, $HomeAntServer in
# ps1); what must match is everything a user would notice after it drifted —
# the download path, the archive name, the temp-file name, the flag name, and
# the `start` invocation the installers end with.

# ${VAR}, $PlainVar and {python_fstring} all collapse to {}; sh's $$ (its pid)
# is spelled differently from ps1's ${PID}, so it collapses to the same thing.
_PLACEHOLDER_RE = re.compile(r"\$\{[^}]+\}|\$[A-Za-z_][\w-]*|\{[^}]+\}")


def _shape(text: str) -> str:
    """Drop the language-specific variable spelling, keep the literal parts."""
    return _PLACEHOLDER_RE.sub("{}", text.replace("$$", "{}"))


def check_drift(repo_root: Path, version: str) -> list[str]:
    """Compare the contracts install.sh / install.ps1 / homeant.py share.

    Returns one message per disagreement, empty when everything lines up. A
    pattern that no longer matches is reported too: it means this checker went
    stale against a sibling, which is the same failure one step earlier.
    """
    sh = (repo_root / "scripts/install.sh").read_text(encoding="utf-8")
    ps1 = (repo_root / "scripts/install.ps1").read_text(encoding="utf-8")
    py = (repo_root / "scripts/homeant.py").read_text(encoding="utf-8")
    pyproject = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    problems: list[str] = []

    def grab(text: str, pattern: str, what: str) -> str | None:
        m = re.search(pattern, text)
        if not m:
            problems.append(f"{what}: not found — this checker is stale, update its pattern")
            return None
        return m.group(1)

    # 1. the release archive the two installers fetch must be the same URL...
    sh_url = grab(sh, r'(?m)^ZIP_URL="([^"]+)"', "install.sh ZIP_URL")
    ps1_url = grab(ps1, r'(?m)^\$ZipUrl\s*=\s*"([^"]+)"', "install.ps1 $ZipUrl")
    if sh_url and ps1_url and _shape(sh_url) != _shape(ps1_url):
        problems.append(
            "release archive URL differs:\n"
            f"        install.sh  {_shape(sh_url)}\n"
            f"        install.ps1 {_shape(ps1_url)}"
        )
    # ... and it must be the name this packager actually writes, or the upload
    # set is internally consistent but points at a file that never exists.  Both
    # interpolations collapse to {} under _shape, so this compares the literal
    # parts around them — version separator, tag separator, extension — and
    # nothing else.
    produced = ARC_NAME.format(version="{}", tag="{}")
    for label, url in (("install.sh", sh_url), ("install.ps1", ps1_url)):
        if url:
            fetched = _shape(url).rsplit("/", 1)[-1]
            if fetched != produced:
                problems.append(
                    f"{label}: fetches {fetched!r}, but this packager writes {produced!r}"
                )

    # 1b. the tag is cut by each installer and appended by this packager.  One
    #     hex char of disagreement is not cosmetic: the URL names a file and the
    #     host holds exactly one name per build, so the platform that cuts the
    #     wrong width 404s — on a user's machine, during an install, with a
    #     download error that says nothing about digests.  Nothing runs an
    #     installer at pack time, so this static comparison is the only place it
    #     can be caught.
    widths: dict[str, str] = {}
    for label, text, pattern in (
        ("install.sh", sh, r"(?m)^ZIP_TAG=.*cut -c1-(\d+)"),
        ("install.ps1", ps1, r"\$HOMEANT_ZIP_SHA256\.Substring\(0,\s*(\d+)\)"),
    ):
        m = re.search(pattern, text)
        if not m:
            problems.append(
                f"{label}: no digest-prefix width (`cut -c1-N` / `Substring(0, N)`)"
                f" — this checker is stale, update its pattern"
            )
        else:
            widths[label] = m.group(1)
    wrong = {k: v for k, v in widths.items() if v != str(TAG_LEN)}
    if wrong:
        problems.append(
            "release-name tag width differs: "
            + ", ".join(f"{k} cuts {v} hex" for k, v in sorted(wrong.items()))
            + f", this packager appends {TAG_LEN} — those URLs point at no file"
        )

    # 2. the temp file each installer downloads into. Same shape on both sides,
    #    and install.ps1 agrees with itself — it downloads in two places (main
    #    and the repair branch) and a rename in one of them leaves the other
    #    writing 19 MB into a name nothing cleans up.
    sh_tmp = grab(sh, r'(?m)^tmp_zip="([^"]+)"', "install.sh tmp_zip")
    ps1_tmps = re.findall(r'\$tmpZip = Join-Path \$env:TEMP "([^"]+)"', ps1)
    if not ps1_tmps:
        problems.append("install.ps1 $tmpZip: not found — this checker is stale, update its pattern")
    elif len({_shape(t) for t in ps1_tmps}) > 1:
        problems.append(
            "install.ps1: $tmpZip is set to different names in its two download "
            "branches — " + " / ".join(sorted({_shape(t) for t in ps1_tmps}))
        )
    if sh_tmp and ps1_tmps:
        sh_name = _shape(sh_tmp).rsplit("/", 1)[-1]
        ps1_name = _shape(ps1_tmps[0])
        if sh_name != ps1_name:
            problems.append(
                "temp zip name differs:\n"
                f"        install.sh  {sh_name}\n"
                f"        install.ps1 {ps1_name}"
            )

    # 3. how each installer finishes: `start --no-upgrade`, that flag and no
    #    other. The py side of the contract is check 4.
    for label, text, call in (
        ("install.sh", sh, "./homeant start --no-upgrade"),
        ("install.ps1", ps1, ".\\homeant start --no-upgrade"),
    ):
        if call not in text:
            problems.append(f"{label}: does not finish with `{call}`")
        # any other spelling means the two sides drifted, and a missing
        # --no-upgrade reopens the py -> installer -> py loop
        for m in re.finditer(r"homeant start ([-\w ]+)", text):
            if m.group(1).strip() != "--no-upgrade":
                problems.append(
                    f"{label}: finishes with `homeant start "
                    f"{m.group(1).strip()}` — the only flag is --no-upgrade")

    # 4. the py side of that contract: `start` is the single entry point, so the
    #    only switches it may register are the ones its callers set. A value
    #    flag would arrive as the string "false" — true in Python — and the
    #    switch would silently do nothing. `init` must not come back either:
    #    setup happens inside `start`, so a second entry point would drift.
    #
    #    `--foreground` is the supervisor contract (the unit's ExecStart,
    #    and the docker/terminal case); `--service-host` is the Windows
    #    SCM entry point.  Both are added straight on the parser.
    #
    #    The upgrade pair is different in kind: the two override the
    #    default in opposite directions, so they live in a mutually exclusive
    #    group and argparse — not the order of the `if`s in cmd_start — decides
    #    what a mixed command line means. The pair is named after the install,
    #    not the network: --no-upgrade is the installer's own tail, --reinstall
    #    is the user's override, and the default (probe the release host) needs
    #    no spelling at all.
    #    Registered through the group only; a fourth direct switch still has to
    #    be argued for here instead of slipping in.
    if "p_start.add_mutually_exclusive_group()" not in py:
        problems.append(
            "homeant.py: the upgrade switches are no longer a mutually "
            "exclusive group — `--no-upgrade --reinstall` would need an "
            "unwritten precedence rule"
        )
    direct = re.findall(
        r"p_start\.add_argument\(\s*\"(--[a-z-]+)\"\s*,\s*action=\"store_true\"",
        py,
    )
    if sorted(direct) != ["--foreground", "--service-host"]:
        problems.append(
            "homeant.py: `start` takes exactly two direct store_true switches — "
            f"--foreground and --service-host (found: {direct or 'none'})"
        )
    grouped = re.findall(
        r"net\.add_argument\(\s*\"(--[a-z-]+)\"\s*,\s*action=\"store_true\"",
        py,
    )
    if sorted(grouped) != ["--no-upgrade", "--reinstall"]:
        problems.append(
            "homeant.py: the mutually exclusive group registers exactly "
            "--no-upgrade and --reinstall, both store_true "
            f"(found: {grouped or 'none'} — this checker reads `net.`; if the "
            "group was renamed, update its pattern too)"
        )
    if re.search(r'add_parser\(\s*"init"', py):
        problems.append(
            "homeant.py: `init` is gone — setup runs inside `start`"
        )

    # 5. the upgrade handshake. py dispatches with --upgrade and both installers
    #    take that exact literal; py has two dispatch branches (Windows / unix)
    #    and losing one means a platform drops the flag.
    #
    #    A comparison, not a grammar. Neither script ever lands on disk (one is
    #    piped into `sh -s --`, the other is parsed in memory), so the only
    #    argv they see is the one homeant builds - a usage screen would be a
    #    promise to a caller that does not exist. What must not regress is the
    #    flag being recognised.
    if not re.search(r'\[\s*"\$_a"\s*=\s*"--upgrade"\s*\]', sh):
        problems.append('install.sh: no `[ "$_a" = "--upgrade" ]` comparison')
    if not re.search(r"-eq\s+'--upgrade'", ps1):
        problems.append("install.ps1: no `-eq '--upgrade'` comparison")
    m = re.search(r"(?ms)^def _run_installer\(.*?(?=^def |\Z)", py)
    body = m.group(0) if m else ""
    if body.count("--upgrade") < 2:
        problems.append(
            "homeant.py: --upgrade should be passed on both dispatch branches, "
            f"found {body.count('--upgrade')} in _run_installer()"
        )

    # 6. the version stamp. Both installers declare it under the same name and
    #    homeant.py reads the release version straight off that line, so a
    #    rename on one side turns the version check into a silent no-op: `start`
    #    would simply stop offering upgrades, with nothing in the log to say so.
    #    Checked by feeding each installer's own stamp line to py's regex, which
    #    tests the two halves together instead of only for presence: a brace or
    #    `$` that one side adds and the other does not shows up here. The line is
    #    completed with the version this run will stamp, so nothing invented ever
    #    appears in a message.
    m = re.search(r"""(?m)^VERSION_RE\s*=\s*r?(['"])(.+?)\1""", py)
    if not m:
        problems.append(
            "homeant.py: VERSION_RE is no longer a single-line literal — "
            "this checker is stale, update its pattern"
        )
    else:
        version_re = re.compile(m.group(2))
        for label, text in (("install.sh", sh), ("install.ps1", ps1)):
            # brace-tolerant on purpose: `$VAR` and `${VAR}` are both legal
            # PowerShell, and the disagreement is better reported by py's regex
            # below than as "no stamp line at all"
            line = re.search(r'(?m)^\$?\{?[\w-]*VERSION[\w-]*\}?\s*=\s*"[^"]*"', text)
            if not line:
                problems.append(f'{label}: no `..._VERSION = "..."` stamp line')
                continue
            sample = line.group(0).replace(VERSION_PLACEHOLDER, version)
            if not version_re.search(sample):
                problems.append(
                    f"{label}: py's VERSION_RE does not match the line this build "
                    f"would stamp ({sample}) — a release would read as having no version"
                )

    # 7. the two bootstrap scripts must be in the tree, and their embedded uv
    #    version must agree with the pin pyproject.toml gives HA at runtime.
    #    The file names are fixed; each file carries its own version inside its
    #    UVLOCK (the only place the installers read a version from). pyproject's
    #    `uv==` is a separate, HA-owned fact recorded in uv.lock. Nothing keeps
    #    the two in sync automatically, so a bump that updates one but not the
    #    other would install a uv the HA lock disagrees with — caught here,
    #    before dist/ is touched. A missing version block is reported too: no
    #    comparison can be claimed against a bootstrap whose version is gone.
    uv_pin = grab(pyproject, r'(?m)^\s*"uv==([^"]+)"', "pyproject.toml uv==")
    for name in ("uvinstall.sh", "uvinstall.ps1"):
        bootstrap = repo_root / "scripts" / name
        if not bootstrap.is_file():
            problems.append(
                f"scripts/{name} does not exist — the installer would look for "
                f"this bootstrap inside the zip and not find it"
            )
            continue
        embedded = re.search(
            r'name\s*=\s*"uv"\s*\r?\n\s*version\s*=\s*"([^"]+)"',
            bootstrap.read_text(encoding="utf-8"),
        )
        if not embedded:
            problems.append(
                f"scripts/{name}: no embedded uv version in its UVLOCK — "
                f"cannot compare against pyproject.toml's pin"
            )
        elif uv_pin and embedded.group(1) != uv_pin:
            problems.append(
                f"uv version differs: pyproject.toml={uv_pin}, "
                f"scripts/{name}={embedded.group(1)} — the bootstrap would "
                f"install a uv the HA lock disagrees with"
            )

    # 8. the digest literal in the source tree is a placeholder and nothing
    #    else. dist/ holds the stamped copy; scripts/ is what the next release
    #    will stamp. A real digest left here means someone edited the tree
    #    after packing, and the next release would ship a checksum describing
    #    the previous zip — caught before unpacking on a user's machine, but
    #    only if it never leaves this check.
    for label, value in (
        ("install.sh", grab(sh, r'(?m)^HOMEANT_ZIP_SHA256="([^"]*)"',
                            "install.sh HOMEANT_ZIP_SHA256")),
        ("install.ps1", grab(ps1, r'(?m)^\$HOMEANT_ZIP_SHA256\s*=\s*"([^"]*)"',
                             "install.ps1 $HOMEANT_ZIP_SHA256")),
    ):
        if value is not None and value != ZIP_SHA256_PLACEHOLDER:
            problems.append(
                f"{label}: digest constant is {value!r}, but the source tree "
                f"must carry {ZIP_SHA256_PLACEHOLDER} — stamping happens in "
                f"dist/, so any other value here ships a stale checksum"
            )

    # 9. the preflight port list, which exists twice and says so in both
    #    comments. Labels may differ (they are prose); the port set may not: a
    #    port checked on one platform and not the other is a green install
    #    followed by a service that cannot bind.
    sh_ports = grab(sh, r'(?m)^PREFLIGHT_PORTS="([^"]*)"',
                    "install.sh PREFLIGHT_PORTS")
    ps1_ports = re.findall(
        r'@\{\s*Port\s*=\s*(\d+)\s*;\s*Label\s*=\s*"[^"]*"\s*\}', ps1
    )
    if not ps1_ports:
        problems.append(
            "install.ps1 $PreflightPorts: no `@{ Port = 8123; Label = \"…\" }` "
            "entries — this checker is stale, update its pattern"
        )
    elif sh_ports is not None:
        # One entry per LINE, not per word: preflight() splits on a literal
        # newline precisely so a label may contain spaces, so splitting on
        # whitespace here would invent ports out of "Home Assistant HTTP".
        sh_set = {
            entry.split("|", 1)[0].strip()
            for entry in sh_ports.splitlines()
            if entry.strip()
        }
        ps1_set = set(ps1_ports)
        if sh_set != ps1_set:
            problems.append(
                "preflight port lists differ:\n"
                f"        install.sh  {sorted(sh_set)}\n"
                f"        install.ps1 {sorted(ps1_set)}"
            )

    # 10. the source-tree guard. A checkout must be handed to its own installer
    #     copy: the released one cannot tell a checkout from an unpacked release
    #     (both are "a HomeAnt tree with a version") and would unpack the zip
    #     over work that exists nowhere else. Both halves are load-bearing —
    #     `.git` is the discriminator (a release zip has none), `_local_installer`
    #     is the decision.
    if "_local_installer()" not in py or '".git"' not in py:
        problems.append(
            "homeant.py: the source-tree guard is gone — a checkout would be "
            "handed to the released installer, which unpacks over it "
            "(`_local_installer()` + the `.git` discriminator)"
        )

    # 11. one help screen. Every command is registered through `add(...)` with
    #     `add_help=False`; a second `sub.add_parser(` call brings back a page
    #     that only restates one line, and `homeant status --help` stops
    #     matching `homeant --help`.
    if "add_help=False" not in py or py.count("sub.add_parser(") != 1:
        problems.append(
            "homeant.py: subcommands no longer share one help screen — "
            "register them through `add(...)` with add_help=False "
            f"(sub.add_parser( found {py.count('sub.add_parser(')}x, want 1)"
        )

    return problems


def find_uv(repo_root: Path) -> Path | None:
    """Locate uv: project-local install first (install.* puts it in .tools/uv)."""
    for rel in (".tools/uv/uv.exe", ".tools/uv/uv", ".tools/uv/bin/uv"):
        candidate = repo_root / rel
        if candidate.exists():
            return candidate
    found = shutil.which("uv")
    return Path(found) if found else None


def ensure_lock(repo_root: Path, uv: Path | None) -> str:
    """Make uv.lock agree with pyproject.toml. Returns what happened.

    --check is a ~5 ms local comparison; `uv lock` only runs when it disagrees.
    A missing uv is a warning, not a failure: the shipped uv.lock is still
    valid input for `uv sync` at install time, it just may not be the newest
    possible resolution.
    """
    lock = repo_root / "uv.lock"
    if uv is None:
        return "skipped (uv not found)"

    if lock.exists():
        probe = subprocess.run(
            [str(uv), "lock", "--check"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0:
            return "up-to-date"

    run = subprocess.run(
        [str(uv), "lock"], cwd=repo_root, capture_output=True, text=True
    )
    if run.returncode != 0:
        detail = (run.stderr or run.stdout).strip()
        sys.exit(
            "ERROR: uv lock failed — pyproject.toml cannot be resolved.\n"
            "  Most likely: a pin disagrees with HA's own requirements, or core/\n"
            "  was bumped without updating pyproject.toml.\n"
            f"{detail}"
        )
    return "regenerated"


def clean_dist(out_dir: Path, repo_root: Path) -> None:
    """Wipe dist/ so it only holds this run's artifacts.

    Guarded on purpose: the whole point is deleting a directory, and a bad
    repo_root would take the project with it.
    """
    if out_dir.name != "dist" or out_dir.parent != repo_root:
        sys.exit(f"ERROR: refusing to clean unexpected path: {out_dir}")
    if out_dir.exists():
        print(f"  clean:   dist/")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)


def should_exclude(path: Path, root: Path) -> bool:
    """Check if a path should be excluded from the zip."""
    rel = path.relative_to(root).as_posix()
    for pat in EXCLUDE_GLOBS:
        if pat.endswith("/"):
            parts = rel.split("/")
            if pat[:-1] in parts:
                return True
        else:
            if path.match(pat) or rel.endswith(pat):
                return True
    return False


# ── a zip that depends on the tree and nothing else ─────────────────────────
#
# A zip entry carries a DOS date/time — two bytes of date, two of time — and
# that field has no timezone at all.  The value is a wall clock, so whoever
# extracts it has to guess: Windows reads it as local time, Linux unzip reads it
# as UTC.  Neither is wrong; the field simply cannot say "this instant".
#
# `zf.write()` fills it from each source file's mtime, which makes the whole
# archive a function of when the tree was checked out: the same commit cloned
# twice packs into two different zips, with two different names, and nothing to
# compare them by.  So this packer writes the timestamps itself, from one input:
#
#     SOURCE_DATE_EPOCH   epoch of the build, per reproducible-builds.  Unset,
#                         the epoch of the last commit is used — the same value
#                         `SOURCE_DATE_EPOCH=$(git log -1 --format=%ct)` gives
#                         by hand, so the default already reproduces
#
# `time.gmtime` is deliberate.  It ignores TZ, so a pack on a +08:00 laptop and
# on a UTC server write the same numbers.  `time.localtime` — what CPython's own
# SOURCE_DATE_EPOCH support uses — does not, which is why this is computed here
# rather than delegated to `zipfile`.
#
# Only those four bytes would be a half-fix.  Two more things leak the machine
# into the archive, and both are pinned below: entry order (the filesystem's,
# not the tree's — NTFS and ext4 disagree) and the mode bits (`zf.write()` takes
# them from the on-disk file, so 0644 under one umask and 0664 under another).
_ZIP_EPOCH_FLOOR = 315532800  # 1980-01-01T00:00:00Z, the earliest DOS date
_COMPRESS_LEVEL = 6


def _git_stamp(repo_root: Path) -> tuple[int, str] | None:
    """``(epoch, label)`` for HEAD, or None when there is no commit to ask.

    ``%ct`` is an epoch, not a formatted date: the committer's ``+08:00`` offset
    never reaches the archive.  That is the whole point — a formatted date would
    carry the offset, and two machines would disagree again.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1", "--format=%ct %h"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    parts = proc.stdout.split()
    if not parts:
        return None
    try:
        epoch = int(parts[0])
    except ValueError:
        return None
    short = parts[1] if len(parts) > 1 else "?"
    return epoch, f"last commit {short} (git log -1 --format=%ct)"


def entry_epoch(repo_root: Path) -> tuple[int, str]:
    """``(epoch, where it came from)`` for every entry's timestamp.

    ``SOURCE_DATE_EPOCH`` wins when set; otherwise the last commit decides, so a
    plain `python scripts/dist-pack.py` reproduces without the caller having to
    remember anything.

    A tree with no commit — not a repo, no Git installed, nothing committed yet
    — is refused rather than stamped with a made-up date: the archive would
    still install, but its name would claim a build time nobody chose, and the
    fix is one line.

    A value below the floor is clamped, not rejected: ``SOURCE_DATE_EPOCH=0`` is
    a legitimate way to say "as early as the format allows", while CPython's own
    support turns it into a ``struct.error``.
    """
    raw = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if raw:
        try:
            epoch = int(raw)
        except ValueError:
            sys.exit(f"ERROR: SOURCE_DATE_EPOCH={raw!r} is not an epoch")
        if epoch < _ZIP_EPOCH_FLOOR:
            return _ZIP_EPOCH_FLOOR, f"SOURCE_DATE_EPOCH={epoch} clamped to 1980-01-01Z"
        return epoch, f"SOURCE_DATE_EPOCH={epoch}"

    found = _git_stamp(repo_root)
    if found is None:
        sys.exit(
            "ERROR: no commit to date this build by, and SOURCE_DATE_EPOCH is unset.\n"
            "       Pack a git checkout, or name the epoch yourself:\n"
            "         SOURCE_DATE_EPOCH=<epoch> python scripts/dist-pack.py"
        )
    epoch, label = found
    if epoch < _ZIP_EPOCH_FLOOR:
        return _ZIP_EPOCH_FLOOR, f"{label} — clamped to 1980-01-01Z"
    return epoch, label


# the entry script must keep the executable bit inside the zip — the mode bits
# are written explicitly now (see make_zip), and on Windows the on-disk file has
# no +x to copy, which would break ./homeant for Linux/macOS users after unzip.
EXEC_ARCS = {"homeant"}


def collect_files(repo_root: Path) -> list[tuple[Path, str]]:
    """
    Walk INCLUDE_PATHS and produce list of (source_abs_path, zip_arcname).

    The archive is FLAT — its root IS the project root, pyproject.toml sits at
    the top level. Every consumer (install.ps1, install.sh, homeant
    update) expands it straight into place, so a wrapper directory would only
    force them to lift its contents one level afterwards; on Windows that lift
    is not even possible (the wrapper would be named homeant/ while the
    zip also holds a launcher file named homeant).

    Every arc stays relative to the repo root — never relative to the included
    dir's parent — which is what keeps core/, vendor/ha_xiaomi_home/custom_-
    components/, config/ and script/ exactly where the consumers resolve them.

    Sorted by arcname before returning, because `rglob` yields whatever order
    the filesystem hands back and NTFS and ext4 do not agree.  The archive has
    to be a function of the tree, so its entry order has to come from the tree
    too — otherwise two machines pack the same sources into different bytes for
    no reason anyone could act on.
    """
    files: list[tuple[Path, str]] = []

    for inc in INCLUDE_PATHS:
        p = repo_root / inc
        if not p.exists():
            print(f"  [skip] {inc} not found")
            continue

        if p.is_file():
            files.append((p, p.relative_to(repo_root).as_posix()))
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_dir():
                    continue
                if should_exclude(f, repo_root):
                    continue
                files.append((f, f.relative_to(repo_root).as_posix()))

    files.sort(key=lambda pair: pair[1])
    return files


def stamp_install_scripts(
    repo_root: Path, version: str, out_dir: Path, zip_sha256: str
) -> tuple[list[str], list[str]]:
    """
    Stamp install.ps1 / install.sh with the real version and the digest of the
    zip, then write them to out_dir for upload. They are never added to the zip,
    and that is exactly what lets the digest be stamped here: the bytes it
    describes are already final, so this step cannot invalidate them.

    Each script is written twice — under its fixed name and under an immutable
    `install-{version}-{tag}` name. The bytes are identical by construction (one
    string, two paths), which is the point: the fixed name is what `curl | sh`
    reaches and it has to keep moving; the versioned one is what a release can be
    held to after that name has moved on to the next build.

    Global replace is safe because each placeholder literal appears ONLY on its
    assignment line — the "was it stamped?" guards test the value's shape
    (^\\d+(\\.\\d+)*$ and 64 hex chars), so they contain no placeholder text to
    corrupt.

    Returns ``(fixed, pinned)`` — both lists of written basenames.
    """
    fixed: list[str] = []
    pinned: list[str] = []
    for rel in STAMP_INSTALL_SCRIPTS:
        src = repo_root / rel
        if not src.exists():
            print(f"  [skip] {rel} not found")
            continue
        text = src.read_text(encoding="utf-8")
        new_text = text.replace(VERSION_PLACEHOLDER, version).replace(
            ZIP_SHA256_PLACEHOLDER, zip_sha256
        )
        # newline="\n" on purpose: the default would translate LF to CRLF on
        # Windows, and a CRLF install.sh makes sh fail on Linux/macOS
        # (`]\r: command not found` from the very first `[ ... ]` test).
        # Only the basename lands in out_dir: the scripts live in scripts/ at
        # the repo root but are stamped into dist/ as a flat upload set.

        (out_dir / src.name).write_text(new_text, encoding="utf-8", newline="\n")
        fixed.append(src.name)
        # Same string, second path: the copy cannot drift from the entry point
        # because there is only ever one write of the stamped text.
        alias = versioned_name(version, zip_sha256, src.suffix)
        (out_dir / alias).write_text(new_text, encoding="utf-8", newline="\n")
        pinned.append(alias)
    return fixed, pinned


def make_zip(file_list: list[tuple[Path, str]], zip_path: Path,
             date_time: tuple[int, int, int, int, int, int]) -> None:
    """Write the archive, choosing every field that would otherwise come from
    the machine rather than from the tree.

    A ``ZipInfo`` is built per entry because ``zf.write()`` derives three things
    from the filesystem: the timestamp (source mtime), the permission bits
    (on-disk mode, i.e. the packer's umask) and nothing else we care about.  All
    three are set explicitly here — see the block comment above entry_epoch().
    """
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, arc in file_list:
            info = zipfile.ZipInfo(arc, date_time=date_time)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if arc in EXEC_ARCS else 0o644) << 16
            # ... and the level: on this write path zipfile reads it off the
            # ZipInfo, defaulting to zlib's own default.  ZipFile(compresslevel=)
            # is what ZipFile.write() copies onto the info it builds for itself;
            # naming the value here keeps the archive compressed the same way it
            # always was, instead of silently switching to -1.
            info._compresslevel = _COMPRESS_LEVEL
            with zf.open(info, "w") as dst, open(src, "rb") as handle:
                shutil.copyfileobj(handle, dst, 1024 * 1024)
        # Nothing is appended after the tree: an informational file in the zip
        # was a claim about the build with no reader (`packed` was the last
        # survivor), and every edit to it moved the entry count this packer
        # verifies.  The build's identity is the file NAME — version plus the
        # digest prefix — and everything else is the bytes themselves.


def verify_zip(zip_path: Path, expected_entries: int) -> None:
    """Read the archive back with the same reader that will consume it.

    A zip whose writer died before close() still holds every local header and
    every byte of file data — only the central directory and the trailing EOCD
    are missing, because zipfile serializes those in __exit__.  The result is a
    file that is a plausible size, starts with PK\\x03\\x04, and cannot be opened
    by anything.  That exact shape was shipped to the CDN once already, and no
    size or magic-number check would have caught it — the only cheap proof is
    to read it back:

      - ZipFile() succeeding  -> EOCD and central directory are present
      - testzip() returning None -> every entry's CRC matches its data
      - entry count matches -> nothing was dropped on the way in

    Raises SystemExit on any disagreement; the caller deletes the file.
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            bad = zf.testzip()
            got = len(zf.namelist())
    except zipfile.BadZipFile as exc:
        sys.exit(
            f"ERROR: {zip_path.name} is not a readable zip ({exc})\n"
            "       No central directory — the writer was interrupted or the\n"
            "       disk filled up. Nothing was renamed into place; pack again."
        )
    if bad is not None:
        sys.exit(f"ERROR: {zip_path.name} failed its CRC check on {bad}; pack again")
    if got != expected_entries:
        sys.exit(
            f"ERROR: {zip_path.name} holds {got} entries, expected {expected_entries}\n"
            "       The archive is short — pack again."
        )
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"  verify:  {got} entries, CRC OK, {size_mb:.1f} MB")


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    pyproject = repo_root / "pyproject.toml"

    version = read_version(pyproject)
    ha_version = read_ha_version(repo_root)
    out_dir = (repo_root / "dist").resolve()

    print(f"  version: {version}")
    print(f"  ha:      {ha_version}")
    print(f"  repo:    {repo_root}")
    print(f"  output:  {out_dir}")

    # 0. cross-file contracts — cheapest check of the run, and a failure here
    #    means the artifacts would be wrong, so it goes before anything is
    #    touched (including dist/).
    problems = check_drift(repo_root, version)
    if problems:
        sys.exit(
            "ERROR: installer drift — fix before packing:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )
    print("  drift:   OK (archive URL, tag width, temp name, start call, "
          "--upgrade, version var, uv bootstrap + pin, digest placeholder, port list)")

    # 1. reconcile the generated constraint block (no-op when unchanged)
    pins, changed = sync_constraints(repo_root, ha_version)
    state = "updated" if changed else "unchanged"
    print(f"  pins:    {pins} from {CONSTRAINTS_REL} — {state}")

    # 2. cross-check the hand-written homeassistant pin
    pyproject_text = pyproject.read_text(encoding="utf-8")
    problems = verify_pins(pyproject_text, ha_version)
    if problems:
        sys.exit(
            "ERROR: version pins disagree — fix before packing:\n"
            + "\n".join(f"  - {p}" for p in problems)
        )

    # 3. make uv.lock agree with pyproject.toml
    print(f"  lock:    {ensure_lock(repo_root, find_uv(repo_root))}")

    # 4. wipe dist/ so only this run's artifacts remain
    clean_dist(out_dir, repo_root)

    # 5. collect files
    file_list = collect_files(repo_root)
    print(f"  files:   {len(file_list)}")

    # 5b. the timestamp every entry will carry, printed with its source — so a
    #     date in `zipinfo` never has to be guessed at.  It comes from the last
    #     commit unless SOURCE_DATE_EPOCH says otherwise; `zipinfo` will show an
    #     even second, because the DOS field keeps seconds / 2.
    stamp, stamp_note = entry_epoch(repo_root)
    date_time = time.gmtime(stamp)[:6]
    print(f"  time:    {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(stamp))}"
          f" ({stamp_note})")

    # 6. build under a .tmp name and verify, then name and rename.
    #
    #    The .tmp name is not tidiness.  An interrupted pack leaves a zip with
    #    no central directory, and under the real name that file is a perfect
    #    stand-in for a release — right extension, right directory, plausible
    #    size — so the next upload ships it, which is exactly what happened
    #    with the 0.1.1 build.  Doing the rename last means the worst case is a
    #    stray .tmp that no upload step will pick up.  A SIGKILL cannot be
    #    caught, so the ordering buys more than the check does.
    #
    #    It also cannot be named yet: the name carries the digest, and the
    #    digest does not exist until the archive is complete.
    tmp_path = out_dir / f"HomeAnt-{version}.zip.tmp"
    try:
        make_zip(file_list, tmp_path, date_time)
        verify_zip(tmp_path, len(file_list))
    except BaseException:
        # SystemExit included: a failed check must not leave a .tmp behind for
        # someone to rename into place by hand.
        tmp_path.unlink(missing_ok=True)
        raise

    # 7b. digest, then name.  Hashing the .tmp is hashing the shipped bytes:
    #     verify_zip() has just proved the file complete — central directory
    #     readable, every CRC matching, the entry count exact — and a rename
    #     moves no bytes.  The digest is printed and stamped into the
    #     installers; it is never written beside the zip, because a
    #     `<zip>.sha256` is a second copy of a number no code reads and is free
    #     to drift from the stamped one.
    zip_sha256 = hashlib.sha256(tmp_path.read_bytes()).hexdigest()
    zip_path = out_dir / arc_name(version, zip_sha256)
    tmp_path.replace(zip_path)
    print(f"  zip:     {zip_path.name}")
    print(f"  sha256:  {zip_sha256}")

    # 7c. stamp the installers last, now carrying both the version and that
    #     digest, and write them into dist/ for upload — twice each, under the
    #     fixed name and the immutable one.  Safe at this point only because they
    #     are never inside the zip: stamping them cannot change the bytes just
    #     hashed, so the digest and the script still describe each other.
    fixed, pinned = stamp_install_scripts(
        repo_root, version, out_dir, zip_sha256)
    print(f"  install: {' '.join(fixed)}")
    print(f"  pinned:  {' '.join(pinned)}")

    # 8. summary
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print()
    print(f"Done! {size_mb:.1f} MB")
    print()
    print(f"Upload to server:")
    print(f"  core/{zip_path.name}")
    for name in fixed + pinned:
        print(f"  core/{name}")
    print()

if __name__ == "__main__":
    main()
