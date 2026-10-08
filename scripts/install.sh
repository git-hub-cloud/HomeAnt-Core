#!/usr/bin/env sh
# install.sh - HomeAnt online installer (Linux / macOS)
#
#   curl -LsSf https://dl.homeant.cn/core/install.sh | sh
#
# Online only. Already have the source? Use the homeant command instead
# of this script:
#   ./homeant start

set -eu

HOMEANT_VERSION="__HOMEANT_VERSION_PLACEHOLDER__"
HOMEANT_SERVER="${HOMEANT_SERVER:-dl.homeant.cn}"
ZIP_URL="https://${HOMEANT_SERVER}/core/HomeAnt-${HOMEANT_VERSION}.zip"
UV_INSTALL_URL="https://${HOMEANT_SERVER}/uv/0.12.23/uvinstall.sh"
CONNECT_TIMEOUT=15
TRANSFER_TIMEOUT=600

info() { printf '[install] %s\n' "$*"; }
# Colour only when the stream is a terminal — the same rule homeant applies
# to its own SUCCESS / PENDING / FAIL line. A redirected log, a CI capture or
# `| tee` therefore stays plain text on both sides and never carries escape
# bytes into a file (which used to happen: this script coloured unconditionally).
if [ -t 2 ]; then
    _c_red=$(printf '\033[0;31m'); _c_off=$(printf '\033[0m')
else
    _c_red=; _c_off=
fi
err()  { printf '%s[install]%s %s\n' "$_c_red" "$_c_off" "$*" >&2; }
# On POSIX the terminal (and its prompt, log) survives after this script exits,
# so there is nothing to keep open - the old "press any key to close window"
# was a Windows double-click idiom. Errors go to stderr and stay visible.
_fatal() { err "$1"; exit 1; }

# ============================================================
# preflight - refuse BEFORE anything is downloaded or overwritten
# ============================================================
# One entry per requirement, "<port>|<label>".  The MQTT broker this project
# will grow is one more entry here plus one more in install.ps1's
# $PreflightPorts - the two lists are meant to be the same list.
#
# The rule for every port is the same: free is fine, our own Home Assistant is
# fine (the install stops it further down), anybody else is a hard stop.  An
# installer that walks past a port it does not own produces the worst kind of
# failure - a green install and a service that cannot bind.
PREFLIGHT_PORTS="8123|Home Assistant HTTP"

# How long the stop below waits for hass to exit before SIGKILL.  The same
# number is $HassStopWaitSec in install.ps1: the two installers stop the same
# instance and must not disagree about when "still alive" starts being true.
HASS_STOP_WAIT_SEC=20

# Does a listening-socket table on stdin mention port $1?  Matched against the
# whole line rather than a fixed column: BSD netstat puts the local address in
# field 4 (dot separator), Windows netstat in field 2 - one pattern covers both.
# `[^0-9]|$` keeps :8123 from matching inside :81230.  Only the macOS/BSD
# fallback needs this now; Linux reads procfs directly.
_pf_table_has() {
    awk -v p="[:.]$1([^0-9]|\$)" 'tolower($0) ~ /listen/ && $0 ~ p { f = 1 } END { exit !f }'
}

# Linux procfs: is anything LISTENing on $port?  No external tool at all -
# /proc/net/tcp{,6} is a kernel file any user can read.  Column 2 is
# "addr:port" in hex, column 4 is the state, 0A = LISTEN.  procfs carries no
# owner pid, so a hit reports "unknown"; the caller then falls back to
# find_hass_pid, exactly as ss/netstat without root would.
_pf_proc_listening() {
    local hex f
    hex=$(printf '%04X' "$1")
    for f in /proc/net/tcp /proc/net/tcp6; do
        [ -r "$f" ] || continue
        if awk -v h="$hex" '
            FNR > 1 { split($2, a, ":"); if (toupper(a[2]) == h && $4 == "0A") { hit = 1; exit } }
            END { exit !hit }
        ' "$f"; then
            printf 'unknown'
            return 0
        fi
    done
    return 0
}

# Pids listening on $1, one per line.  Empty output = nothing listening.
# "unknown" = listening, but this box has no way to name the owner (procfs has
# no pid, and netstat -p needs root); the caller then falls back to "is our own
# HA alive".  Returns 3 when no listener source exists at all - a skipped check
# is honest, a guess is not.
#
# Linux answers from procfs, so the common case needs no lsof/ss/netstat at all
# (those are optional packages on many minimal spins - exactly the hosts this
# check exists for).  macOS/BSD have no /proc but ship lsof and netstat as part
# of the base system, so they stay zero-install there.
_pf_listeners() {
    if [ -r /proc/net/tcp ] || [ -r /proc/net/tcp6 ]; then
        _pf_proc_listening "$1"
        return 0
    fi
    if command -v lsof >/dev/null 2>&1; then
        # exit 1 means "no match", i.e. free - not an error
        lsof -nP -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null || true
        return 0
    fi
    if command -v netstat >/dev/null 2>&1; then
        if netstat -an 2>/dev/null | _pf_table_has "$1"; then
            printf 'unknown'
        fi
        return 0
    fi
    return 3
}

# Is $1 a hass that this project owns?  Image name narrows the search - the
# verdict comes from the command line carrying "--config" *and* this exact
# HA_ROOT, the same pair homeant uses to call a process its own.  `ps`
# rather than /proc so macOS can answer too.
_pf_is_our_hass() {
    local img cmd root="${HOME:-}/.homeant"
    img=$(ps -o comm= -p "$1" 2>/dev/null) || return 1
    case "$img" in
        *python*|*hass*) ;;
        *) return 1 ;;
    esac
    cmd=$(ps -o command= -p "$1" 2>/dev/null) || return 1
    case "$cmd" in
        *--config*) ;;
        *) return 1 ;;
    esac
    case "$cmd" in
        *"$root"*) return 0 ;;
    esac
    return 1
}

# The hass pid this install owns, or empty.  Shared verdict for preflight and
# for the stop below: one implementation, two readers.
#
# One record: HA's own HA_ROOT/.ha_run.lock ({"pid": N, ...}).  homeant no
# longer keeps a pid file of its own, so this is the same record its own `stop`
# reads - one authority, and no second copy left to disagree with it.  The ~8s
# a fresh hass needs before HA writes that lock is out of reach here: this
# script runs after a full unzip, and an instance interrupted mid-boot has
# already been killed by homeant's atexit cleanup.
#
# The lock lives inside HA_ROOT (~/.homeant), a directory no other tool ever
# runs HA against, so a pid found there is ours - provided the number still
# points at a hass-shaped process.  HA writes the file but never deletes it, so
# the number inside is only a claim: kill -0 answers "is *some* process holding
# this number", not "is it ours", and the stop takes the whole process group, so
# a recycled pid could take down an unrelated shell.  The kernel's name for the
# pid (/proc/<pid>/comm) is the cheap second opinion.
#
# A normal install reaches this with no lock file at all: HA has never run in
# this HA_ROOT, so only the processes can answer.  The image name only narrows
# the search - the verdict comes from the command line carrying "--config"
# *and* this exact HA_ROOT.  Killing on the name alone would take out any
# python script the user happens to be running, which is not a trade this
# script gets to make.
find_hass_pid() {
    local lock pid img dir cand cmd root
    pid=""
    lock="${HOME:-}/.homeant/.ha_run.lock"
    if [ -n "${HOME:-}" ] && [ -f "$lock" ]; then
        root="${HOME}/.homeant"
        pid=$(sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' \
            "$lock" 2>/dev/null | head -1) || pid=""
        if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
            pid=""
        else
            # The command line has to answer, exactly as it does in the scan
            # below and in homeant's _cmdline_is_hass.  comm only narrows:
            # it says "some python", and the stop below is a group kill, so a
            # recycled pid would take an unrelated shell with it.  Unreadable
            # cmdline (permissions, a dying process) falls through to the scan
            # rather than being trusted.
            cmd=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null) || cmd=""
            case "$cmd" in
                *--config*) ;;
                *) pid="" ;;
            esac
            case "$cmd" in
                *"$root"*) ;;
                *) pid="" ;;
            esac
        fi
    fi
    if [ -z "$pid" ] && [ -n "${HOME:-}" ] && [ -d /proc ]; then
        root="${HOME}/.homeant"
        for dir in /proc/[0-9]*; do
            cand="${dir#/proc/}"
            if [ "$cand" = "$$" ]; then
                continue
            fi
            img=$(cat "$dir/comm" 2>/dev/null) || continue
            case "$img" in
                python*|*hass*) ;;
                *) continue ;;
            esac
            cmd=$(tr '\0' ' ' <"$dir/cmdline" 2>/dev/null) || continue
            case "$cmd" in
                *--config*) ;;
                *) continue ;;
            esac
            case "$cmd" in
                *"$root"*)
                    pid="$cand"
                    break
                    ;;
            esac
        done
    fi
    printf '%s' "$pid"
}

preflight() {
    local entry port label out rc pid foreign ours _pf_ifs
    # One entry per line, so a label may contain spaces.  Split on IFS in a
    # `for` rather than in a `while read` pipeline: a pipeline runs the loop in
    # a subshell, where _fatal would exit only that subshell and preflight
    # would report success right after declaring the port unusable.
    _pf_ifs=$IFS
    IFS='
'
    for entry in $PREFLIGHT_PORTS; do
        port=${entry%%|*}
        label=${entry#*|}
        rc=0
        out=$(_pf_listeners "$port") || rc=$?
        if [ "$rc" -eq 3 ]; then
            info "preflight: no procfs/lsof/netstat - port $port ($label) not checked"
            continue
        fi
        if [ "$rc" -ne 0 ]; then
            info "preflight: port $port ($label) probe failed - not checked"
            continue
        fi
        if [ -z "$out" ]; then
            continue
        fi
        if [ "$out" = "unknown" ]; then
            # procfs/netstat without an owner pid: the port is taken, the name
            # is hidden.  Our own HA being alive is the only acceptable
            # explanation left.
            ours=$(find_hass_pid)
            if [ -n "$ours" ]; then
                info "preflight: port $port (PID $ours) - will be stopped before uv sync"
                continue
            fi
            _fatal "port $port ($label) is already in use and this installer cannot tell by which process - stop it and re-run. HomeAnt will not take a port it does not own."
        fi
        foreign=""
        for pid in $out; do
            if ! _pf_is_our_hass "$pid"; then
                foreign="$pid"
            fi
        done
        if [ -n "$foreign" ]; then
            _fatal "port $port ($label) is already in use by PID $foreign ($(ps -o comm= -p "$foreign" 2>/dev/null || echo unknown)) - stop it and re-run. HomeAnt will not take a port it does not own."
        fi
        # Every listener here is ours - a foreign one fataled just above - so the
        # first pid names the instance, same shape as the unknown-owner branch.
        for pid in $out; do break; done
        info "preflight: port $port (PID $pid) - will be stopped before uv sync"
    done
    IFS=$_pf_ifs

    # ── program checks (reserved) ───────────────────────────────────────────
    # Non-port requirements belong here: the MQTT broker binary a later release
    # ships, a kernel/OS feature HA needs, a minimum free-space budget.  Same
    # contract as the port loop above - say what is wrong and _fatal, never
    # warn-and-continue.  Nothing is added here until it has a real failure
    # mode to catch.
    #
    # The one such requirement today: the directory this zip is about to be
    # expanded into must be writable by whoever ran `curl | sh`.  Unchecked, a
    # read-only or root-owned target fails thousands of writes into the unzip
    # and surfaces as the extractor's own "Permission denied" - a message that
    # names neither the directory nor the fix.  One stat here replaces that.
    [ -w "$_here" ] || _fatal "install directory is not writable: $_here - change to a directory you own (e.g. cd ~) and re-run"
}

# --- upgrade mode: argv only, one spelling ---
# `--upgrade` repurposes this script: instead of refusing an existing tree it
# takes it as the overwrite target (the guard below is what changes in T6).
# Nothing else is accepted - no -Upgrade / -u alias, no HOMEANT_UPGRADE env -
# because every dispatch form we support can pass an argument
# (sh -s -- --upgrade), so a second channel would only add a way to get it wrong.
# Two POSIX traps avoided here:
#   * `if` instead of `[ "$_a" = ... ] && ...`: that one-liner returns 1 for the
#     last non-matching argument, and under `set -e` that aborts the script.
#   * `for _a in "$@"` instead of `$1`: under `set -u` an absent $1 aborts too.
UPGRADE_MODE=false
for _a in "$@"; do
    if [ "$_a" = "--upgrade" ]; then UPGRADE_MODE=true; fi
done
if [ "$UPGRADE_MODE" = true ]; then
    info "mode: upgrade"
else
    info "mode: fresh install"
fi

# --- local source present? hand it over to the homeant command ---
# install.sh is the online entry point. Running it inside a source tree would
# re-download the release zip and clobber local edits, so bail out early -
# EXCEPT in upgrade mode, where that same tree is exactly what we are asked to
# overwrite. Same tree test both ways, so the two branches cannot drift.
_here="$(pwd)"
# Auto-relocate when launched from the user's home directory: unpacking ~14k
# files straight into $HOME would litter it, so drop into ~/homeant instead.
# Fresh mode only — `--upgrade` must stay anchored to the tree it is pointed at,
# so it keeps fataling when run from home (that is not a HomeAnt tree).
if [ "$UPGRADE_MODE" = false ] && [ -n "${HOME:-}" ]; then
    _home_norm="$(cd "$HOME" 2>/dev/null && pwd)"
    if [ -n "$_home_norm" ] && [ "$_here" = "$_home_norm" ]; then
        _here="$HOME/homeant"
        # set -e alone would kill the script on a read-only or full $HOME with
        # no message whatsoever - `curl | sh` would just return 1 and the user
        # would have nothing to act on.  Both failures name the directory.
        mkdir -p "$_here" || _fatal "cannot create install directory: $_here"
        cd "$_here" || _fatal "cannot enter install directory: $_here"
        info "installing into $_here"
    fi
fi
_is_tree=false
if [ -f "$_here/pyproject.toml" ] && grep -qE '^name\s*=\s*"homeant' "$_here/pyproject.toml"; then
    _is_tree=true
fi

if [ "$UPGRADE_MODE" = true ]; then
    # --upgrade means "overwrite the tree you are standing in". Requiring that
    # it *is* one costs nothing and stops a mistyped command from unpacking
    # ~14k files into $HOME or /.
    if [ "$_is_tree" = false ]; then
        _fatal "upgrade target is not a HomeAnt tree: $_here"
    fi
elif [ "$_is_tree" = true ]; then
    info "HomeAnt source found in $_here"
    info "Run command:"
    info "  ./homeant start"
    exit 0
fi

# --- resolve version: stamped at package build time ---
# positive check ("is this a real version?") - never compare against the
# placeholder literal here, or the packager's global replace rewrites the guard too
# POSIX sh has no [[ ]] / =~, so validate the value with grep -E; `if !`
# keeps the guard out of errexit (a false test must not abort the good path).
if ! printf '%s\n' "$HOMEANT_VERSION" | grep -qE '^[0-9]+(\.[0-9]+)*$'; then
    # Reaching this means "not inside a source tree" (the guard above already
    # returned for that case) AND "no version stamped on me" - i.e. a copy of
    # this file was taken out of the source tree and run somewhere else.
    # No install-method menu here (removed 2026-10-26): the guard above already
    # tells a source-tree user to run ./homeant start, so listing it again
    # as "option 1" made the two branches say the same thing twice.
    # `exit 1` is the only way POSIX sh reports failure - a script that just
    # returns would hand the caller rc=0 and look successful.
    err "installer is not stamped with a version"
    exit 1
fi

# Before the 20 MB download and the 14k-file unzip: a port we cannot have is
# worth nothing to discover three minutes later.
preflight

# --- helper: extract zip (unzip -> bsdtar -> python3) ---
# unzip leads because it is the zip-native path, but it is NOT always there:
# macOS ships it and most desktop Linux too, yet minimal/server/container spins
# of many distros leave it out. bsdtar (libarchive) reads zip as well, and
# python3 is the last resort - present on nearly every Linux, and required
# anyway further down to run the project itself.
# GNU tar is deliberately absent: it cannot read zip at all (fails with
# "This does not look like a tar archive").
# Returns non-zero on failure instead of calling _fatal: extract_here runs the
# dot ticker in the background and has to stop it and close the line BEFORE
# anything is reported - a fatal exit from here would print the reason onto the
# same line as the dots. The reason is passed back through _zip_err (no `local`,
# so the caller can read it); the caller owns the fatal exit.
_zip_err=""
extract_zip() {
    local zip_path="$1" dest_dir="$2"
    _zip_err=""
    mkdir -p "$dest_dir"
    if command -v unzip >/dev/null 2>&1; then
        # Shape matches install.ps1's "zip extract failed: <zip> - <reason>": the
        # extractor's name is this side's reason, so it goes after the path, not
        # before it. Sharing the literal prefix is the point - a user searching
        # for the message finds the same one on either platform.
        (cd "$dest_dir" && unzip -q -o "$zip_path") \
            || { _zip_err="zip extract failed: $zip_path (unzip)"; return 1; }
    elif command -v tar >/dev/null 2>&1 && tar --version 2>/dev/null | grep -qiE 'bsdtar|libarchive'; then
        tar -xf "$zip_path" -C "$dest_dir" \
            || { _zip_err="zip extract failed: $zip_path (bsdtar)"; return 1; }
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
            "$zip_path" "$dest_dir" \
            || { _zip_err="zip extract failed: $zip_path (python3)"; return 1; }
    else
        _zip_err="need unzip, bsdtar (libarchive) or python3 to extract zip"
        return 1
    fi
}

# --- helper: download with a visible progress bar ---
# the transfer is the only step slow enough to look hung
# curl's own bar on purpose: it already reports total and speed,
# and a "[install]" prefix would fight its \r redraw. Do not "unify" this with
# the ps1 side - that difference is intended, not an oversight.
download() {
    curl -fL --progress-bar --connect-timeout "$CONNECT_TIMEOUT" \
        --max-time "$TRANSFER_TIMEOUT" -o "$2" "$1"
}

# --- helper: fetch a remote SCRIPT into memory and validate it ---
# Used for scripts we then hand to an interpreter (uvinstall.sh). POSIX sh has
# no `pipefail`, so `curl ... | sh` reports a broken download as success: curl
# exits 22, sh reads an empty body and exits 0. Fetching into a variable first
# fixes that (the assignment carries curl's own exit status), but one more case
# gets through, so both checks below are load-bearing:
#   ① curl -f  -> 4xx/5xx (rc 22)
#   ② [ -n ]   -> 200 with a zero-byte body (empty input still runs as a no-op)
# Not covered: 200 carrying a non-empty HTML error page / host default page.
# Deliberately not content-checked - a keyword match proves nothing about the
# script being correct, and it breaks the moment that copy is edited. TLS plus
# the two checks above are the accepted trade-off (same as uv/rustup/mise).
# Prints the script text on stdout; on failure prints the reason on stderr and
# returns 1 (never _fatal - the caller decides the message and the exit).
fetch_script() {   # $1=url  $2=label
    local s
    # $1 (the url), not $2 (the label): every other download failure on both
    # platforms reads "download failed: <url>". The label is kept for the
    # empty-body message below, where there is no url to blame.
    s=$(curl -fL -sS --connect-timeout "$CONNECT_TIMEOUT" --max-time "$TRANSFER_TIMEOUT" "$1") \
        || { err "download failed: $1"; return 1; }
    [ -n "$s" ] || { err "downloaded $2 is empty"; return 1; }
    printf '%s' "$s"
}

# --- helper: expand the release zip straight into a directory ---
# The zip is a flat mirror of the project root - pyproject.toml sits at its top
# level - so extracting into $dest_dir installs in place: no wrapper dir to
# lift, no staging, no cp. Overwrites in place, which is also what lets the
# repair branch below fill in a partial source tree.
extract_here() {
    local zip_path="$1" dest_dir="$2"
    local dot_pid=0 rc=0
    # Parity with install.ps1's _extract_here: the release zip has ~14k entries
    # and a silent unzip reads as a hang, so tick one dot every second while it
    # runs. The ticker is a background subshell; it is stopped before any exit
    # path is taken, otherwise it would keep printing dots into the terminal of
    # a `curl | sh` session that has already moved on.
    printf '[install] files extracting '
    ( while :; do printf '.'; sleep 1; done ) & dot_pid=$!
    extract_zip "$zip_path" "$dest_dir" || rc=$?
    kill "$dot_pid" 2>/dev/null || true
    wait "$dot_pid" 2>/dev/null || true
    printf '\n'
    [ "$rc" -eq 0 ] || _fatal "${_zip_err:-zip extract failed: $zip_path}"
    [ -f "$dest_dir/pyproject.toml" ] || _fatal "zip did not expand into $dest_dir - unexpected archive layout"
}

# ============================================================
# download HomeAnt source
# ============================================================
# a plain source tree never reaches here - the guard at the top hands it over to
# the homeant command instead. An `--upgrade` run does reach here, and
# $_here is then the tree being overwritten.
# No trailing " ...": install.ps1 passes the same string as _download's label and
# paints bytes right after it, so one side's ellipsis would be the other side's
# missing text. curl's own bar starts on the next line, which reads fine.
info "downloading HomeAnt-${HOMEANT_VERSION}"
# installs into the current directory
repo_root="$_here"

# straight into the system temp dir - no staging subdirectory. $$ keeps
# concurrent runs (two terminals, or a re-run while the first is stuck) off
# each other's file, and it means the first progress paint can never be a
# stale leftover from an earlier interrupted download.
tmp_zip="${TMPDIR:-/tmp}/HomeAnt-${HOMEANT_VERSION}.$$.zip"
# Every exit path has to take the release zip with it. install.ps1 gets this
# from `finally`; here _fatal calls exit(1), so a trailing `rm -f` after the
# failing call is unreachable - a failed extract would leave ~19 MB behind in
# /tmp. The trap covers success, every _fatal and Ctrl-C alike, and it follows
# the repair branch because that re-assigns the same variable.
trap 'if [ -n "${tmp_zip:-}" ]; then rm -f "$tmp_zip" || true; fi' EXIT

# The URL is part of the message because install.ps1's _download reports
# "download failed: <url>" - the two must say the same thing about the same
# event. The failure reason itself comes from curl's own stderr here (ps1 has to
# carry it in the message, since its Invoke-WebRequest error is swallowed by the
# job); information ends up equal, only the channel differs.
download "$ZIP_URL" "$tmp_zip" || { rm -f "$tmp_zip"; _fatal "download failed: $ZIP_URL"; }
extract_here "$tmp_zip" "$repo_root"
rm -f "$tmp_zip"

# ============================================================
# ensure uv is available
# ============================================================
# Priority: 1) project-local .tools/uv  2) system-wide uv on PATH
#           3) neither -> download into the project's .tools/uv
# The fallback install stays inside the project: UV_INSTALL_DIR points there,
# and uvinstall never touches PATH or writes a shell profile on its own.
# absolute path on purpose: repo_root comes from `pwd` at startup, so it stays
# valid even if a later step changes directory (the zip extract runs in a subshell).
if [ -x "$repo_root/.tools/uv/uv" ]; then
    uv_bin="$repo_root/.tools/uv/uv"
    info "uv in .tools: $uv_bin ($($uv_bin --version))"
elif command -v uv >/dev/null 2>&1; then
    uv_bin=$(command -v uv)
    info "uv on PATH: $uv_bin ($($uv_bin --version))"
else
    mkdir -p "$repo_root/.tools/uv"
    export UV_INSTALL_DIR="$repo_root/.tools/uv"
    # fetch_script validates BEFORE a single line runs: curl -f for 4xx/5xx and
    # a non-empty body. Without them the server answering with a 404 would be
    # piped into sh and read as a successful install.
    # All three failure modes say "uv install failed: <reason>", matching
    # install.ps1, whose catch reports "uv install failed: <exception>". "aborted"
    # was a third wording for the same event; the reason after the colon is what
    # makes the two platforms' messages equivalent.
    uv_script=$(fetch_script "$UV_INSTALL_URL" "uvinstall.sh") \
        || _fatal "uv install failed: could not fetch uvinstall.sh"
    printf '%s' "$uv_script" | sh || _fatal "uv install failed: uvinstall.sh exited $?"
    if [ -x "$repo_root/.tools/uv/uv" ]; then
        uv_bin="$repo_root/.tools/uv/uv"
    else
        _fatal "uv install failed - binary not found after download"
    fi
fi

# ============================================================
# auto-repair incomplete source (partial clone)
# ============================================================
# core/pyproject.toml matters as much as the package itself: core/ is linked as
# an editable path dependency, so a tree holding homeassistant/ but missing the
# package metadata makes `uv sync` fail with "does not appear to be a Python
# project". Both halves are checked, either one missing means re-extract.
if [ ! -f "core/pyproject.toml" ] || [ ! -f "core/homeassistant/__init__.py" ]; then
    info "core/ incomplete (homeassistant/ or pyproject.toml missing) - downloading full package to repair ..."
    # fresh extract into $repo_root itself merges over whatever is missing
    tmp_zip="${TMPDIR:-/tmp}/HomeAnt-${HOMEANT_VERSION}.$$.zip"
    # Not "repair download failed": install.ps1 routes this through the same
    # _download, so it reports the plain "download failed: <url>". The preceding
    # "core/ incomplete ... to repair ..." line already says which phase we are in.
    download "$ZIP_URL" "$tmp_zip" || { rm -f "$tmp_zip"; _fatal "download failed: $ZIP_URL"; }
    extract_here "$tmp_zip" "$repo_root"
    rm -f "$tmp_zip"
    if [ ! -f "core/pyproject.toml" ] || [ ! -f "core/homeassistant/__init__.py" ]; then
        _fatal "repair failed - core/ still incomplete after re-extract"
    fi
    info "source repaired from $ZIP_URL"
fi

if [ ! -d "vendor/ha_xiaomi_home/custom_components/xiaomi_home" ]; then
    info "vendor/ha_xiaomi_home not found - Xiaomi integration skipped"
fi

# ============================================================
# uv sync + homeant start
# ============================================================
# Stop a running Home Assistant - re-install must not race with a live process
# (the venv holds .venv/Scripts/hass.exe open, uv sync cannot replace it).
#
# *Which* pid that is comes from find_hass_pid() at the top of this file: the
# same verdict preflight uses to decide whether the process sitting on 8123 is
# ours.  Shared judgement, separate kill - `./homeant stop` would run on
# the very venv `uv sync` is about to replace (mid-upgrade), or on nothing at
# all (fresh install), so the one step that must not fail here stays free of
# Python entirely.
#
# The group kill (`-$pid`) is not decoration: start creates the session with
# start_new_session=True, so the pid is also the process-group id, and hass'
# workers live in that group. Plain `kill $pid` would leave them holding the
# venv.
_hass_pid=$(find_hass_pid)

if [ -n "$_hass_pid" ]; then
    info "Home Assistant running (PID $_hass_pid) - stopping before uv sync"
    kill -TERM "-$_hass_pid" 2>/dev/null || kill -TERM "$_hass_pid" 2>/dev/null || true
    # wait for it to really be gone: uv sync needs the file handles released,
    # and a half-dead hass still holds the recorder database
    _wait=0
    while kill -0 "$_hass_pid" 2>/dev/null && [ "$_wait" -lt "$HASS_STOP_WAIT_SEC" ]; do
        sleep 1
        _wait=$((_wait + 1))
    done
    if kill -0 "$_hass_pid" 2>/dev/null; then
        info "PID $_hass_pid still alive after ${HASS_STOP_WAIT_SEC}s - killing again"
        kill -KILL "-$_hass_pid" 2>/dev/null || kill -KILL "$_hass_pid" 2>/dev/null || true
        sleep 1
    fi
fi

# --inexact: `uv sync` must not prune what HA installed at runtime from
# integration manifests (frontend 235 MB, hassil, numpy, PyTurboJPEG ...).
# Those are not in uv.lock, so the default exact sync deletes them and the
# next boot spends ~30s putting 288 MB back. Versions covered by the lock
# are still aligned exactly - only extraneous packages are left alone.
info "uv sync ..."
"$uv_bin" sync --inexact --link-mode=copy || { _fatal "uv sync FAILED"; }
info "dependencies installed"

# a zip built on Windows drops the +x bit
chmod +x ./homeant 2>/dev/null || true

# ============================================================
# replace the integrations this release ships
# ============================================================
# ~/.homeant/custom_components is written by `homeant start`, and start only
# fills gaps: a missing target is copied, an existing one is merged — and a
# merge cannot rewrite a .py (only yaml/json have merge semantics).  A fixed
# integration would therefore never reach an install that already had one.
#
# Install is the step that owns versions, so it (a) drops the seed marker, the
# only thing that makes start re-run its deploy at all, and (b) removes the
# domains this release ships, so start has to *copy* them from the tree just
# unpacked instead of merging into what is already there.
#
# Deleting rather than copying over is deliberate: copying into an existing
# directory leaves behind files the release no longer ships, and HA keeps
# importing them.  Only domains named by the two shipped sources are removed —
# a component the user added by hand is not in that list and stays put.
if [ -n "${HOME:-}" ] && [ -d "${HOME}/.homeant/custom_components" ]; then
    rm -f "${HOME}/.homeant/.seed-state"
    for _domain_dir in config/custom_components/*/ \
                      vendor/ha_xiaomi_home/custom_components/*/; do
        [ -d "$_domain_dir" ] || continue
        _domain="$(basename "$_domain_dir")"
        case "$_domain" in
            _*) continue ;;  # private helper dirs are not HA domains
        esac
        [ -d "${HOME}/.homeant/custom_components/$_domain" ] || continue
        rm -rf -- "${HOME}/.homeant/custom_components/$_domain"
        info "replacing custom_components/$_domain"
    done
fi

info "homeant start ..."
# --no-install: this script has just unpacked the newest release and synced the
# venv, so the two things `start` would otherwise do first — probe the release
# host and hand back to this script — can only be wrong. Handing back is what would make
# the loop endless.
# Only zero / non-zero is inspected, on purpose. homeant prints its own
# SUCCESS / PENDING / FAIL line — the three-way distinction is for the reader —
# and folds SUCCESS and PENDING into the same code, so there is no state
# vocabulary to keep in sync between the two files. A step left for a human is
# not an install failure: the files are in place and the service is up, and
# re-running is the retry. Reporting it as FAILED is how a good install gets
# reinstalled in a loop that cannot end differently.
./homeant start --no-install || _fatal "FAILED"
