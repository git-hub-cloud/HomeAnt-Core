#!/usr/bin/env python3
"""Report which dependencies would need a *source build* on any target.

The single question this answers: "if `uv sync` runs on platform X with
interpreter 3.Y, which packages have no usable wheel and fall back to an
sdist?"  Building a pure-python sdist takes a second and needs no compiler;
building a package that *does* publish wheels, just none for this platform or
interpreter, is what turns an install into a ten-minute compile (measured
11-06: grpcio on macOS/3.15 stalled for minutes, macOS/3.14 took 9.3s).

Two inputs, with strictly separate jobs:

  * ``uv.lock`` (checked into git and consumed by ``uv sync`` in the
    installers) supplies the **pin set** - the exact name==version pairs this
    project installs - plus the PEP 508 markers on its dependency edges and
    the ``requires-python`` range that resolution was made under.  It supplies
    NO wheel list.  uv resolves under ``requires-python`` and writes down only
    the wheels it may install: measured 11-17, zeroconf 0.152.0 publishes 76
    wheels, the 3.14 lock records 20, and all 16 cp315 wheels are absent.
  * the project's PyPI index (``[[tool.uv.index]]`` in pyproject.toml,
    default https://mirrors.aliyun.com/pypi/simple) supplies the **file list**.
    Each pin's simple page is fetched once, unfiltered, and wheel tags plus
    ``data-requires-python`` are matched locally - the same unfiltered list uv
    itself resolves from.  149 packages / ~48 MB / ~6s cold, cached afterwards.

No uv process is started, no package is installed, nothing is inferred from
the running interpreter, and pyproject.toml is read only for the index URL.
``--python`` is required and is deliberately not guessed or read from
pyproject.toml.  A patch component may be passed (3.14.8) but is ignored -
wheel tags carry no patch component.

  python scripts/check-wheels.py --python 3.14
  python scripts/check-wheels.py --python 3.15

Asking about an interpreter outside the lock's ``requires-python`` is a
*hypothetical*: the wheels reported are real, but the pin set is still the
lock's own 3.14 resolution, so a WARNING is printed.  This was validated
against a real ``uv lock --python 3.15`` (uv 0.13.6, CPython 3.15.2, 1.5s,
151 packages): scanning the index for the 3.14 pin set and for the freshly
resolved 3.15 pin set yields *identical* BUILD sets on all five targets.  The
25 version bumps in that re-resolution do not change wheel availability.  A
real 3.15 install still requires bumping ``requires-python`` and ``uv lock``.

Every supported target is checked in one matrix - there is no --target flag,
because the interesting answer is *which* platform (if any) has no matching
wheel, not a single platform chosen by whoever ran the command.

Verdicts per package/target:
  OK      a wheel matches the target - nothing to build
  SDIST   no wheels published at all - pure-python build, no compiler
  BUILD   wheels exist but none fits - uv falls back to the sdist (alarm)
  local   path/editable dependency - always built, expected
  skip    not installed on this target (every requirer marks it foreign)
  UNKNOWN the index knows nothing about this pin, or it could not be fetched

BUILD is conservative, not proof a compiler runs: the index gives filenames
and Requires-Python, not what is inside the sdist.  A pure-python sdist needs
no compiler; a C/Rust/Cython one does.  Over-reporting is safe, silently
missing a real fallback is not.

Colors: compiler needed (BUILD/UNKNOWN) is red, no compiler needed (OK/SDIST
and the per-target conclusion) is green; local/skip are neutral.  Colors are
on by default - pass --no-color or set NO_COLOR when redirecting to a file.

Exit code: 1 when any third-party package lands in BUILD on *any* target;
2 when the report is incomplete (fetch failures / UNKNOWN pins).  So it
works as a release gate:
  python scripts/check-wheels.py --python 3.14 || exit 1

Platform filtering comes from the lock itself: uv records the PEP 508 marker on
every declared edge (dbus-fast -> "sys_platform == 'linux'"), so a package whose
requirers all mark it foreign is simply not installed here.  Edges carrying an
`extra ==` marker are treated as *relevant* on purpose - over-reporting is safe,
silently missing a real BUILD is not.  Marker evaluation uses the --python
version; no marker in this lock depends on the patch component.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FALLBACK_INDEX = "https://mirrors.aliyun.com/pypi/simple"
WORKERS = 16
CACHE = Path.home() / ".cache" / "homeant-check-wheels" / "index"

RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"

# target -> (wheel platform-tag families, machine alias)
TARGETS = {
    "macos-arm64": (("macosx", "universal2"), "arm64"),
    "macos-x86_64": (("macosx", "universal2"), "x86_64"),
    "linux-x86_64": (("manylinux", "musllinux", "linux"), "x86_64"),
    "linux-aarch64": (("manylinux", "musllinux", "linux"), "aarch64"),
    "win-amd64": (("win",), "amd64"),
}

ENV_FOR = {
    "macos-arm64": dict(sys_platform="darwin", platform_system="Darwin", os_name="posix", machine="arm64"),
    "macos-x86_64": dict(sys_platform="darwin", platform_system="Darwin", os_name="posix", machine="x86_64"),
    "linux-x86_64": dict(sys_platform="linux", platform_system="Linux", os_name="posix", machine="x86_64"),
    "linux-aarch64": dict(sys_platform="linux", platform_system="Linux", os_name="posix", machine="aarch64"),
    "win-amd64": dict(sys_platform="win32", platform_system="Windows", os_name="nt", machine="AMD64"),
}

ORDER = {"BUILD": 0, "UNKNOWN": 1, "SDIST": 2, "local": 3, "skip": 4, "OK": 5}
COLUMN = 15

LINK_RE = re.compile(r'<a\s+href="[^"]*"([^>]*)>([^<]*)</a>', re.I)
RP_RE = re.compile(r'data-requires-python="([^"]*)"', re.I)


# --------------------------------------------------------------------------- tags


def platform_ok(tag: str, target: str) -> bool:
    if tag == "any":
        return True
    families, arch = TARGETS[target]
    # A universal2 wheel is a fat binary: it fits BOTH macOS archs, not just
    # arm64.  bcrypt / pyobjc-core / grpcio publish only
    # macosx_*_universal2 wheels; treating those as arm64-only made macOS
    # x86_64 report 8 phantom BUILDs.
    if tag.endswith("_universal2") and target.startswith("macos"):
        return True
    if not tag.endswith("_" + arch) and not (arch == "amd64" and tag.endswith("_win_amd64")):
        return False
    return any(tag.startswith(f) for f in families)


def python_ok(pytag: str, minor: int) -> bool:
    for part in pytag.split("."):
        if part in ("py3", f"py3{minor}"):
            return True
        m = re.fullmatch(r"cp3(\d+)", part)
        if m and int(m.group(1)) == minor:
            return True
    return False


def abi3_ok(pytag: str, abitag: str, minor: int) -> bool:
    # compressed abi tags are legal too (cryptography ships "abi3.abi3t")
    if "abi3" not in abitag.split("."):
        return False
    for part in pytag.split("."):
        m = re.fullmatch(r"cp3(\d+)", part)
        if m and int(m.group(1)) <= minor:
            return True
    return False


def wheel_fits(filename: str, target: str, minor: int) -> bool:
    stem = filename[:-4] if filename.endswith(".whl") else filename
    parts = stem.split("-")
    if len(parts) < 5:
        return False
    pytag, abitag, plattag = parts[-3], parts[-2], parts[-1]
    # compressed tags ("macosx_10_9_x86_64.macosx_11_0_arm64") are legal
    if not any(platform_ok(t, target) for t in plattag.split(".")):
        return False
    return python_ok(pytag, minor) or abi3_ok(pytag, abitag, minor)


def os_family(tag: str) -> str:
    if tag == "any":
        return "any"
    if tag.startswith("macosx"):
        return "mac"
    if tag.startswith(("manylinux", "musllinux", "linux")):
        return "linux"
    if tag.startswith("win"):
        return "win"
    return "other"


def classify(wheels: list[str], sdist: bool, target: str, minor: int) -> str:
    """OK / BUILD / SDIST / skip, from the wheel filenames alone.

    ``BUILD`` means a wheel exists for this *OS* but none fits this
    interpreter or arch - uv then falls back to the sdist.  A package whose
    wheels are all for another OS is simply not installed here (pywin32 on
    macOS), so it gets ``skip`` instead of a false alarm - the lock carries no
    marker for it because its parent is a path dependency.
    """
    if not wheels:
        return "SDIST" if sdist else "BUILD"
    tags = [w[:-4].split("-")[-1] if w.endswith(".whl") else "" for w in wheels]
    if any(wheel_fits(w, target, minor) for w in wheels):
        return "OK"
    want = "mac" if target.startswith("macos") else "win" if target == "win-amd64" else "linux"
    if any(os_family(t) in (want, "any") for t in tags):
        return "BUILD"
    return "skip"


# ------------------------------------------------------------------ Requires-Python


def _rp_fallback(spec: str, version: str) -> bool:
    """Tiny PEP 440 subset for stdlib-only environments (no packaging)."""
    cur = tuple(int(x) for x in re.findall(r"\d+", version)[:3])
    cur += (0,) * (3 - len(cur))

    def clause(raw: str):
        m = re.fullmatch(r"\s*(===|==|!=|~=|<=|>=|<|>)\s*([0-9][0-9.*]*)", raw.strip())
        if not m:
            return None
        op, rhs = m.groups()
        wild = rhs.endswith(".*")
        base = rhs[:-2] if wild else rhs
        nums = tuple(int(x) for x in re.findall(r"\d+", base))
        n = len(nums)
        nums = (nums + (0, 0, 0))[:3]
        return op, nums, n, wild

    for raw in spec.split(","):
        c = clause(raw)
        if c is None:
            continue
        op, nums, n, wild = c
        if op in ("==", "!="):
            hit = cur[:n] == nums[:n] if wild else cur == nums
            if (op == "==" and not hit) or (op == "!=" and hit):
                return False
        elif op == ">=" and cur < nums:
            return False
        elif op == ">" and cur <= nums:
            return False
        elif op == "<=" and cur > nums:
            return False
        elif op == "<" and cur >= nums:
            return False
        elif op == "~=" and (cur < nums or cur[0] != nums[0] or (n > 1 and cur[1] != nums[1])):
            return False
        elif op == "===" and raw.strip()[3:] != version:
            return False
    return True


def requires_python_ok(spec: str, version: str) -> bool:
    """Whether this file's Requires-Python admits ``version``."""
    if not spec:
        return True
    try:
        from packaging.specifiers import SpecifierSet
    except ImportError:
        return _rp_fallback(spec, version)
    try:
        return SpecifierSet(spec).contains(version, prereleases=True)
    except Exception:
        return True  # unparseable specifier: keep the file (over-reporting is safe)


def python_version(minor: int) -> str:
    """Wheel tags carry no patch, so use the most permissive patch in the minor."""
    return f"3.{minor}.999"


# ------------------------------------------------------------------------- markers


def marker_true(marker: str | None, target: str, minor: int) -> bool:
    """True when the edge applies here.  Unknown / extra-marked edges stay on."""
    if not marker:
        return True
    if "extra" in marker:  # conservative: never filter on extras
        return True
    env = ENV_FOR[target]
    try:
        from packaging.markers import Marker
    except ImportError:  # no packaging -> cannot evaluate, keep the edge
        return True
    full = {
        "sys_platform": env["sys_platform"],
        "platform_system": env["platform_system"],
        "os_name": env["os_name"],
        "platform_machine": env["machine"],
        "python_version": f"3.{minor}",
        "python_full_version": f"3.{minor}.0",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "extra": "",
    }
    try:
        return bool(Marker(marker).evaluate(full))
    except Exception:
        return True


# ---------------------------------------------------------------------------- lock


def load_lock(path: Path) -> tuple[dict[str, dict], dict[str, list[str | None]], str]:
    """Pin set + dependency markers + requires-python.  Deliberately NO wheels.

    A lock's wheel list is the *filtered* subset uv may install; the index is
    the authority for which files actually exist, so it is not read here.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    packages: dict[str, dict] = {}
    edges: dict[str, list[str | None]] = {}
    for pkg in data.get("package", []):
        name = pkg["name"]
        src = pkg.get("source", {})
        local = any(k in src for k in ("editable", "directory", "virtual"))
        packages[name] = {"version": pkg.get("version"), "local": local}
        # uv records the platform marker on the edge itself - inline in
        # `dependencies` (uv >= 0.12) or in `metadata.requires-dist` on older
        # locks.  Both are read; a plain edge with neither is universal.
        declared: dict[str, list[str | None]] = {}
        for dep in (pkg.get("metadata", {}) or {}).get("requires-dist", []) or []:
            declared.setdefault(dep["name"], []).append(dep.get("marker"))
        for dep in pkg.get("dependencies", []):
            marker = dep.get("marker")
            if marker:
                edges.setdefault(dep["name"], []).append(marker)
            else:
                markers = declared.get(dep["name"])
                edges.setdefault(dep["name"], []).extend(markers if markers else [None])
    return packages, edges, data.get("requires-python", "")


def foreign_names(edges: dict[str, list[str | None]], target: str, minor: int) -> set[str]:
    """Names whose every incoming edge is marked foreign for ``target``.

    One level is enough: a package pulled in only by foreign-marked edges is
    itself absent on that platform, and the chain repeats package by package.
    Several markers on one name are alternatives, hence ``any``.
    """
    out = set()
    for name, markers in edges.items():
        if markers and not any(marker_true(m, target, minor) for m in markers):
            out.add(name)
    return out


# -------------------------------------------------------------------- index mode


def index_config() -> tuple[str, str]:
    """Read the configured simple index (config only - never a Python version)."""
    try:
        data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "index", FALLBACK_INDEX
    indexes = (data.get("tool", {}).get("uv", {}).get("index") or [])
    for idx in indexes:
        if idx.get("default") and idx.get("url"):
            return idx.get("name") or "index", idx["url"].rstrip("/")
    for idx in indexes:
        if idx.get("url"):
            return idx.get("name") or "index", idx["url"].rstrip("/")
    return "index", FALLBACK_INDEX


def normalize_name(name: str) -> str:
    """PEP 503: runs of -, _ and . collapse to a single dash."""
    return re.sub(r"[-_.]+", "-", name).lower()


def file_identity(filename: str) -> tuple[str, str] | None:
    """(distribution, version) from a wheel or sdist filename."""
    if filename.endswith(".whl"):
        parts = filename[:-4].split("-")
        # {dist}-{version}[-{build}]-{python}-{abi}-{platform}.whl
        return (parts[0], parts[1]) if len(parts) >= 5 else None
    for ext in (".tar.gz", ".zip"):
        if filename.endswith(ext):
            stem = filename[: -len(ext)]
            if "-" in stem:
                dist, ver = stem.rsplit("-", 1)
                return dist, ver
    return None


def version_eq(a: str, b: str) -> bool:
    if a == b:
        return True
    try:
        from packaging.version import Version
        return Version(a) == Version(b)
    except Exception:
        return a.lower().replace("-", "_") == b.lower().replace("-", "_")


def parse_index_page(page: str, name: str, ver: str) -> list[list[str]]:
    """All files published for this exact version: [filename, requires-python].

    The simple index is unfiltered - unlike uv.lock it lists every wheel for
    every Python and every platform.  Measured 11-17: 2969 wheels for the
    project's 149 pins vs 948 recorded in the 3.14 lock.
    """
    want = normalize_name(name)
    out: list[list[str]] = []
    for attrs, text in LINK_RE.findall(page):
        filename = html.unescape(text.strip())
        ident = file_identity(filename)
        if not ident:
            continue
        dist, file_ver = ident
        if normalize_name(dist) != want or not version_eq(file_ver, ver):
            continue
        m = RP_RE.search(attrs)
        out.append([filename, html.unescape(m.group(1)) if m else ""])
    return out


def scan_index(pin: tuple[str, str], index_url: str, refresh: bool):
    """(spec, files|None, cached, failed).  files is None on 404 or no match."""
    name, ver = pin
    spec = f"{name}=={ver}"
    cache = CACHE / f"{normalize_name(name)}-{ver}.json"
    if not refresh and cache.is_file():
        try:
            obj = json.loads(cache.read_text(encoding="utf-8"))
            if obj.get("index") == index_url:
                return spec, obj["files"], True, False
        except Exception:
            pass
    url = f"{index_url}/{normalize_name(name)}/"
    failed = False
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "homeant-check-wheels/1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                page = r.read().decode("utf-8", "replace")
            files = parse_index_page(page, name, ver)
            try:  # only the version-filtered subset is cached, not the 3 MB page
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({"index": index_url, "files": files}), encoding="utf-8")
            except OSError:
                pass
            return spec, files, False, False
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return spec, None, False, False
        except Exception:
            failed = True
    return spec, None, False, failed


# ------------------------------------------------------------------------- output


def paint(text: str, code: str | None, use_color: bool) -> str:
    return f"{code}{text}{RESET}" if use_color and code else text


def verdict_color(verdict: str) -> str | None:
    if verdict in ("BUILD", "UNKNOWN"):
        return RED
    if verdict in ("OK", "SDIST"):
        return GREEN
    return None  # local / skip are neutral


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lock", default=str(REPO / "uv.lock"),
                    help="lock supplying the pin set, platform markers and requires-python (no wheel list)")
    ap.add_argument("--python", required=True, help="interpreter to check, e.g. 3.14 or 3.15 (patch ignored)")
    ap.add_argument("--index", help=f"simple index URL (default: [[tool.uv.index]], else {FALLBACK_INDEX})")
    ap.add_argument("--refresh", action="store_true", help="ignore the local index cache")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colors (NO_COLOR is also honored)")
    args = ap.parse_args()

    m = re.fullmatch(r"3\.(\d+)(?:\.\d+)?", args.python)
    if not m:
        ap.error("--python must look like 3.14 or 3.14.8")
    minor = int(m.group(1))
    py_version = python_version(minor)
    targets = sorted(TARGETS)
    use_color = not args.no_color and "NO_COLOR" not in os.environ
    index_name, index_url = index_config()
    if args.index:
        index_name, index_url = "custom", args.index.rstrip("/")

    packages, edges, lock_rp = load_lock(Path(args.lock))
    foreign = {t: foreign_names(edges, t, minor) for t in targets}
    in_range = requires_python_ok(lock_rp, py_version)

    pins = [(n, i["version"]) for n, i in packages.items() if not i["local"]]
    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        results = list(pool.map(partial(scan_index, index_url=index_url, refresh=args.refresh), pins))
    elapsed = time.monotonic() - t0
    cached = sum(1 for _, _, hit, _ in results if hit)
    fetched = len(results) - cached
    failures = sum(1 for _, _, _, failed in results if failed)

    entries: dict[str, dict] = {}
    for spec, files, _, failed in results:
        wheels = ([f for f, rp in files if f.endswith(".whl") and requires_python_ok(rp, py_version)]
                  if files else [])
        entries[spec] = {
            "wheels": wheels,
            "total": sum(1 for f, _ in files if f.endswith(".whl")) if files else 0,
            "sdist": bool(files) and any(not f.endswith(".whl") for f, _ in files),
            "local": False,
            "known": bool(files) and not failed,
        }
    for name, info in packages.items():
        if info["local"]:
            entries[f"{name}=={info['version']}"] = {
                "wheels": [], "total": 0, "sdist": False, "local": True, "known": True,
            }
    source = (f"{index_name} {index_url} ({len(pins)} pins, {elapsed:.1f}s, "
              f"{cached} cached / {fetched} fetched)")

    def verdict_of(spec: str, entry: dict, target: str) -> str:
        name = spec.split("==")[0]
        if entry["local"]:
            return "local"
        if not entry["known"]:
            return "UNKNOWN"
        if name in foreign[target]:
            return "skip"
        return classify(entry["wheels"], entry["sdist"], target, minor)

    verdicts = {spec: {t: verdict_of(spec, e, t) for t in targets} for spec, e in entries.items()}

    print(f"cpython {args.python} | {len(entries)} packages | {len(targets)} targets | {source}\n")
    if not in_range:
        print(paint(
            f"WARNING: cpython {args.python} is outside uv.lock's requires-python ({lock_rp}).\n"
            "         Wheels below are real (scanned from the index), but the pin set is the "
            "lock's own resolution;\n"
            "         a real install on this interpreter requires bumping requires-python and `uv lock`.",
            YELLOW, use_color) + "\n")
    header = "  " + f"{'package':<34}" + f"{'whl':>4}  " + "".join(f"{t:<{COLUMN}}" for t in targets)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for spec in sorted(entries, key=lambda s: (min(ORDER[v] for v in verdicts[s].values()), s.lower())):
        cells = "".join(
            paint(f"{verdicts[spec][t]:<{COLUMN}}", verdict_color(verdicts[spec][t]), use_color)
            for t in targets
        )
        print(f"  {spec:<34}{entries[spec]['total']:>4}  {cells}")

    print("\nper-target:")
    bad_targets = []
    unknown = [s.split("==")[0] for s in entries if "UNKNOWN" in verdicts[s].values()]
    for t in targets:
        counts: dict[str, int] = {}
        for spec in entries:
            v = verdicts[spec][t]
            counts[v] = counts.get(v, 0) + 1
        summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: ORDER[kv[0]]))
        builds = [s.split("==")[0] for s in entries if verdicts[s][t] == "BUILD"]
        missing = [s.split("==")[0] for s in entries if verdicts[s][t] == "UNKNOWN"]
        if builds:
            bad_targets.append(t)
            conclusion = paint(f"sdist fallback (compiler likely): {', '.join(builds)}", RED, use_color)
        elif missing:
            conclusion = paint(f"incomplete: {len(missing)} pins unresolved", RED, use_color)
        else:
            conclusion = paint("no compiler needed", GREEN, use_color)
        print(f"  {t:<16} {summary:<44} {conclusion}")

    if unknown or failures:
        print("\n" + paint(
            f"INCOMPLETE: {len(unknown)} pins unknown, {failures} fetch failures "
            f"({', '.join(unknown[:8])}{'...' if len(unknown) > 8 else ''}). "
            "Re-run with network access; do not use this report as a gate.", RED, use_color))
        return 2
    if bad_targets:
        print("\n" + paint("BUILD = no matching wheel on " + ", ".join(bad_targets) + "; uv falls back to an sdist. Check these pins before shipping.", RED, use_color))
        return 1
    print("\n" + paint("no compiler needed on any target: every package installed here has a matching wheel (or a pure-python sdist).", GREEN, use_color))
    return 0


if __name__ == "__main__":
    sys.exit(main())
