"""D03 — reaping stale dns-sd children must not reach into another app.

The component starts ``dns-sd -P`` children on macOS and kills orphans left by a
hass that died badly (a surviving child keeps the instance name taken, so the
next run comes up as "HomeAnt (2)").  The selector is ``pgrep -f``, which matches
the *whole command line*, so the pattern is the only thing standing between our
cleanup and somebody else's broker announcement.

Two layers are asserted, because either one alone is a trap:

* the pattern must name an instance we would actually publish — a bare
  ``_(homeant|mqtt)\\._tcp`` matches every other tool's ``_mqtt._tcp``;
* the pid must be confirmed against its full command line before SIGTERM —
  ``pgrep`` also matches a ``tail -f`` on a log that merely quotes our argv, and
  killing that is a bug with no upside.

The regex is exercised with Python's ``re`` rather than by running ``pgrep``:
the constructs used here (alternation, escaped dot) mean the same thing in
POSIX ERE, and shelling out would make the criterion macOS-only.
"""

from __future__ import annotations

import inspect
import re

# What another app's broker announcement looks like on the same box.
FOREIGN = "dns-sd -P Other _mqtt._tcp local 1883 other.local 127.0.0.1"

# What this component starts: both names, both service types.
OWN = (
    "dns-sd -P homeassistant _homeant._tcp local 8123 homeassistant.local 192.168.1.5",
    "dns-sd -P HomeAnt _homeant._tcp local 8123 HomeAnt.local 192.168.1.5",
    # The broker keeps the box's instance name but rides on the alias host
    # record, because `homeassistant.local` is pinned to loopback.
    "/usr/bin/dns-sd -P homeassistant _mqtt._tcp local 1883 HomeAnt.local 192.168.1.5",
)


def _guard(mod):
    guard = getattr(mod, "_is_own_dns_sd", None)
    assert guard is not None, (
        "no confirmation step between pgrep and SIGTERM: the pattern alone has "
        "to decide, and pgrep -f matches any command line containing our argv"
    )
    return guard


def test_pattern_ignores_a_foreign_broker(component):
    mod = component("homeant_mdns")
    hit = re.search(mod._STALE_PATTERN, FOREIGN)
    assert hit is None, (
        f"pattern {mod._STALE_PATTERN!r} matches another app's {FOREIGN!r} "
        f"(hit {hit.group(0)!r}): the reaper would kill a process it never started"
    )


def test_pattern_still_matches_our_own_records(component):
    mod = component("homeant_mdns")
    missing = [cmd for cmd in OWN if re.search(mod._STALE_PATTERN, cmd) is None]
    assert not missing, (
        f"pattern {mod._STALE_PATTERN!r} no longer matches "
        f"{missing!r}: orphans of our own would survive"
    )


def test_guard_accepts_only_our_argv(component):
    guard = _guard(component("homeant_mdns"))
    assert all(guard(cmd) for cmd in OWN), f"guard rejects our own argv: {OWN!r}"


def test_guard_rejects_near_misses(component):
    guard = _guard(component("homeant_mdns"))
    bad = {
        "foreign instance": FOREIGN,
        "nested in a shell": f"tail -f /var/log/x {OWN[0]}",
        "right instance, other type": (
            "dns-sd -P homeassistant _http._tcp local 80 homeassistant.local 127.0.0.1"
        ),
        "other binary": "python -c print('dns-sd -P HomeAnt _homeant._tcp')",
        "empty": "",
        "not a dns-sd -P": "dns-sd -P",
    }
    accepted = {label: cmd for label, cmd in bad.items() if guard(cmd)}
    assert not accepted, f"guard would SIGTERM a process that is not ours: {accepted!r}"


def test_guard_is_wired_into_the_reaper(component):
    """A guard nobody calls looks like a fix and changes nothing."""
    mod = component("homeant_mdns")
    source = inspect.getsource(mod._async_reap_stale_dns_sd)
    assert "_is_own_dns_sd" in source, (
        "_async_reap_stale_dns_sd kills whatever pgrep returned; the guard is "
        "defined but never consulted"
    )
