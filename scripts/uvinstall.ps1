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
version = "0.13.6"
source = { registry = "https://mirrors.aliyun.com/pypi/simple" }
sdist = { url = "https://mirrors.aliyun.com/pypi/packages/08/d5/88d43310d425f6e3a43997ad771658ded3237d0ed78c16a89b2353eddf56/uv-0.13.6.tar.gz", hash = "sha256:d762215478854f6cf20c3faed8f1eb30c8101d568e7bc32a3abafc063929d5c5" }
wheels = [
    { url = "https://mirrors.aliyun.com/pypi/packages/e4/05/74c9ae1b59e0aa49f1fa84f31ea4ee0ec930825681d3af8430c017faa848/uv-0.13.6-py3-none-linux_armv6l.whl", hash = "sha256:4d20de8a3ed6c3d39b4e2f8f7d0411cd13a3959f5191b2b5d0209344034ed056" },
    { url = "https://mirrors.aliyun.com/pypi/packages/33/55/ff10d3f756c39a239e492e57262bd22fcda412a9f85a268175447cf33b09/uv-0.13.6-py3-none-macosx_10_12_x86_64.whl", hash = "sha256:95f042b95e67ef8c3d3569bb6e9d895e592002c5e871cf3f1afe407c9d77a294" },
    { url = "https://mirrors.aliyun.com/pypi/packages/f4/14/77230cc4f6a73a31578cb8070a5f67f58a76946ead643ae8a4155620d6da/uv-0.13.6-py3-none-macosx_11_0_arm64.whl", hash = "sha256:51361029b7cf91185d0092c4b356d110b4ad629e9cb11593f1e006ac72a72ca9" },
    { url = "https://mirrors.aliyun.com/pypi/packages/05/3b/f78583af6504f1270da95fd0d0935e381f58330c007260f450e81a1e5e1b/uv-0.13.6-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64.whl", hash = "sha256:7ec5486476d67a4214c460b5492393ee2300d84e1de17b293100a019b413568b" },
    { url = "https://mirrors.aliyun.com/pypi/packages/2b/c3/ee263d5c8e0f0bbd553b19692b1de2ce5860baf0f9653c6217535ff27833/uv-0.13.6-py3-none-manylinux_2_17_armv7l.manylinux2014_armv7l.musllinux_1_1_armv7l.whl", hash = "sha256:710c68ea4853aaf70ab2d0ccf2e147dee59449f035a1f8fe7759f087a31cdbf5" },
    { url = "https://mirrors.aliyun.com/pypi/packages/27/33/172e323f6e424f7bf4aef0480b514132dce6350b2acf14235d9b0671fa82/uv-0.13.6-py3-none-manylinux_2_17_armv7l.manylinux2014_armv7l.whl", hash = "sha256:d4d7026c7327dd11fc2c0d4bae4735ff6944293d370644f35d2e6e917cdcae04" },
    { url = "https://mirrors.aliyun.com/pypi/packages/96/e6/29fa137fe3699a9f61f0e05640caff4713ac485cdf09b005f99f9bd595d4/uv-0.13.6-py3-none-manylinux_2_17_i686.manylinux2014_i686.whl", hash = "sha256:46c2ef1f744f20007be0cdd66db3022bb1ddcac477c754efda875e401a48d810" },
    { url = "https://mirrors.aliyun.com/pypi/packages/2f/8c/bf3672027b68bd930e6764122147fcbd1f2dd9d76fc8e02707916c3e0727/uv-0.13.6-py3-none-manylinux_2_17_ppc64le.manylinux2014_ppc64le.whl", hash = "sha256:b2d25a49d135a441f688a6869d6d8062fb6f0a2e3e32e18b72c9de1d0eb9521e" },
    { url = "https://mirrors.aliyun.com/pypi/packages/a3/c0/77c56a78a94b488cf0b54e505df5ecfae65b90b08cddb8e018bb2ae66268/uv-0.13.6-py3-none-manylinux_2_17_s390x.manylinux2014_s390x.whl", hash = "sha256:b9f57153fc98b7aa41dd6720e3de7578747f60b7bff22f50b76dcc47205bdde3" },
    { url = "https://mirrors.aliyun.com/pypi/packages/5c/43/0688ff5142934238f2812e348f4a24e09b57d48bfb38dd8f850325a0d9de/uv-0.13.6-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", hash = "sha256:22759b8895e1103578de6fb4cd27e94b4399d021023378d29f01333717751fba" },
    { url = "https://mirrors.aliyun.com/pypi/packages/99/3a/451b544e105b3595b27f74535c0d6b1105b1b2dcb9b49ca5282dd27bcbc3/uv-0.13.6-py3-none-manylinux_2_28_aarch64.whl", hash = "sha256:2e9a99100abe4f99605366974c4f9fe27f236615e05de8e78c0f357e38f09f97" },
    { url = "https://mirrors.aliyun.com/pypi/packages/ab/59/9483985fadf5b2e60e64ca89e215f9e2da45a54d7323b9765ba03a5ebd80/uv-0.13.6-py3-none-manylinux_2_31_riscv64.musllinux_1_1_riscv64.whl", hash = "sha256:1cd4b0a35fa599b83da5437748150319ae0917c522da60898327645382942faa" },
    { url = "https://mirrors.aliyun.com/pypi/packages/8d/c9/a412c2aa0494b307fce8c9c8234827f2fedb888954d3b3c8930a32a047c9/uv-0.13.6-py3-none-manylinux_2_31_riscv64.whl", hash = "sha256:8542dcf118ce8a7cf2e4ba1fea7f5cc1638bc02b5b30dab1ead7e9034dc62fd3" },
    { url = "https://mirrors.aliyun.com/pypi/packages/28/51/4fecb1d7a32ebf5c444643a4da9d1ae0878de0c71284792dbcdaaf61a5b8/uv-0.13.6-py3-none-musllinux_1_1_i686.whl", hash = "sha256:feb0f1d555798f68f28719cab437ab1dfb5d918db86ab4554215cbf9d940f63b" },
    { url = "https://mirrors.aliyun.com/pypi/packages/3a/10/7a4c287b1d377353c939f451881529528c78815d57f36097a6607187ab1a/uv-0.13.6-py3-none-musllinux_1_1_x86_64.whl", hash = "sha256:d1efed1d15102ffdbf5e0fd49a440643dda2d921a2ad856792ae42ccd43c253f" },
    { url = "https://mirrors.aliyun.com/pypi/packages/c8/b3/288467a6d1fdd99aca73dec3d4a919b80c201ec0d680093cd494d5b8efc7/uv-0.13.6-py3-none-win32.whl", hash = "sha256:29135609166960c74539abdaf4264f9819a0381be18b224125418356d67dd208" },
    { url = "https://mirrors.aliyun.com/pypi/packages/7c/9d/714523188aea3054644d8e69ae8089aac70325a3cee2d3d05509d4f23398/uv-0.13.6-py3-none-win_amd64.whl", hash = "sha256:7c9bc35ccb65932d8dc10a0669f9c805eaa3c9827ce18ecbecb1bd67944b2cdf" },
    { url = "https://mirrors.aliyun.com/pypi/packages/6a/7d/5ebb4cccbb11ae9e10f6cda704ce382722ab9dc68c9280a5b810378aee3e/uv-0.13.6-py3-none-win_arm64.whl", hash = "sha256:722c7f96803f115e53918b8ee8858d1680ea6077c614b6db750747d709ab8d9a" },
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
