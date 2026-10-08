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
$ZipUrl                = "https://$HomeAntServer/core/HomeAnt-${HOMEANT_VERSION}.zip"
$UvInstallUrl          = "https://$HomeAntServer/uv/0.12.23/uvinstall.ps1"
# $ConnectTimeoutSec     = 15
$TransferTimeoutSec    = 600
# How long _stop_hass waits for hass to exit before killing it again.  The same
# number is HASS_STOP_WAIT_SEC in install.sh: both installers stop the same
# instance, so "still alive" must start being true at the same moment.
$HassStopWaitSec       = 20

function _info($m) { Write-Host "[install] $m" }
function _err($m)  { Write-Host "[install] $m" -ForegroundColor Red }
function _fatal($m) { _err $m; throw $m }

# ============================================================
# preflight - refuse BEFORE anything is downloaded or overwritten
# ============================================================
# Same list and same rule as install.sh's PREFLIGHT_PORTS: free is fine, our
# own Home Assistant is fine (it gets stopped further down), anybody else is a
# hard stop.  The MQTT broker this project will grow is one more entry here
# plus one more in install.sh - the two lists are meant to be the same list.
$PreflightPorts = @(
    @{ Port = 8123; Label = "Home Assistant HTTP" }
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
        if (_is_our_hass_pid $owner) {
            # Worded exactly as install.sh says it: the two installers answer to
            # the same rule (keep their logic and their output in step), and a
            # platform that stays silent here would look like it skipped a check.
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
    # Non-port requirements belong here: the MQTT broker binary a later release
    # ships, a Windows feature HA needs, a free-space budget.  Same contract as
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
        _fatal "install directory is not writable: $Dir - change to a directory you own (e.g. cd ~) and re-run"
    }
}

# --- upgrade mode: argv only, one spelling ---
# `--upgrade` repurposes this script: instead of refusing an existing tree it
# takes it as the overwrite target (the guard in main() is what changes in T6).
# Nothing else is accepted - no -Upgrade / -u alias, no HOMEANT_UPGRADE env -
# because every dispatch form we support can pass an argument
# (& $sb --upgrade, -File ... --upgrade); `irm ... | iex` cannot, and that is
# exactly the fresh-install case, so it needs none.
# Script scope on purpose: $args read inside main() would be main()'s own
# parameter list (empty), not the script's - the two are not the same variable.
$UpgradeMode = $false
foreach ($__a in $args) { if ($__a -eq '--upgrade') { $UpgradeMode = $true } }

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
        throw "zip did not expand into $destDir - unexpected archive layout"
    }
}

function _stop_hass {
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
            try { New-Item -ItemType Directory -Path $__newDir -Force -ErrorAction Stop | Out-Null }
            catch { _fatal "cannot create install directory: $__newDir" }
            Set-Location $__newDir
            _info "installing into $__newDir"
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
        if (-not $isTree) { _fatal "upgrade target is not a HomeAnt tree: $here" }
    } elseif ($isTree) {
        _info "HomeAnt source found in $here"
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
        # Reaching this means "not inside a source tree" (the guard above already
        # returned for that case) AND "no version stamped on me" - i.e. a copy of
        # this file was taken out of the source tree and run somewhere else.
        # No install-method menu here (removed 2026-10-26): the guard above
        # already points a source-tree user at homeant start, so listing it
        # again as "option 1" made the two branches say the same thing twice.
        # Same text as install.sh.
        # Bare throw, not _fatal: _fatal would re-print its message. And never
        # `exit` here - under `irm ... | iex` that tears down the caller's own
        # console window (see the contract at the bottom of this file).
        throw "installer is not stamped with a version"
    }

    # Before the 20 MB download and the 14k-file unzip: a port we cannot have is
    # worth nothing to discover three minutes later.
    _preflight $here

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
        _info "uv in .tools: $uvCmd ($(& $uvCmd --version))"
    } else {
        $pathUv = (Get-Command uv -ErrorAction SilentlyContinue).Source
        if ($pathUv) {
            $uvCmd = $pathUv
            _info "uv on PATH: $uvCmd ($(& $uvCmd --version))"
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
    _info "uv sync ..."
    & $uvCmd sync --inexact --link-mode=copy
    if ($LASTEXITCODE -ne 0) { _fatal "uv sync FAILED" }
    _info "dependencies installed"

    # ============================================================
    # replace the integrations this release ships
    # ============================================================
    # ~/.homeant/custom_components is written by `homeant start`, and start only
    # fills gaps: a missing target is copied, an existing one is merged - and a
    # merge cannot rewrite a .py (only yaml/json have merge semantics).  A fixed
    # integration would therefore never reach an install that already had one.
    #
    # Install is the step that owns versions, so it (a) drops the seed marker,
    # the only thing that makes start re-run its deploy at all, and (b) removes
    # the domains this release ships, so start has to *copy* them from the tree
    # just unpacked instead of merging into what is already there.
    #
    # Deleting rather than copying over is deliberate: copying into an existing
    # directory leaves behind files the release no longer ships, and HA keeps
    # importing them.  Only domains named by the two shipped sources are removed
    # - a component the user added by hand is not in that list and stays put.
    $haCc = Join-Path $env:USERPROFILE ".homeant\custom_components"
    if (Test-Path $haCc) {
        $seedMarker = Join-Path $env:USERPROFILE ".homeant\.seed-state"
        if (Test-Path $seedMarker) {
            Remove-Item $seedMarker -Force -ErrorAction SilentlyContinue
        }
        $ccSources = @(
            (Join-Path $RepoRoot "config\custom_components"),
            (Join-Path $RepoRoot "vendor\ha_xiaomi_home\custom_components")
        )
        foreach ($ccSrc in $ccSources) {
            if (-not (Test-Path $ccSrc)) { continue }
            foreach ($ccDir in (Get-ChildItem -Path $ccSrc -Directory)) {
                if ($ccDir.Name.StartsWith('_')) { continue }
                $ccTarget = Join-Path $haCc $ccDir.Name
                if (Test-Path $ccTarget) {
                    Remove-Item $ccTarget -Recurse -Force
                    _info "replacing custom_components/$($ccDir.Name)"
                }
            }
        }
    }

    _info "homeant start ..."
    # --no-install: this script has just unpacked the newest release and synced
    # the venv, so the two things `start` would otherwise do first - probe the
    # release host and hand back to this script - can only be wrong. Handing
    # back is what would make the loop endless.
    # Only zero / non-zero is inspected, on purpose. homeant prints its own
    # SUCCESS / PENDING / FAIL line - the three-way distinction is for the
    # reader - and folds SUCCESS and PENDING into the same code, so there is no
    # state vocabulary to keep in sync between the two files. A step left for a
    # human is not an install failure: the files are in place and the service is
    # up, and re-running is the retry. Reporting it as FAILED is how a good
    # install gets reinstalled in a loop that ends the same way.
    & .\homeant start --no-install
    $startRc = $LASTEXITCODE
    if ($startRc -ne 0) {
        _fatal "FAILED"
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
