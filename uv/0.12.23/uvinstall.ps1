# Licensed under the MIT license
# <LICENSE-MIT or https://opensource.org/licenses/MIT>, at your
# option. This file may not be copied, modified, or distributed
# except according to those terms.
# =============================================================================
# HomeAnt uv install
# -----------------------------------------------------------------------------
# Downloads the uv PyPI wheel matching this machine and unpacks uv.exe,
# uvx.exe and uvw.exe. No parameter block: the only consumer is the
# HomeAnt online installer, which sets UV_INSTALL_DIR and pipes this
# script into iex.
#
# Install destination:
#     $env:UV_INSTALL_DIR    when set (flat layout)
#     .tools\uv              otherwise, relative to the current directory
#
# PATH is never touched and no cargo-dist receipt is written - an isolated
# project-local uv is the whole point of this fork.
#
# Environment variables:
#     UV_INSTALL_DIR    install directory, takes absolute priority
#     UV_MIRROR         PyPI packages base override. Example:
#                       UV_MIRROR=https://pypi.tuna.tsinghua.edu.cn
#                       (/packages appended automatically)
#                       Default: derived from the uv.lock registry field.
#     UV_DOWNLOAD_URL   full wheel URL, skips the mirror fallback chain
# =============================================================================

# Transfer timeouts in seconds - same two knobs, same values, as install.ps1:
#   $ConnectTimeoutSec  HEAD size probe (a tiny request, fail fast)
#   $TransferTimeoutSec the real transfer (a wheel can be tens of MB)
$ConnectTimeoutSec  = 15
$TransferTimeoutSec = 600

# =============================================================================
# HomeAnt: Embedded uv.lock [[package]] uv TOML block
# Synchronized from project uv.lock. Parser below extracts name + version + wheels
# at runtime - nothing else is hardcoded here.
# =============================================================================

$UVLOCK_TOML = @'
[[package]]
name = "uv"
version = "0.12.23"
source = { registry = "https://mirrors.aliyun.com/pypi/simple" }
sdist = { url = "https://mirrors.aliyun.com/pypi/packages/6d/f5/34435c1d9ee52d38bda1d7d2948d8ef48a9ac9ec79e6fbfd81ced8b0ad19/uv-0.12.23.tar.gz", hash = "sha256:e952162a5be6847deaccf3fd5b41a628506fd9ff1ea4363b8c975bd6f927d832" }
wheels = [
    { url = "https://mirrors.aliyun.com/pypi/packages/7c/af/ed4bcd2fbebe19d613412d02053af44809d02a1204cb6cf0a7a9af7a02fa/uv-0.12.23-py3-none-linux_armv6l.whl", hash = "sha256:8f021dfbe5b6081021dd47fd8372cfba386fc5d39161e73d4f33cff90ce1760c" },
    { url = "https://mirrors.aliyun.com/pypi/packages/01/15/a2514d061b92d4b7c0ebefc3da51f3ff5a528e9f4c067ea2cf48c5573e5a/uv-0.12.23-py3-none-macosx_10_12_x86_64.whl", hash = "sha256:4951286e68f116fa41897ecf8e248cc2836cb013be6d4f37afae4049f9ef4df7" },
    { url = "https://mirrors.aliyun.com/pypi/packages/23/e9/b75db33954e985a763bd3277a477cabd551aaba7d08872b22400838dace2/uv-0.12.23-py3-none-macosx_11_0_arm64.whl", hash = "sha256:99197c022afbd1edd0a295e9fdac5078c61eddf2e77657a77294cfe8ea0d94b9" },
    { url = "https://mirrors.aliyun.com/pypi/packages/39/77/ff15f878a215b543fd5aa05d2e3f939f3a2c46502ff58362eb5b51434af3/uv-0.12.23-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64.whl", hash = "sha256:895137194d242cc8075c3006288b485af54feeae68512f4ffc96f75b27543cc0" },
    { url = "https://mirrors.aliyun.com/pypi/packages/ab/2f/94bfc387f1c8169c2f82415f39a1a318d148104053b0efc70d3e15e72da6/uv-0.12.23-py3-none-manylinux_2_17_armv7l.manylinux2014_armv7l.musllinux_1_1_armv7l.whl", hash = "sha256:c4d236bb220856027ba8e28ef693cac472ee2a740a61faef1ac2001165c7b3d5" },
    { url = "https://mirrors.aliyun.com/pypi/packages/a2/e2/42eba7023a4ab5496c17dacd27834cb0479fbccdadafe406cd4c187c5b33/uv-0.12.23-py3-none-manylinux_2_17_armv7l.manylinux2014_armv7l.whl", hash = "sha256:02f2a0fd1cb90b5fc06b52e8cbc3169bdc4e53804e9d66ba0ad516be767424f4" },
    { url = "https://mirrors.aliyun.com/pypi/packages/19/cb/c68249501d913d3ef35596283acbe0af0f1b388e27c6b46aa402e968c55d/uv-0.12.23-py3-none-manylinux_2_17_i686.manylinux2014_i686.whl", hash = "sha256:855232fd95d456bd5ee17e262f73112967fbbb2803e745f1911c08ceb042e641" },
    { url = "https://mirrors.aliyun.com/pypi/packages/14/fb/a6cf5eb99a37368ce8ad698ff406997897502eee5cacfd3d9805a02336ff/uv-0.12.23-py3-none-manylinux_2_17_ppc64le.manylinux2014_ppc64le.whl", hash = "sha256:b70220be9056bd05908770179101a53687eb5c89f72a3edc1e20df0cd3f157c2" },
    { url = "https://mirrors.aliyun.com/pypi/packages/c2/73/809af8a6fc97adfce7750b6b6d8c7fdf3e57c6ad40bc99e6b1228cdabe1e/uv-0.12.23-py3-none-manylinux_2_17_s390x.manylinux2014_s390x.whl", hash = "sha256:08b2fc8e250efdfc1e47da0483f3e19f433bb0934282361817f61c0b19cb3674" },
    { url = "https://mirrors.aliyun.com/pypi/packages/e5/83/85a6c63c24905af4924fddb11a499b934913f59a134248367a1ef1a4716f/uv-0.12.23-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:565c6e2874dbeae86c02f3dea97255e878fec672659a73d4930c6b93fcab2fff" },
    { url = "https://mirrors.aliyun.com/pypi/packages/df/50/84d0424b63718ac6f277c0b77206751e078a0adea16813320508a033e718/uv-0.12.23-py3-none-manylinux_2_28_aarch64.whl", hash = "sha256:9346c741175c7fc0d380f30fcfcb8d5e13f5f900b30536230c8d34208524e47a" },
    { url = "https://mirrors.aliyun.com/pypi/packages/66/b7/03fe205f2f9af0dfa4467e561c7ecc620f03e7f7339d78d16fa38582e207/uv-0.12.23-py3-none-manylinux_2_31_riscv64.musllinux_1_1_riscv64.whl", hash = "sha256:b03b1c35b5f301e888671eb5d0e74b8789558905db1d411cb8e07ea4ec320268" },
    { url = "https://mirrors.aliyun.com/pypi/packages/f3/17/26862297f6bece90d7f825f78a814580b36a9ac69a139f0385660e1a205c/uv-0.12.23-py3-none-manylinux_2_31_riscv64.whl", hash = "sha256:5b07cbea635177da0d6b902bcde9ce0304c1fa5b49a37b27ef7aa021c5c31697" },
    { url = "https://mirrors.aliyun.com/pypi/packages/da/04/31ae46fe42cbdc91b30162e5d430719911339a18f8ab3fb9126ee409fe29/uv-0.12.23-py3-none-musllinux_1_1_i686.whl", hash = "sha256:7a919e3420adf4cc03895b404af655c7229f646bffabbd9a16979683772a7d34" },
    { url = "https://mirrors.aliyun.com/pypi/packages/71/b7/340260cf1a92017b271869764a68d9a019d796c4a7fa80c6c1bbcf5721eb/uv-0.12.23-py3-none-musllinux_1_1_x86_64.whl", hash = "sha256:11d18c1589d7907376b4f3fa31028e46cc609ac4b7d83eabc2b04cd8ca1433cc" },
    { url = "https://mirrors.aliyun.com/pypi/packages/77/76/3a7ab3ab0bd8ab107b50fe981a3198188c8260e9b17b629e64899eca709b/uv-0.12.23-py3-none-win32.whl", hash = "sha256:f05c5ad8d6cb4b2659cb6368ed02c067539415b24a2b69effd8ada576fdbf303" },
    { url = "https://mirrors.aliyun.com/pypi/packages/af/fd/816f4e8a76197312ec8037369e5b5c8a5ebde7aa118b6250259e031d3444/uv-0.12.23-py3-none-win_amd64.whl", hash = "sha256:fb8a4117a5224d73abe2204a1e744ae34b544f1b9a0b761576851deaa7215c12" },
    { url = "https://mirrors.aliyun.com/pypi/packages/d9/74/1906f7f3f785fcf37a72b371d4ab58ec7193c5760923136cca1376209bc8/uv-0.12.23-py3-none-win_arm64.whl", hash = "sha256:9a65258535db432df51ca7d26338c01431820b94b634762f5718fc55895f974e" },
]
'@

# -----------------------------------------------------------------------------
# Parse embedded TOML -> globals (app_name, app_version, packages_base, wheels)
# -----------------------------------------------------------------------------
function Parse-UvLock {
    param([string]$TomlText)
    $result = @{}
    $result.wheels = @()

    # name - also the wheel's <name>-<version>.data directory prefix
    if ($TomlText -match 'name\s*=\s*"([^"]+)"') {
        $result.name = $Matches[1]
    }

    # version
    if ($TomlText -match 'version\s*=\s*"([^"]+)"') {
        $result.version = $Matches[1]
    }

    # registry = "https://mirrors.aliyun.com/pypi/simple"
    if ($TomlText -match 'registry\s*=\s*"([^"]+)"') {
        $reg = $Matches[1]
        # packages_base = registry with /simple stripped + /packages
        $result.packages_base = ($reg -replace '/simple$', '') + '/packages'
    }

    # wheels = [ { url = "...", hash = "sha256:..." }, ... ]
    foreach ($line in ($TomlText -split "`n")) {
        if ($line -match 'url\s*=\s*"([^"]+)"') {
            $wurl = $Matches[1]
            $whash = ''
            if ($line -match 'sha256:([0-9a-f]+)') { $whash = $Matches[1] }
            $result.wheels += @{ url = $wurl; hash = $whash }
        }
    }

    return $result
}

# Parse immediately
$uvlock = Parse-UvLock -TomlText $UVLOCK_TOML
$app_name = $uvlock.name
$app_version = $uvlock.version
$packages_base = $uvlock.packages_base

# Runtime overrides
if ($env:UV_MIRROR) {
    $mbase = $env:UV_MIRROR.TrimEnd('/')
    if ($mbase -notmatch '/packages$') { $mbase += '/packages' }
    $packages_base = $mbase
}

# Build fallback download URLs
if ($env:UV_DOWNLOAD_URL) {
    $ArtifactDownloadUrls = @($env:UV_DOWNLOAD_URL)
} else {
    $ArtifactDownloadUrls = @(
        $packages_base,
        "https://pypi.tuna.tsinghua.edu.cn/packages",
        "https://pypi.org/packages"
    )
}

function Install-Binary {
  Initialize-Environment

  # HomeAnt: Build platforms hashtable dynamically from embedded TOML wheels.
  # artifact_name = path suffix (everything after /packages/) - download code does
  # $packages_base/$artifact_name -> full URL. zip_ext = .whl always (PyPI wheel).
  $platforms = @{}

  foreach ($wheel in $uvlock.wheels) {
      $wurl = $wheel.url
      # path suffix = strip everything up to and including /packages/
      $suffix = ($wurl -replace '^.*/packages/', '')
      $fname = Split-Path $wurl -Leaf

      # Rust triple <-> wheel filename tag mapping
      $triples = @()
      if ($fname -match 'win_amd64')  { $triples = @('x86_64-pc-windows-msvc', 'x86_64-pc-windows-gnu') }
      elseif ($fname -match 'win_arm64') { $triples = @('aarch64-pc-windows-msvc', 'aarch64-pc-windows-gnu') }
      elseif ($fname -match 'win32')  { $triples = @('i686-pc-windows-msvc', 'i686-pc-windows-gnu') }

      foreach ($t in $triples) {
          $platforms[$t] = @{
              "artifact_name" = $suffix
              "bins"          = @("uv.exe", "uvx.exe", "uvw.exe")
              "zip_ext"       = ".whl"
              # sha256 from the embedded uv.lock - verified after the transfer,
              # same as uvinstall.sh does.
              "hash"          = $wheel.hash
          }
      }
  }

  $arch = Get-TargetTriple $platforms
  if (-not $platforms.ContainsKey($arch)) {
    $platforms_json = ConvertTo-Json $platforms
    throw "ERROR: could not find binaries for this platform. Last platform tried: $arch platform info: $platforms_json"
  }

  # Unpacking a wheel needs System.IO.Compression.ZipFile, so make sure the type
  # is there before touching the network. A missing type is an environment
  # problem, not a bad mirror - loading it inside Download would surface as
  # "failed to download binaries" only after every URL had been retried.
  # Windows PowerShell 5.1 gets the assembly from .NET Framework 4.5, its own
  # minimum. PowerShell 6+ resolves the type from the shared framework already,
  # where loading the facade by name is unnecessary and may fail outright.
  if ($PSVersionTable.PSVersion.Major -lt 6) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction Stop
  }

  $download_result = $false
  $first_url = $true
  foreach ($url in $ArtifactDownloadUrls) {
    if (-not $first_url) {
      Write-Information "[install] retrying alternative download URL"
    }
    $first_url = $false

    try {
      $fetched = Download -download_url "$url" -platforms $platforms -arch $arch
      $download_result = $true
      break
    } catch {
      Write-Information "[install] failed to download from $url"
      Write-Information "  $(Get-ExceptionMessage $_.Exception)"
      # keep going, maybe we have backup download URLs
    }
  }
  if (-not $download_result) {
    throw "failed to download binaries"
  }

  try {
    Invoke-Installer -artifacts $fetched
  } catch {
    throw @"
We encountered an error trying to perform the installation;
please review the error messages below.

$_
"@
  }
}

function Get-TargetTriple($platforms) {
  $double = Get-Arch
  if ($platforms.Contains("$double-msvc")) {
    return "$double-msvc"
  } else {
    return "$double-gnu"
  }
}

function Get-Arch() {
  try {
    # NOTE: this might return X64 on ARM64 Windows, which is OK since emulation is available.
    # It works correctly starting in PowerShell Core 7.3 and Windows PowerShell in Win 11 22H2.
    # Ideally this would just be
    #   [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture
    # but that gets a type from the wrong assembly on Windows PowerShell (i.e. not Core)
    $a = [System.Reflection.Assembly]::LoadWithPartialName("System.Runtime.InteropServices.RuntimeInformation")
    $t = $a.GetType("System.Runtime.InteropServices.RuntimeInformation")
    $p = $t.GetProperty("OSArchitecture")
    # Possible OSArchitecture Values: https://learn.microsoft.com/dotnet/api/system.runtime.interopservices.architecture
    # Rust supported platforms: https://doc.rust-lang.org/stable/rustc/platform-support.html
    switch ($p.GetValue($null).ToString())
    {
      "X86" { return "i686-pc-windows" }
      "X64" { return "x86_64-pc-windows" }
      "Arm" { return "thumbv7a-pc-windows" }
      "Arm64" { return "aarch64-pc-windows" }
    }
  } catch {
    # The above was added in .NET 4.7.1, so Windows PowerShell in versions of Windows
    # prior to Windows 10 v1709 may not have this API.
    Write-Verbose "Get-TargetTriple: Exception when trying to determine OS architecture."
    Write-Verbose $_
  }

  # This is available in .NET 4.0. We already checked for PS 5, which requires .NET 4.5.
  Write-Verbose("Get-TargetTriple: falling back to Is64BitOperatingSystem.")
  if ([System.Environment]::Is64BitOperatingSystem) {
    return "x86_64-pc-windows"
  } else {
    return "i686-pc-windows"
  }
}

function Get-ExceptionMessage($exception) {
  if ($null -eq $exception) {
    return ""
  }

  while (($null -ne $exception.InnerException) -and (-not [string]::IsNullOrWhiteSpace($exception.InnerException.Message))) {
    $exception = $exception.InnerException
  }

  return $exception.Message
}

# Shared with install.ps1: a HEAD request yields the total size, a background
# job runs the transfer (silent, no Write-Progress residue), and the foreground
# polls the file size to show "received / total MB" with a carriage-return
# rewrite. Falls back to received-only when HEAD is unsupported.
# Advanced function for the same reason as Invoke-DownloadFile: a mistyped
# parameter name must fail at the call, not as a null path inside the loop.
function _download {
  [CmdletBinding()]
  param($url, $outFile, $label)
  $total = $null
  try {
    $h = Invoke-WebRequest -UseBasicParsing -Uri $url -Method Head `
      -TimeoutSec $ConnectTimeoutSec -ErrorAction Stop
    $cl = $h.Headers['Content-Length']
    if ($cl) { $total = [long]$cl }
  } catch { }
  if ($label) {
    if ($total) {
      Write-Host ("$label 0.0 / {0:N1}MB" -f ($total/1MB)) -NoNewline
    } else {
      Write-Host "$label" -NoNewline
    }
  }
  $job = Start-Job -ArgumentList $url, $outFile, $TransferTimeoutSec -ScriptBlock {
    param($u, $o, $t)
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -UseBasicParsing -Uri $u -OutFile $o -TimeoutSec $t
  }
  # Right-pad to the widest line drawn so far: "\r" cannot erase characters
  # from a longer previous render, so a shrinking number would leave a tail.
  $drawn = 0
  while ($job.State -eq 'Running') {
    Start-Sleep -Milliseconds 200
    $got = if (Test-Path $outFile) { (Get-Item $outFile).Length } else { 0 }
    $line = if ($total) { "${label} {0:N1} / {1:N1}MB" -f ($got/1MB), ($total/1MB) }
            else        { "${label} {0:N1}MB" -f ($got/1MB) }
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
  # Same message install.ps1 uses, so a failure reads the same on both sides.
  # The URL is already in it; repeating the local temp path added nothing.
  if ($failed) { throw "download failed: $url - $($err -join '; ')" }
}

# Advanced function on purpose. A plain PowerShell function silently swallows
# unrecognized -named arguments into $args, so the parameter just stays empty
# and the failure surfaces somewhere deep inside the body - that is how the
# -path/$outFile mismatch turned into "failed to download from <url>" on every
# mirror. [CmdletBinding()] makes a wrong name a bind-time error instead.
function Invoke-DownloadFile {
  [CmdletBinding()]
  param($url, $outFile, $label)
  try {
    _download $url $outFile $label
  } catch [System.Management.Automation.PipelineStoppedException] {
    Remove-Item $outFile -Force -ErrorAction SilentlyContinue
    throw "Download cancelled by user"
  } catch {
    # No prefix and no path here: _download already reports
    # "download failed: <url> - <reason>". Wrapping it again produced
    # "failed to download X to Y: download failed: X - reason".
    Remove-Item $outFile -Force -ErrorAction SilentlyContinue
    throw (Get-ExceptionMessage $_.Exception)
  }
}

# HomeAnt: SHA256 through the runtime, not through Get-FileHash.  This
# script is piped into `iex` from the online installer, so it inherits whatever
# session that produced - and a session can exist where the cmdlet is not
# discoverable even though the rest of Microsoft.PowerShell.Utility works
# (observed on 5.1, where the same name resolves fine in a fresh shell).
# System.Security.Cryptography is the runtime PowerShell itself sits on, so
# this needs no module loaded and no dependency installed.  BitConverter spells
# the digest "AA-BB-.."; callers want one bare token, lowercase.
function Get-Sha256Hex([string]$Path) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $stream = [System.IO.File]::OpenRead($Path)
    try {
      return [System.BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally { $stream.Dispose() }
  } finally { $sha.Dispose() }
}

# HomeAnt: one place decides the target directory - UV_INSTALL_DIR wins,
# otherwise project-local .tools\uv (isolated, never a PATH-friendly location).
function Get-InstallDir() {
  if ($env:UV_INSTALL_DIR) { return $env:UV_INSTALL_DIR }
  return (Join-Path (Get-Location) ".tools\uv")
}

function Download($download_url, $platforms, $arch) {
  # Lookup what we expect this platform to look like
  $info = $platforms[$arch]
  $artifact_name = $info["artifact_name"]
  # One line carries identity + progress: the byte ticker below rewrites this
  # same label, so a separate "[install] download <app> <ver>" line would just
  # repeat what the next line already says.
  $label = "[install] $app_name $app_version downloading"

  # HomeAnt: no temp *directory* any more. The wheel lands in one
  # uniquely-named temp file, gets verified, and is then unpacked entry by
  # entry straight into the install dir - only the three binaries ever reach
  # the disk and nothing else is extracted. The `finally` removes the wheel on
  # every exit path (success, hash mismatch, download failure, Ctrl-C), so a
  # new machine is never left with a %TEMP% directory tree behind.
  #
  # ZipFile does not care about the extension, but .zip keeps a leftover file
  # recognisable if the process is killed hard enough to skip the `finally`.
  $tmp_wheel = Join-Path ([System.IO.Path]::GetTempPath()) "$app_name-$([System.Guid]::NewGuid()).zip"

  # Download and unpack!
  $url = "$download_url/$artifact_name"
  Write-Verbose "  from $url"
  Write-Verbose "  to $tmp_wheel"

  try {
    Invoke-DownloadFile -url $url -outFile $tmp_wheel -label $label

    # Parity with uvinstall.sh: the embedded uv.lock carries a sha256 per wheel,
    # so verify the bytes before unpacking anything. A mismatch throws, which
    # Install-Binary treats as "this source failed" and retries the next mirror -
    # exactly the right response for a truncated or tampered download.
    # Get-Sha256Hex, not Get-FileHash: see the note on that function.
    $expected = $info["hash"]
    if ($expected) {
      $actual = Get-Sha256Hex $tmp_wheel
      if ($actual -ne $expected.ToLowerInvariant()) {
        throw "hash mismatch: expected $expected got $actual"
      }
    }

    Write-Information "[install] files extracting ..."

    # HomeAnt: flat layout, so the online installer finds .tools\uv\uv.exe
    # exactly where Get-InstallDir said it would be.
    $dest_dir = (New-Item -Force -ItemType Directory -Path (Get-InstallDir)).FullName

    # Wheel bins live under <name>-<version>.data/scripts/ - pull exactly those
    # entries out of the archive instead of extracting the whole wheel.
    $prefix = "$app_name-$app_version.data/scripts/"
    $wanted = @{}
    foreach ($bin_name in $info["bins"]) { $wanted[$bin_name] = $true }

    # ZipFile (loaded once in Install-Binary) reads a .whl byte-for-byte the same
    # way Expand-Archive reads a .zip, and it does not care about the extension.

    $bin_paths = @()
    $zip = [System.IO.Compression.ZipFile]::OpenRead($tmp_wheel)
    try {
      foreach ($entry in $zip.Entries) {
        $name = $entry.FullName -replace '\\', '/'
        if (-not $name.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
        $leaf = $name.Substring($prefix.Length)
        if (-not $wanted.ContainsKey($leaf)) { continue }
        Write-Verbose "  Unpacked $leaf"
        # ExtractToFile returns void, so the path is recorded separately.
        $out_path = Join-Path $dest_dir $leaf
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $out_path, $true)
        $bin_paths += $out_path
        $wanted.Remove($leaf)
      }
    } finally {
      $zip.Dispose()
    }

    if ($wanted.Count -gt 0) {
      $missing = @($wanted.Keys) -join ", "
      throw "unexpected wheel layout - $prefix is missing $missing"
    }

    return @{ "bin_paths" = $bin_paths }
  } finally {
    Remove-Item -LiteralPath $tmp_wheel -Force -ErrorAction SilentlyContinue
  }
}

function Invoke-Installer($artifacts) {
  # HomeAnt: Download() already unpacked the binaries straight into
  # Get-InstallDir(). All that is left is to confirm each one landed and is
  # non-empty - a zero-byte uv.exe would otherwise only surface much later as
  # ".tools\uv\uv.exe is not a valid application".
  foreach ($bin_path in $artifacts["bin_paths"]) {
    if (-not (Test-Path -LiteralPath $bin_path)) { throw "missing binary: $bin_path" }
    $size = (Get-Item -LiteralPath $bin_path).Length
    if ($size -le 0) { throw "empty binary: $bin_path" }
    Write-Verbose "  Installed $bin_path"
  }
}

function Initialize-Environment() {
  If (($PSVersionTable.PSVersion.Major) -lt 5) {
    throw @"
Error: PowerShell 5 or later is required to install $app_name.
Upgrade PowerShell:

    https://docs.microsoft.com/en-us/powershell/scripting/setup/installing-windows-powershell

"@
  }

  # HomeAnt: no execution-policy gate here. The online installer executes
  # this script from memory via [scriptblock]::Create, which the execution
  # policy does not govern at all - on a default Windows client (Restricted)
  # the gate would refuse a run that would in fact have worked, and telling the
  # user to run Set-ExecutionPolicy would not change anything. The PS version
  # check above is the only prerequisite that genuinely applies.
}

# Make Write-Information statements be visible
$InformationPreference = "Continue"

# A plain call is enough. Every failure path inside Install-Binary throws, and
# this script is inlined into the online installer (iex), where `exit` would
# tear down the caller's console window - the throw propagates to that
# installer's catch, which reports it and keeps the window open. Wrapping it in
# `try { ... } catch { throw }` only added a frame that did nothing.
Install-Binary
