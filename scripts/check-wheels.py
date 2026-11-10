#!/usr/bin/env python3
"""Report which dependencies would need a *source build* on a given target.

The single question this answers: "if `uv sync` runs on platform X with
interpreter 3.Y, which packages have no usable wheel and must be built from an
sdist?"  Building a pure-python sdist takes a second and needs no compiler;
building a package that *does* publish wheels, just none for this platform or
interpreter, is what turns an install into a ten-minute compile (measured
11-06: grpcio on macOS/3.15 stalled for minutes, macOS/3.14 took 9.3s).

Two inputs:

  # what we are about to ship - reads uv.lock, no network, no uv binary
  python scripts/check-wheels.py
  python scripts/check-wheels.py --target macos-arm64 --python 3.14

  # a candidate pin set, before it is locked (queries PyPI).  Use this to ask
  # "does a Home Assistant bump make my Mac compile?" before touching the tree:
  python scripts/check-wheels.py --requirements /tmp/constraints-2026.11.0.txt

Caveat for --requirements: a constraints file is the *whole* pin universe, not
what this project installs.  Measured 11-06: pymicro-vad==1.0.1 has no cp314
macOS wheel, yet it is not in the 121 packages `uv sync` installs - so the pin
list says "BUILD" while the real install compiles nothing.  Use --requirements
to spot risk cheaply, then confirm with the lock: bump, `uv lock`, run this
again without --requirements.

Verdicts per package:
  OK      a wheel matches the target - nothing to build
  SDIST   no wheels published at all - pure-python build, no compiler
  BUILD   wheels exist but none fits - compiler needed (this is the alarm)
  local   path/editable dependency - always built, expected
  skip    not installed on this target (every requirer marks it foreign)

Exit code is 1 when any third-party package lands in BUILD, so it works as a
release gate: `python scripts/check-wheels.py || exit 1`.

Platform filtering comes from the lock itself: uv records the PEP 508 marker on
every declared edge (dbus-fast -> "sys_platform == 'linux'"), so a package whose
requirers all mark it foreign is simply not installed here.  Edges carrying an
`extra ==` marker are treated as *relevant* on purpose - over-reporting is safe,
silently missing a real BUILD is not.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlparse

REPO = Path(__file__).resolve().parent.parent

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


# --------------------------------------------------------------------------- tags


def detect_target() -> str:
    import platform

    m = platform.machine().lower()
    arm = m in ("arm64", "aarch64")
    if sys.platform == "darwin":
        return "macos-arm64" if arm else "macos-x86_64"
    if sys.platform == "win32":
        return "win-amd64"
    return "linux-aarch64" if arm else "linux-x86_64"


def platform_ok(tag: str, target: str) -> bool:
    if tag == "any":
        return True
    families, arch = TARGETS[target]
    if arch == "arm64" and tag.endswith("_universal2"):
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
    if abitag != "abi3":
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
    interpreter or arch - that is the "compile on install" case (grpcio on
    macOS/3.15).  A package whose wheels are all for another OS is simply not
    installed here (pywin32 on macOS), so it gets ``skip`` instead of a false
    alarm - the lock carries no marker for it because its parent is a path
    dependency.
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


# ------------------------------------------------------------------------- markers


def marker_true(marker: str | None, target: str) -> bool:
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
        "python_version": "3.14",
        "python_full_version": "3.14.8",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "extra": "",
    }
    try:
        return bool(Marker(marker).evaluate(full))
    except Exception:
        return True


# ---------------------------------------------------------------------------- lock


def load_lock(path: Path) -> tuple[dict[str, dict], dict[str, list[str | None]]]:
    """packages: name -> {version, wheels, sdist, local}; edges: dep -> markers."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    packages: dict[str, dict] = {}
    edges: dict[str, list[str | None]] = {}
    for pkg in data.get("package", []):
        name = pkg["name"]
        src = pkg.get("source", {})
        local = any(k in src for k in ("editable", "directory", "virtual"))
        wheels = [unquote(urlparse(w["url"]).path.rsplit("/", 1)[-1]) for w in pkg.get("wheels", [])]
        packages[name] = {
            "version": pkg.get("version"),
            "wheels": wheels,
            "sdist": bool(pkg.get("sdist")),
            "local": local,
        }
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
    return packages, edges


def foreign_names(edges: dict[str, list[str | None]], target: str) -> set[str]:
    """Names whose every incoming edge is marked foreign for ``target``.

    One level is enough: a package pulled in only by foreign-marked edges is
    itself absent on that platform, and the chain repeats package by package.
    Several markers on one name are alternatives, hence ``any``.
    """
    out = set()
    for name, markers in edges.items():
        if markers and not any(marker_true(m, target) for m in markers):
            out.add(name)
    return out


# --------------------------------------------------------------------- pypi mode


def load_requirements(path: Path) -> list[tuple[str, str]]:
    pins = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name, _, ver = line.partition("==")
        ver = ver.split(";")[0].split(",")[0].strip()
        if name and ver:
            pins.append((name.strip(), ver))
    return pins


def fetch_files(pin: tuple[str, str]) -> tuple[str, list[str] | None]:
    import urllib.error

    name, ver = pin
    cache = Path.home() / ".cache" / "homeant-check-wheels" / f"{name}-{ver}.json"
    if cache.is_file():
        try:
            return f"{name}=={ver}", json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    url = f"https://pypi.org/pypi/{name}/{ver}/json"
    for _ in range(3):
        try:
            with urllib.request.urlopen(url, timeout=25) as r:
                files = [f["filename"] for f in json.load(r)["urls"]]
            try:  # ~100 lookups per run; caching keeps reruns instant
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(files), encoding="utf-8")
            except OSError:
                pass
            return f"{name}=={ver}", files
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return f"{name}=={ver}", None
        except Exception:
            pass
    return f"{name}=={ver}", None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lock", default=str(REPO / "uv.lock"))
    ap.add_argument("--requirements", help="name==version file checked against PyPI instead of the lock")
    ap.add_argument("--target", choices=sorted(TARGETS), help="target platform (default: this machine)")
    ap.add_argument("--python", default="3.14", help="interpreter minor, e.g. 3.14")
    ap.add_argument("--all", action="store_true", help="also list OK packages")
    args = ap.parse_args()

    target = args.target or detect_target()
    minor = int(args.python.split(".")[1])
    packages, edges = load_lock(Path(args.lock))
    foreign = foreign_names(edges, target)

    rows: list[tuple[str, str, str]] = []  # verdict, spec, note
    if args.requirements:
        pins = load_requirements(Path(args.requirements))
        print(f"pypi:    {len(pins)} pins from {args.requirements}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            fetched = list(pool.map(fetch_files, pins))
        for spec, files in fetched:
            name = spec.split("==")[0]
            if files is None:
                rows.append(("UNKNOWN", spec, "not on PyPI or version missing"))
                continue
            if name in foreign:
                rows.append(("skip", spec, "not installed on this target"))
                continue
            wheels = [f for f in files if f.endswith(".whl")]
            rows.append((classify(wheels, True, target, minor), spec, f"{len(wheels)} wheels"))
    else:
        for name, info in packages.items():
            spec = f"{name}=={info['version']}"
            if info["local"]:
                rows.append(("local", spec, "path/editable"))
            elif name in foreign:
                rows.append(("skip", spec, "not installed on this target"))
            else:
                rows.append((classify(info["wheels"], info["sdist"], target, minor), spec, f"{len(info['wheels'])} wheels"))

    order = {"BUILD": 0, "UNKNOWN": 1, "SDIST": 2, "local": 3, "skip": 4, "OK": 5}
    rows.sort(key=lambda r: (order[r[0]], r[1].lower()))
    counts: dict[str, int] = {}
    for verdict, _, _ in rows:
        counts[verdict] = counts.get(verdict, 0) + 1

    print(f"target:  {target} / cpython {args.python}  ({len(rows)} packages)\n")
    for verdict, spec, note in rows:
        if verdict == "OK" and not args.all:
            continue
        print(f"  {verdict:<8} {spec:<34} {note}")
    print("\n  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda kv: order[kv[0]])))

    if counts.get("BUILD"):
        print("\nBUILD = a compiler is required on this target. Do not ship this pin set as-is.")
        return 1
    print("\nno compiler needed: every package installed here has a wheel or is pure-python sdist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
