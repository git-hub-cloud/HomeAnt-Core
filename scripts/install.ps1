# install.ps1 - HomeAnt online installer (Windows)
#
#   irm https://dl.homeant.cn/core/install.ps1 | iex
#
# Online only. Already have the source? Use the homeant command instead
# of this script:
#   .\homeant start

# `irm ... | iex` evaluates this file in the caller's own session, so anything
# set here outlives the installer: a window that ran it would silently start
# treating non-terminating errors as fatal and stop drawing progress bars. Stash
# the caller's values now; the `finally` around main puts them back.
$__prevErrorAction = $ErrorActionPreference
$__prevProgress    = $ProgressPreference

$ErrorActionPreference = "Stop"
$ProgressPreference   = "SilentlyContinue"

# Same variable name as install.sh on purpose: homeant.py reads the release
# version straight out of this line, so one regex serves both platforms.
$HOMEANT_VERSION  = "__HOMEANT_VERSION_PLACEHOLDER__"
$HomeAntServer         = if ($env:HOMEANT_SERVER) { $env:HOMEANT_SERVER } else { "dl.homeant.cn" }
# sha256 of the release zip, computed and stamped in here by dist-pack.py at pack
# time.  Same reasoning as HOMEANT_ZIP_SHA256 in install.sh: the digest rides
# inside the script instead of being fetched beside the zip, so no second request
# and no CDN entry that can hand back a digest matching the stale zip it served
# with it.  Unstamped (run from the source tree) it stays a placeholder and
# _zip_digest_ok reports "could not check", never a match.
$HOMEANT_ZIP_SHA256    = "__HOMEANT_ZIP_SHA256_PLACEHOLDER__"
# The first 8 hex of that digest, cut from the stamped value rather than stamped
# again: two stored numbers can disagree, and that is how one platform ends up
# fetching a file name the release host never has.  Identifier only - the bytes
# are checked against the full 64 hex above, so a wrong prefix still stops at
# the checksum, never at a half-installed tree.  Width must equal TAG_LEN in
# dist-pack.py, and check_drift() refuses to pack when it does not.
$ZipTag                = $HOMEANT_ZIP_SHA256.Substring(0, 8)
$ZipUrl                = "https://$HomeAntServer/core/HomeAnt-${HOMEANT_VERSION}-${ZipTag}.zip"
# uv release bootstrapped by this installer. Same role as UV_VERSION in
# install.sh: the URL below and the CDN directory uv/<version>/ (uploaded
# separately, never inside the zip) derive from it. Keep it equal to the
# "uv==" pin in pyproject.toml.
$UvVersion             = "0.13.6"
$UvInstallUrl          = "https://$HomeAntServer/uv/${UvVersion}/uvinstall.ps1"
# $ConnectTimeoutSec     = 15
$TransferTimeoutSec    = 600
# How long _stop_hass waits for hass to exit before killing it again.  The same
# number is HASS_STOP_WAIT_SEC in install.sh: both installers stop the same
# instance, so "still alive" must start being true at the same moment.
$HassStopWaitSec       = 20
# The SCM registration this project installs.  Same name as SERVICE_NAME in
# homeant.py and as the unit name in install.sh: one service, one name, and the
# three files must agree on it.
$ServiceName           = "homeant"

function _info($m) { Write-Host "[install] $m" }
function _err($m)  { Write-Host "[install] $m" -ForegroundColor Red }
function _fatal($m) { _err $m; throw $m }

# Display form of a path: ~/rest when it sits under the profile, unchanged
# otherwise.  The same rule install.sh's _tilde and homeant.py's _home_tilde
# follow - forward slashes included - so a user moving between platforms, or
# between `homeant` and the installer, never sees their own tree spelled two
# ways.  Display only: the commands inside these messages keep absolute paths,
# which are the ones that work wherever the tree happens to sit.
function _Tilde([string]$Path) {
    $profileDir = $env:USERPROFILE
    if (-not $profileDir) { return $Path }
    $profileDir = $profileDir.TrimEnd('\', '/')
    if (-not $profileDir) { return $Path }
    if ($Path -ieq $profileDir) { return '~' }
    if ($Path.StartsWith("$profileDir\", 'OrdinalIgnoreCase') -or
        $Path.StartsWith("$profileDir/", 'OrdinalIgnoreCase')) {
        return '~/' + $Path.Substring($profileDir.Length + 1).Replace('\', '/')
    }
    return $Path
}

# --- banner ---------------------------------------------------------------
# The same two lines `homeant` opens with, before anything else, differing only
# in the tag (`[install]` here, `[homeant]` there): a pasted log names its own
# program while both sides still answer "which build?" without being asked. The version is the stamp;
# an unstamped copy (this tree's own installer) falls back to the
# pyproject.toml beside it - the field homeant.py::_local_version() reads, so
# the two sides cannot disagree. "unknown" only when neither is there.
#
# The dash is built from its code point instead of written literally: this file
# is ASCII on purpose, because Windows PowerShell 5.1 decodes a BOM-less script
# with the ANSI code page and would print mojibake where homeant prints a dash.
$BannerVersion = $HOMEANT_VERSION
if ($BannerVersion -notmatch '^[0-9]+(\.[0-9]+)*$') {
    $BannerVersion = "unknown"
    $BannerCands = @()
    if ($PSScriptRoot) { $BannerCands += (Split-Path $PSScriptRoot -Parent) }
    $BannerCands += (Get-Location).Path
    foreach ($cand in $BannerCands) {
        $proj = if ($cand) { Join-Path $cand "pyproject.toml" } else { $null }
        if ($proj -and (Test-Path $proj)) {
            $hit = Select-String -Path $proj -Pattern '^version\s*=\s*"([^"]+)"' |
                Select-Object -First 1
            if ($hit) { $BannerVersion = $hit.Matches[0].Groups[1].Value; break }
        }
    }
}
Write-Host ("[install] HomeAnt " + [char]0x2014 + " Control home through Agent.")
Write-Host "[install] version $BannerVersion"

# ============================================================
# preflight - refuse BEFORE anything is downloaded or overwritten
# ============================================================
# Same list and same rule as install.sh's PREFLIGHT_PORTS: free is fine, our
# own service is fine (it gets stopped further down), anybody else is a hard
# stop.  1883 is the broker, 8123 is hass; both are children of
# the supervised process, so Stop-Service frees both.  Keep the two lists
# equal - check_drift() in dist-pack.py compares their port sets at pack time.
$PreflightPorts = @(
    @{ Port = 8123; Label = "Home Assistant HTTP" },
    @{ Port = 1883; Label = "HomeAnt MQTT" }
)

function _is_our_hass_pid($procId) {
    # Image name narrows the search; the verdict comes from the command line
    # carrying "--config" *and* this exact HA_ROOT - the same pair homeant
    # uses to call a process its own.  A live python.exe is just as likely to be
    # somebody else's script, and that one must not be claimed *or* killed.
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    if ($p.Name -notmatch '^(python|hass)') { return $false }
    $cl = $p.CommandLine
    if (-not $cl) { return $false }
    if (-not $cl.Contains('--config')) { return $false }
    $root = (Join-Path $env:USERPROFILE '.homeant').Replace('\', '/')
    return ($cl.Replace('\', '/').IndexOf($root, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
}

function _is_our_broker_pid($procId) {
    # The second daemon in the service's process tree.  Its command line has
    # neither "--config" nor a hass.exe, so _is_our_hass_pid would call it a
    # stranger and an upgrade on a running install would hard-stop on 1883.
    # For example:
    #   python.exe -m amqtt.scripts.broker_script -c <HA_ROOT>/mqtt.yaml
    # The module name alone is not ownership: another amqtt broker on this
    # machine is somebody else's, so the config must also live under HA_ROOT.
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    if ($p.Name -notmatch '^python') { return $false }
    $cl = $p.CommandLine
    if (-not $cl) { return $false }
    if ($cl -notmatch '-m\s+amqtt\.scripts\.broker_script') { return $false }
    $root = (Join-Path $env:USERPROFILE '.homeant').Replace('\', '/')
    $flat = $cl.Replace('\', '/')
    return (($flat.IndexOf($root, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) -and ($flat -match 'mqtt\.ya?ml'))
}

function _preflight {
    param([string]$Dir)
    # Who holds each listening TCP port right now. netstat.exe is a plain core
    # Windows binary - not a PowerShell module that might fail to load - so there
    # is no "tool unavailable" state to branch on: if it cannot run, the failing
    # call throws and the install stops. Verified to match Get-NetTCPConnection
    # port-for-port (including IPv6) before this replaced it.
    $listen = @{}
    foreach ($line in (& netstat.exe -ano)) {
        $f = ($line.Trim() -split '\s+')
        if ($f.Count -ge 5 -and $f[0] -eq 'TCP' -and $f[3] -eq 'LISTENING' -and ($f[1] -match ':(\d+)$')) {
            $p = [int]$Matches[1]
            if (-not $listen.ContainsKey($p)) { $listen[$p] = [int]$f[-1] }
        }
    }
    foreach ($entry in $PreflightPorts) {
        if (-not $listen.ContainsKey($entry.Port)) { continue }   # free: nothing to say
        $owner = $listen[$entry.Port]
        if ((_is_our_hass_pid $owner) -or (_is_our_broker_pid $owner)) {
            # hass owns 8123, the broker owns 1883, and both die with the
            # service.  Worded exactly as install.sh says it: the two installers
            # answer to the same rule (keep their logic and their output in
            # step), and a platform that stays silent here would look like it
            # skipped a check.
            _info "preflight: port $($entry.Port) (PID $owner) - will be stopped before uv sync"
            continue
        }
        # Two statements, not `$who = try {...}`: try/catch is not an expression
        # in Windows PowerShell 5.1.
        $who = "unknown"
        try { $who = (Get-Process -Id $owner -ErrorAction Stop).ProcessName } catch { }
        _fatal "port $($entry.Port) ($($entry.Label)) is already in use by PID $owner ($who) - stop it and re-run. HomeAnt will not take a port it does not own."
    }

    # --- program checks (reserved) ---
    # Non-port requirements belong here: a new broker binary shipped in the zip,
    # a Windows feature HA needs, a free-space budget.  Same contract as
    # the port loop above: say what is wrong and _fatal, never warn-and-continue.
    # Nothing is added here until it has a real failure mode to catch.
    #
    # The one such requirement today: the directory this zip is about to expand
    # into must be writable.  Probed by creating and removing a file, because an
    # ACL query answers a different question - with an integrity level or a
    # parental deny in the way it reports "allowed" for an account that is then
    # refused at the first write, which is the exact failure this is meant to
    # replace with one clear line.
    $probe = Join-Path $Dir (".homeant-write-probe-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    try {
        [System.IO.File]::WriteAllText($probe, '')
        Remove-Item $probe -Force -ErrorAction SilentlyContinue
    } catch {
        _fatal "install directory is not writable: $(_Tilde $Dir) - change to a directory you own (e.g. cd ~) and re-run"
    }
}

# --- upgrade mode: argv only, one spelling ---
# `--upgrade` repurposes this script: instead of refusing an existing tree it
# takes it as the overwrite target (the guard in main() switches on it).
# Nothing else is accepted - no -Upgrade / -u alias, no HOMEANT_UPGRADE env -
# because every dispatch form we support can pass an argument
# (& $sb --upgrade, -File ... --upgrade); `irm ... | iex` cannot, and that is
# exactly the fresh-install case, so it needs none.
# Script scope on purpose: $args read inside main() would be main()'s own
# parameter list (empty), not the script's - the two are not the same variable.
#
# "Nothing else is accepted" is meant as a closed grammar, not as an error path.
# Unrecognised arguments are ignored, deliberately: this script never lands on
# disk - homeant fetches it and parses the text in memory (scriptblock::Create)
# before handing $args in - so the only argv it ever sees is the one homeant
# builds itself. A usage screen, a --help and an "unknown argument" refusal
# would serve a caller that does not exist; a usage screen is a promise.
# ASCII only, like the rest of this file: PS 5.1 decodes a BOM-less script as
# ANSI, so a curly quote here would arrive mangled.
$UpgradeMode = $false
foreach ($__a in $args) {
    if ($__a -eq '--upgrade') {
        $UpgradeMode = $true
    }
}

# Transfers are slow enough to look hung, but PowerShell's Write-Progress leaves
# post-completion glyphs on the line that can't be cleared reliably across hosts
# and terminals (PS 5.1 conhost and PS 7 / Windows Terminal both do). So: run the
# transfer in a background job with progress suppressed, and poll the file length
# from the foreground to rewrite "received MB" in place with a carriage return.
# No Write-Progress, no .NET WebClient, no curl, and no total-size probe: that
# probe cost a whole extra round trip and only ever bought a "x / y MB" readout.
#
# The timeout comes from $TransferTimeoutSec (top of script), never a literal here.
# Advanced function on purpose. A plain PowerShell function silently sweeps
# unrecognized -named arguments into $args, leaving the parameter empty and
# pushing the failure into the body - the uvinstall -path/$outFile mismatch
# surfaced as "failed to download from <url>" for every mirror before anyone
# spotted it. [CmdletBinding()] turns a wrong name into a bind-time error.
function _download {
    [CmdletBinding()]
    param($url, $outFile, $label)
    # Received-only progress on purpose - one request per download, see above.
    if ($label) { Write-Host "[install] $label" -NoNewline }
    $job = Start-Job -ArgumentList $url, $outFile, $TransferTimeoutSec -ScriptBlock {
        param($u, $o, $t)
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -UseBasicParsing -Uri $u -OutFile $o -TimeoutSec $t
    }
    # $drawn tracks the widest line written so far. "\r" only overwrites the
    # characters it re-prints, so a line shorter than its predecessor leaves a
    # tail behind (that is how "19.4MB" -> "2.6MB" produced a stray "B"). Any
    # shrink is right-padded to the previous width.
    $drawn = 0
    while ($job.State -eq 'Running') {
        Start-Sleep -Milliseconds 200
        $got = if (Test-Path $outFile) { (Get-Item $outFile).Length } else { 0 }
        $line = if ($label) { "[install] {0} {1:N1}MB" -f $label, ($got/1MB) }
                else        { "{0:N1}MB" -f ($got/1MB) }
        if ($line.Length -lt $drawn) { $line = $line.PadRight($drawn) } else { $drawn = $line.Length }
        Write-Host "`r$line" -NoNewline
    }
    Write-Host ""
    # $job.State on its own is not a failure signal: Invoke-WebRequest errors land
    # in the child job's error stream and the job still ends 'Completed' - measured
    # on PS 5.1, connection refused and HTTP 404 both read Completed with no file
    # written. Without the extra checks a failed transfer returns as a success and
    # the caller reports the wrong reason ("Cannot find path ...") instead.
    $jobErr = @()
    if ($job.ChildJobs.Count -gt 0) { $jobErr = @($job.ChildJobs[0].Error) }
    $failed = ($job.State -ne 'Completed') -or
              ($jobErr.Count -gt 0) -or
              -not (Test-Path $outFile)
    # No SilentlyContinue here: it silences the only useful text when the transfer
    # failed. Continue merely keeps the replay from throwing under the caller's
    # $ErrorActionPreference = Stop.
    $err = @(Receive-Job $job -ErrorAction Continue 2>&1)
    Remove-Job $job -Force
    if ($failed) { throw "download failed: $url - $($err -join '; ')" }
}
# 0 = match, 1 = mismatch, 2 = could not check. 2 is deliberately not an error:
# an installer run straight from the source tree still carries the placeholder,
# and a host without the cmdlet must still be able to install. Neither becomes a
# pass - "verified" and "unchecked" are different facts and the caller says
# which one it got.
#
# Catches: a transfer that changed the bytes, and a zip that is not the release
# this script was packed for (e.g. only the zip got refreshed on the CDN).
# Cannot catch: a digest and a zip that were both wrong when this script was
# packed - nothing inside a release can outvote its own build. That needs a
# read-back after upload, on the publishing side.
#
# Get-FileHash ships with PS 4+, so it is always there; it is still probed rather
# than assumed, because a missing cmdlet must degrade to "unchecked" instead of
# aborting an install half way with module-not-found.
function _zip_digest_ok($zipPath) {
    if (-not (Get-Command Get-FileHash -ErrorAction SilentlyContinue)) { return 2 }
    $want = $HOMEANT_ZIP_SHA256
    if (-not ($want -match '^[0-9a-fA-F]{64}$')) { return 2 }
    try {
        $got = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
    } catch {
        return 2
    }
    # Get-FileHash reports upper case, the stamp is lower case; PS -eq ignores
    # case anyway, normalised so a reader does not have to know that.
    if ($got -eq $want.ToLower()) { return 0 } else { return 1 }
}

# The zip is a flat mirror of the project root - pyproject.toml sits at its
# top level - so expanding into $destDir installs in place: no wrapper dir to
# lift, no temp staging, no copy step. Overwrites in place, which is also what
# lets the repair branch below fill in a partial source tree.
#
# Extraction is .NET (System.IO.Compression.ZipFile), not Expand-Archive: the
# latter lives in the Microsoft.PowerShell.Archive module, and this script has
# to run under `irm ... | iex` on hosts where an optional module may not load -
# the same reason the port check avoids NetTCPIP. ZipFile ships inside the .NET
# Framework the PowerShell host already runs on, so it is the zero-dependency
# extractor here; uvinstall.ps1 unpacks its wheel exactly the same way.
#
# It still cannot report progress, and on the 14k-entry release zip it runs for
# minutes - a silent console that reads as a hang. So it runs out of process and
# this side ticks one dot per second while the job is live. Nothing about the
# archive is inspected; this only says "still working".
function _extract_here($zipPath, $destDir) {
    $zipPath = (Resolve-Path -LiteralPath $zipPath).Path
    $destDir = [System.IO.Path]::GetFullPath($destDir)
    Write-Host "[install] files extracting " -NoNewline
    $job = Start-Job -ArgumentList $zipPath, $destDir -ScriptBlock {
        param($z, $d)
        $ProgressPreference = 'SilentlyContinue'
        Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
        $zip = [System.IO.Compression.ZipFile]::OpenRead($z)
        try {
            foreach ($e in $zip.Entries) {
                $target = [System.IO.Path]::Combine($d, $e.FullName)
                if ([string]::IsNullOrEmpty($e.Name)) {
                    # directory entry - create it, nothing to write
                    $null = [System.IO.Directory]::CreateDirectory($target)
                } else {
                    $parent = [System.IO.Path]::GetDirectoryName($target)
                    if ($parent) { $null = [System.IO.Directory]::CreateDirectory($parent) }
                    [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true)
                }
            }
        } finally {
            $zip.Dispose()
        }
    }
    while ($job.State -eq 'Running') {
        Write-Host "." -NoNewline
        $null = Wait-Job $job -Timeout 1
    }
    Write-Host ""
    $failed = $job.State -ne 'Completed'
    $err = @(Receive-Job $job -ErrorAction SilentlyContinue 2>&1)
    Remove-Job $job -Force
    # throw, not _fatal: both call sites sit inside a try whose catch owns the
    # reporting. _fatal here would print the reason and then get caught and
    # printed a second time by that catch - two red lines for one failure.
    # Same reasoning as install.sh's extract_zip, which returns a code and
    # leaves the fatal exit to extract_here.
    if ($failed) {
        throw "zip extract failed: $zipPath - $($err -join '; ')"
    }
    if (-not (Test-Path (Join-Path $destDir "pyproject.toml"))) {
        throw "zip did not expand into $(_Tilde $destDir) - unexpected archive layout"
    }
}

function _is_admin {
    # Is this process elevated?  Only used to *say* what is missing: the
    # installer never elevates itself, because a UAC dialog cannot be answered
    # by an unattended run and a re-launched install would change which account
    # owns the files.
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function _stop_service {
    # The SCM is the only thing that starts hass on Windows, so one
    # Stop-Service replaces the pid hunt whenever a registration exists.
    # Deliberately "stop" and not "set to demand start": an upgrade must not
    # change whether this home comes back after a reboot.
    #
    # Returns $true when a registration exists - running or not - so the caller
    # knows the pid tier below has nothing left to do.  $false means no
    # registration, which is either a fresh install or a tree from an older build
    # whose hass was spawned directly; that second case still needs the hunt.
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if (-not $svc) { return $false }
    if ($svc.Status -eq 'Stopped') {
        _info "homeant service is stopped"
        return $true
    }
    # Worded exactly as install.sh says it: same rule, same report.
    _info "homeant service is running - stopping it before uv sync"
    try {
        Stop-Service -Name $ServiceName -Force -ErrorAction Stop
    } catch {
        _info "could not stop the homeant service - uv sync may fail"
        return $true
    }
    try { $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds($HassStopWaitSec)) } catch {
        # WaitForStatus throws on timeout; the status check below is the verdict.
    }
    if ((Get-Service -Name $ServiceName -ErrorAction SilentlyContinue).Status -ne 'Stopped') {
        _info "service still active after ${HassStopWaitSec}s - uv sync may fail"
    } else {
        _info "service stopped"
    }
    return $true
}

function _stop_hass {
    # Tier 1: the service.  A registration means the SCM owns hass, and killing
    # that process behind the manager's back would look like a crash and pull it
    # straight back into the tree uv sync is replacing.
    if (_stop_service) { return }

    # Tier 2 (older trees only): a hass that no SCM knows about, started
    # directly by a build that had no service.  Deleted together with the pid
    # tier it belongs to once `start` refuses to leave anything unsupervised.
    #
    # Stop the Home Assistant tree that this install owns, before uv sync.
    #
    # One record: HA's own HA_ROOT\.ha_run.lock ({"pid": N, ...}).  homeant
    # no longer keeps a pid file of its own, so this is the same record its own
    # `stop` reads - one authority, and no second copy left to disagree with it.
    # The ~8s a fresh hass needs before HA writes that lock is out of reach
    # here: this script runs after a full unzip, and an instance interrupted
    # mid-boot has already been killed by homeant's atexit cleanup.
    #
    # The lock lives inside HA_ROOT (~/.homeant), a directory no other tool ever
    # runs HA against, so a pid found there is ours - provided the number still
    # points at a hass-shaped process, which is what the ProcessName check below
    # adds.  Deliberately no command-line pattern fallback: matching on "hass"
    # hits ANY process whose argv merely contains the word - a second checkout,
    # an editor with the path in it, an oh-my-posh theme called hassy - and it
    # breaks that boundary.
    #
    # Shared judgement, separate kill: `.\homeant stop` would run on the
    # very venv `uv sync` is about to replace - mid-upgrade, or absent on a
    # fresh install - so the one step that must not fail here stays free of
    # Python entirely.
    #
    # Tree, not process: the lock records the python child (os.getpid() inside
    # runner.py), not the uv trampoline in .venv\Scripts\hass.exe that spawned
    # it.  The trampoline exits on its own once the child is gone, but anything
    # the child spawned still holds the venv, which is why children are walked
    # with CIM and stopped too.
    $rootPid = 0
    # HA writes this file but never deletes it, so the number inside is only a
    # claim.  A live pid is not the same as *our* live pid: the number may have
    # been recycled onto an unrelated process, and the stop below is a hard kill
    # of that pid and its children.
    #
    # The verdict is therefore _is_our_hass_pid - the same rule the port
    # preflight uses and the same one homeant applies in
    # _cmdline_is_hass: image name, AND "--config", AND this exact HA_ROOT.
    # Matching the image name alone was a hole with a live demo on this machine:
    # two unrelated python.exe processes running a desktop script passed
    # '^(python|hass)$' and would have been killed together with their trees -
    # and so would the homeant process that is executing this very
    # upgrade, which is also a python.exe.  A pid that fails the check is not
    # trusted at all and the scan below answers instead: refusing here costs
    # one CIM lookup, never a missed HA.
    $lockFile = Join-Path $env:USERPROFILE ".homeant\.ha_run.lock"
    if (Test-Path $lockFile) {
        try {
            $lockInfo = Get-Content $lockFile -Raw -ErrorAction Stop | ConvertFrom-Json
            $lockPid = [int]$lockInfo.pid
            if ($lockPid -gt 0 -and (_is_our_hass_pid $lockPid)) {
                $rootPid = $lockPid
            }
        } catch { }
    }

    if ($rootPid -eq 0) {
        # A normal install still has no lock file: HA has never run in this
        # HA_ROOT, so only the processes can answer.  The image name narrows the
        # search; the verdict comes from the command line carrying "--config"
        # *and* this exact HA_ROOT, because a live python.exe is just as likely
        # to be someone else's script and must not be touched.
        $haRoot = Join-Path $env:USERPROFILE '.homeant'
        $haRootNorm = $haRoot.Replace('\', '/')
        $cands = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='hass.exe'" -ErrorAction SilentlyContinue |
            Where-Object {
                $cl = $_.CommandLine
                ($cl) -and $cl.Contains('--config') -and
                    ($cl.Replace('\', '/').IndexOf($haRootNorm, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
            })
        if ($cands.Count -gt 0) {
            $candIds = @($cands | ForEach-Object { [int]$_.ProcessId })
            # Topmost first: stopping the launcher takes its python child with
            # it.  If the parent is not among the candidates (started from a
            # shell that has since exited), the child is stopped instead and the
            # launcher follows on its own.
            $top = @($cands | Where-Object { $candIds -notcontains [int]$_.ParentProcessId })
            if ($top.Count -gt 0) {
                $rootPid = [int]$top[0].ProcessId
            } else {
                $rootPid = [int]$cands[0].ProcessId
            }
        }
    }

    if ($rootPid -gt 0) {
        _info "Home Assistant running (PID $rootPid) - stopping before uv sync"
        $all = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
        $ids = @($rootPid)
        $frontier = @($rootPid)
        while ($frontier.Count -gt 0) {
            $kids = @($all |
                Where-Object { $frontier -contains [int]$_.ParentProcessId } |
                ForEach-Object { [int]$_.ProcessId } |
                Where-Object { $ids -notcontains $_ })
            if ($kids.Count -eq 0) { break }
            $ids += $kids
            $frontier = $kids
        }
        foreach ($procId in $ids) {
            try { Stop-Process -Id $procId -Force -ErrorAction Stop } catch { }
        }
        # wait for it to really be gone: uv sync needs the file handles released
        $waited = 0
        # 500ms ticks, so the second count is twice the tick count.
        while ((Get-Process -Id $rootPid -ErrorAction SilentlyContinue) -and $waited -lt ($HassStopWaitSec * 2)) {
            Start-Sleep -Milliseconds 500
            $waited++
        }
        if (Get-Process -Id $rootPid -ErrorAction SilentlyContinue) {
            _info "PID $rootPid still alive after ${HassStopWaitSec}s - killing again"
            try { Stop-Process -Id $rootPid -Force -ErrorAction Stop } catch { }
        }
        Start-Sleep -Milliseconds 300
        return
    }
}

# ============================================================
# replace the integrations this tree ships
# ============================================================
# ~/.homeant/custom_components is written by `homeant start`, and start only
# fills gaps: a missing target is copied, an existing one is merged - and a
# merge cannot rewrite a .py (only yaml/json have merge semantics).  A fixed
# integration would therefore never reach a deployment that already had one.
#
# Two callers, one rule: the published install (bottom of main()) and
# `--upgrade` inside a source tree.  Both end in `homeant start`, so both need
# the same "remove what this tree ships, then let start copy it in" step - and
# both must remove exactly the same set, which is why the scan lives here once
# instead of being written twice and drifting.
#
# Deleting rather than copying over is deliberate: copying into an existing
# directory leaves behind files the tree no longer ships, and HA keeps
# importing them.  Only domains named by the two shipped sources are removed -
# a component the user added by hand is not in that list and stays put.
#
# $Root is the tree to read the domain names from; the target is always
# $env:USERPROFILE\.homeant\custom_components.  Same text as install.sh.
function _ReplaceShippedComponents([string]$Root) {
    $haCc = Join-Path $env:USERPROFILE ".homeant\custom_components"
    if (-not (Test-Path $haCc)) { return }
    # Drop the seed marker too: it is the other thing `start` consults before
    # deciding that its deploy already ran.
    $seedMarker = Join-Path $env:USERPROFILE ".homeant\.seed-state"
    if (Test-Path $seedMarker) {
        Remove-Item $seedMarker -Force -ErrorAction SilentlyContinue
    }
    $ccSources = @(
        (Join-Path $Root "config\custom_components"),
        (Join-Path $Root "vendor\ha_xiaomi_home\custom_components")
    )
    foreach ($ccSrc in $ccSources) {
        if (-not (Test-Path $ccSrc)) { continue }
        foreach ($ccDir in (Get-ChildItem -Path $ccSrc -Directory)) {
            if ($ccDir.Name.StartsWith('_')) { continue }
            $ccTarget = Join-Path $haCc $ccDir.Name
            if (Test-Path $ccTarget) {
                Remove-Item $ccTarget -Recurse -Force
                _info "deleting custom_components/$($ccDir.Name)"
            }
        }
    }
}

# `--upgrade` in an unstamped tree.  There is no version to download - the
# packager stamps a *copy* - but "bring the deployment up to date with the tree
# you are standing in" is still a real action, and the one a developer asks for
# by editing these files.  That is the same meaning the mode has in a published
# tree; only the transport differs (nothing to fetch, so redeploy in place).
#
# Order is the whole fix, not an implementation detail:
#   1. stop  - `start` is idempotent.  With HA still running it finds nothing
#              to do, so the domains removed in step 2 would only come back on
#              the next restart - possibly a day later, and looking like the
#              redeploy silently failed.
#   2. remove the domains this tree ships (the scan above, shared with install)
#   3. start - `_seed_ready()` now answers false, so start copies them in.
#
# --no-upgrade for the same reason the install path uses it: this script is the
# installer, and handing back to a "newer release" would replace this tree.
# Push/Pop-Location because `.\homeant` resolves against the tree, not the
# caller's cwd.  Same text as install.sh.
function _RedeploySourceTree([string]$Root) {
    _info "no version to download - redeploying this tree"
    Push-Location $Root
    try {
        _info "homeant stop ..."
        & .\homeant stop
        if ($LASTEXITCODE -ne 0) { _fatal "stop failed - fix the line above, then re-run" }
        _ReplaceShippedComponents $Root
        _info "homeant start ..."
        & .\homeant start --no-upgrade
        if ($LASTEXITCODE -ne 0) {
            _fatal "start did not finish - fix the line above, then re-run: cd '$Root'; .\homeant start"
        }
    } finally {
        Pop-Location
    }
}

function main() {
    if ($UpgradeMode) { _info "mode: upgrade" } else { _info "mode: fresh install" }

    # Auto-relocate when launched from the user's home directory: unpacking ~14k
    # files straight into $HOME would litter it, so drop into ~/homeant instead.
    # Fresh mode only - `--upgrade` must stay anchored to the tree it is pointed at,
    # so it keeps fataling when run from home (that is not a HomeAnt tree).
    if (-not $UpgradeMode -and $env:USERPROFILE) {
        $__homeNorm = $env:USERPROFILE.TrimEnd('\', '/').Replace('\', '/')
        $__cur = (Get-Location).Path.TrimEnd('\', '/').Replace('\', '/')
        if ($__cur -ieq $__homeNorm) {
            $__newDir = Join-Path $env:USERPROFILE "homeant"
            # Worded exactly as install.sh: a read-only or full profile would
            # otherwise surface as a raw New-Item error with the same facts in a
            # different shape.  -ErrorAction Stop so the catch is reached at all.
            # The path prints as ~/homeant, same as install.sh - it is the user's
            # own tree, and both platforms then hand over the same sentence.
            try { New-Item -ItemType Directory -Path $__newDir -Force -ErrorAction Stop | Out-Null }
            catch { _fatal "cannot create install directory: $(_Tilde $__newDir)" }
            Set-Location $__newDir
            _info "installing into $(_Tilde $__newDir)"
        }
    }

    # --- local source present? hand it over to the homeant command ---
    # install.ps1 is the online entry point. Running it inside a source tree would
    # re-download the release zip and clobber local edits, so bail out early -
    # EXCEPT in upgrade mode, where that same tree is exactly what we are asked to
    # overwrite. One $isTree covers both branches so they cannot drift.
    $here     = (Get-Location).Path
    $localToml = Join-Path $here "pyproject.toml"
    $isTree   = (Test-Path $localToml) -and
                ((Get-Content $localToml -Raw) -match '(?m)^\s*name\s*=\s*"homeant')

    if ($UpgradeMode) {
        # --upgrade means "overwrite the tree you are standing in". Requiring that
        # it *is* one costs nothing and stops a mistyped command from unpacking
        # ~14k files into $HOME or C:\.
        if (-not $isTree) { _fatal "upgrade target is not a HomeAnt tree: $(_Tilde $here)" }
    } elseif ($isTree) {
        _info "HomeAnt source found in $(_Tilde $here)"
        _info "Run command:"
        _info "  .\homeant start"
        # Not `exit`: under `irm ... | iex` this body runs inline in the caller's
        # session, so `exit 0` tears down the console window on this early-return
        # path as well - the user sees three lines flash by and nothing else.
        # Report success through LASTEXITCODE and let main() return instead.
        $global:LASTEXITCODE = 0
        return
    }

    # --- resolve version: stamped at package build time ---
    # positive check ("is this a real version?") - never compare against the
    # placeholder literal here, or the packager's global replace rewrites the guard too
    if ($HOMEANT_VERSION -notmatch '^\d+(\.\d+)*$') {
        # Two ways in, and they need different words.  In fresh mode the guard
        # above already returned for a source tree, so this is a copy of this file
        # taken out of the tree and run somewhere else.  In upgrade mode it is the
        # opposite case: only a source tree is accepted as the target (see the
        # guard above), and a source tree is by definition never stamped - the
        # packager stamps a *copy*.  So there is nothing to fetch, which is a fact
        # about the tree and not a failure: `--upgrade` means "make the deployment
        # match this tree", and in a tree that is done in place.  Why each step is
        # in that order, and why the scan is shared with the install path, live
        # next to _RedeploySourceTree.  Same wording as install.sh.
        #
        # `$global:LASTEXITCODE = 0` + `return`, never `exit`: under
        # `irm ... | iex` that body runs in the caller's session and `exit 0`
        # tears down their console window (see the contract at the bottom of
        # this file).  Same shape as the fresh-mode early return above.
        if ($UpgradeMode) {
            _RedeploySourceTree $here
            $global:LASTEXITCODE = 0
            return
        }
        # No install-method menu here: the guard above
        # already points a source-tree user at homeant start, so listing it
        # again as "option 1" made the two branches say the same thing twice.
        # Bare throw, not _fatal: _fatal would re-print its message. And never
        # `exit` here - under `irm ... | iex` that tears down the caller's own
        # console window (see the contract at the bottom of this file).
        throw "installer is not stamped with a version"
    }

    # Before the 20 MB download and the 14k-file unzip: a port we cannot have is
    # worth nothing to discover three minutes later.
    _preflight $here

    # Registering the service is the one step here that needs administrator
    # rights, and nothing in this file elevates: a UAC dialog cannot be answered
    # by an unattended run, and re-launching the install under another account
    # would leave the files owned by it.  So the limit is said once, now, rather
    # than being discovered by the last line of the install.
    if (-not (_is_admin) -and -not (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue)) {
        _info "note: starting at boot needs the Windows service, and registering it needs administrator rights"
        _info "      after this finishes, run once from an elevated prompt: .\homeant start"
    }

    # ============================================================
    # download HomeAnt source
    # ============================================================
    # a plain source tree never reaches here - the guard at the top hands it over
    # to the homeant command instead. An `--upgrade` run does reach here,
    # and $RepoRoot is then the tree being overwritten.
    # installs into the current directory
    $RepoRoot = (Get-Location).Path

    # straight into the system temp dir - no staging subdirectory. The PID keeps
    # concurrent installs (two terminals, or a re-run while the first is stuck) off
    # each other's file, and it means the first progress frame can never read a
    # stale leftover from a previous run: a fixed name would show whatever the last
    # interrupted download had already written, then collapse back to zero.
    $tmpZip = Join-Path $env:TEMP "HomeAnt-${HOMEANT_VERSION}.${PID}.zip"
    try {
        _download $ZipUrl $tmpZip "downloading HomeAnt-${HOMEANT_VERSION}"
        # Before the extract, not after: a bad archive must not be able to
        # half-populate a tree first. The default branch keeps "unchecked"
        # visibly distinct from "verified" - folding it into either other case
        # would make a missing digest look like a pass.
        switch (_zip_digest_ok $tmpZip) {
            0 { }
            1 { throw "checksum mismatch: $tmpZip is not the HomeAnt-${HOMEANT_VERSION} this script was packed for - retry once; if it repeats, the published zip is the wrong file" }
            default { _info "note: zip checksum not verified (no sha256 tool, or this installer carries no stamped digest)" }
        }
        _extract_here $tmpZip $RepoRoot
    } catch {
        # No prefix here: _download and _extract_here already say what failed.
        # Prepending "download failed: " turned a zip-layout error into a download
        # error and, with _download's own prefix, produced it twice.
        _fatal $_.Exception.Message
    } finally {
        if (Test-Path $tmpZip) { Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue }
    }

    # ============================================================
    # ensure uv is available
    # ============================================================
    # Priority: 1) project-local .tools/uv  2) system-wide uv on PATH
    #           3) neither -> download into the project's .tools/uv
    # The fallback install stays inside the project: UV_INSTALL_DIR points there,
    # and uvinstall never touches PATH or writes a registry receipt on its own.
    $UvDir = Join-Path $RepoRoot ".tools\uv"
    $UvExe = Join-Path $UvDir "uv.exe"
    if (Test-Path $UvExe) {
        $uvCmd = $UvExe
        _info "uv in .tools: $(_Tilde $uvCmd) ($(& $uvCmd --version))"
    } else {
        $pathUv = (Get-Command uv -ErrorAction SilentlyContinue).Source
        if ($pathUv) {
            $uvCmd = $pathUv
            _info "uv on PATH: $(_Tilde $uvCmd) ($(& $uvCmd --version))"
        } else {
            New-Item -ItemType Directory -Path $UvDir -Force | Out-Null
            $env:UV_INSTALL_DIR     = $UvDir
            # Run the installer straight from the response - nothing touches disk.
            # The body is executed through a scriptblock, i.e. in its own scope:
            # every variable and function the installer defines ($app_name,
            # $UVLOCK_TOML, its timeout knobs, its functions) stays local, so it can
            # neither collide with this script's names nor leak into the caller's
            # session. The server serves .ps1 as application/octet-stream with no
            # charset, so decode the raw bytes as UTF-8 rather than trust .Content.
            try {
                $resp = Invoke-WebRequest -UseBasicParsing -Uri $UvInstallUrl `
                    -TimeoutSec $TransferTimeoutSec
                $uvInstaller = if ($resp.RawContentStream) {
                    [System.Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
                } else {
                    [string]$resp.Content
                }
                & ([scriptblock]::Create($uvInstaller))
            } catch {
                _fatal "uv install failed: $($_.Exception.Message)"
            }
            if (-not (Test-Path $UvExe)) { _fatal "uv install failed - binary not found after download" }
            $uvCmd = $UvExe
        }
    }

    # ============================================================
    # auto-repair incomplete source (partial clone)
    # ============================================================
    # core/pyproject.toml matters as much as the package itself: core/ is linked as
    # an editable path dependency, so a tree holding homeassistant/ but missing the
    # package metadata makes `uv sync` fail with "does not appear to be a Python
    # project". Both halves are checked, either one missing means re-extract.
    $corePkg  = Join-Path $RepoRoot "core\homeassistant\__init__.py"
    $coreMeta = Join-Path $RepoRoot "core\pyproject.toml"
    if ((-not (Test-Path $corePkg)) -or (-not (Test-Path $coreMeta))) {
        _info "core/ incomplete (homeassistant/ or pyproject.toml missing) - downloading full package to repair ..."
        $tmpZip = Join-Path $env:TEMP "HomeAnt-${HOMEANT_VERSION}.${PID}.zip"
        try {
            # label passed: without it the progress line has no "[install] ..." prefix
            # and no file name, just a bare "2.6 / 19.4MB" redrawing itself.
            _download $ZipUrl $tmpZip "downloading HomeAnt-${HOMEANT_VERSION}"
            # Same gate as the first download: repairing a tree with a zip that
            # is not the published one would leave a mixed tree behind.
            switch (_zip_digest_ok $tmpZip) {
                0 { }
                1 { throw "checksum mismatch: $tmpZip is not the HomeAnt-${HOMEANT_VERSION} this script was packed for - retry once; if it repeats, the published zip is the wrong file" }
                default { _info "note: zip checksum not verified (no sha256 tool, or this installer carries no stamped digest)" }
            }
            _extract_here $tmpZip $RepoRoot
            # throw, not _fatal - the catch below owns the reporting (same reason as
            # _extract_here). _fatal here would report twice.
            if ((-not (Test-Path $corePkg)) -or (-not (Test-Path $coreMeta))) {
                throw "repair failed - core/ still incomplete after re-extract"
            }
            _info "source repaired from $ZipUrl"
        } catch {
            _fatal $_.Exception.Message
        } finally {
            if (Test-Path $tmpZip) { Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue }
        }
    }

    if (-not (Test-Path (Join-Path $RepoRoot "vendor\ha_xiaomi_home\custom_components\xiaomi_home"))) {
        _info "vendor/ha_xiaomi_home not found - Xiaomi integration skipped"
    }

    # ============================================================
    # uv sync + homeant start
    # ============================================================
    # Stop a running Home Assistant before uv sync - Windows locks .exe files and
    # uv sync cannot overwrite .venv\Scripts\hass.exe while it is in use. The
    # pid-file-first order and the process-tree kill live in _stop_hass; the
    # paragraph that used to be here described the old command-line match.
    _stop_hass

    # --inexact: uv sync must not prune what HA installed at runtime from
    # integration manifests (frontend 235 MB, hassil, numpy, PyTurboJPEG ...).
    # Those are not in uv.lock, so the default exact sync deletes them and the
    # next boot spends ~30s putting 288 MB back. Versions covered by the lock
    # are still aligned exactly - only extraneous packages are left alone.
    # No --python here, on purpose. The interpreter is pinned in exactly one
    # place - requires-python in pyproject.toml, ">=3.14.2,<3.15" - and uv
    # enforces it on every entry point: a mismatching UV_PYTHON, a --python flag
    # or a stale .python-version is a hard error, never a silent build from
    # source. A version literal repeated here could only drift out of step with
    # that one source. Keep this identical to install.sh: same reason, same text.
    _info "uv sync ..."
    & $uvCmd sync --inexact --link-mode=copy
    if ($LASTEXITCODE -ne 0) { _fatal "uv sync FAILED" }
    _info "dependencies installed"

    # ============================================================
    # replace the integrations this release ships
    # ============================================================
    # Install is the step that owns versions, so it drops the seed marker and
    # removes the domains this tree ships; `start` below then has to *copy* them
    # from the tree just unpacked instead of merging into what is already there.
    # The scan itself is _ReplaceShippedComponents, shared with `--upgrade` in a
    # source tree so the two paths can never delete different sets.
    _ReplaceShippedComponents $RepoRoot

    _info "homeant start ..."
    # --no-upgrade: this script has just unpacked the newest release and synced
    # the venv, so the two things `start` would otherwise do first - probe the
    # release host and hand back to this script - can only be wrong. Handing
    # back is what would make the loop endless.
    # Only zero / non-zero is inspected, on purpose. homeant prints its own
    # SUCCESS / PENDING / FAIL line - the three-way distinction is for the
    # reader - and folds SUCCESS and PENDING into the same code, so there is no
    # state vocabulary to keep in sync between the two files. A step left for a
    # human is not an install failure: the files are in place, the missing piece
    # is on the line above, and re-running `homeant start` is the retry.
    # Reporting it as FAILED is how a good install gets reinstalled in a loop
    # that ends the same way - so say what is actually wrong and what to do, and
    # keep rc=1 for the automation that only reads the exit code.
    & .\homeant start --no-upgrade
    $startRc = $LASTEXITCODE
    if ($startRc -ne 0) {
        _fatal "start did not finish - fix the line above, then re-run: cd '$here'; .\homeant start"
    }
} # end main

try {
    main
} finally {
    # Restore the caller's session, then drop the stashes so nothing is left
    # behind in a window that stays open after the install. $UpgradeMode and
    # $__a are the mode-parse leftovers: under `irm ... | iex` this whole file
    # runs in the caller's own scope, so they would otherwise stay in the session.
    $ErrorActionPreference = $__prevErrorAction
    $ProgressPreference    = $__prevProgress
    Remove-Variable __prevErrorAction, __prevProgress, UpgradeMode, __a -ErrorAction SilentlyContinue
}
# `irm ... | iex` evaluates this script inside the caller's own session, so a
# bare `exit` - even `exit 0` - tears that session down and takes the console
# window with it. The online installer only ever runs from a window the user
# opened, so success falls through and leaves the prompt (and the log above it)
# alive. Failures surface through the throw in _fatal, which the caller's own
# session reports without closing anything - no key press needed.
#
# No `exit` anywhere in this file, by design. Every real failure already throws
# through _fatal at the call site (uv sync, homeant start), so control never
# reaches this point with a non-zero status - the check below is belt-and-braces
# for a future native call whose status nobody inspects. It throws instead of
# exiting on purpose: exiting here would close the window at the exact moment
# the user needs to read why it failed. A child process (-File, the dispatcher's
# -Command) still gets a non-zero code, because an uncaught throw ends that
# process with 1.
# `$null -ne` first: LASTEXITCODE is unset until the first native command runs,
# and `$null -ne 0` is true - that alone would fire on a clean early return.
if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    throw "installer finished with exit code $LASTEXITCODE"
}
