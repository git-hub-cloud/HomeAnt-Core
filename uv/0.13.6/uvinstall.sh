#!/bin/sh
# shellcheck shell=dash
# shellcheck disable=SC2039
#
# Licensed under the MIT license.

# =============================================================================
# HomeAnt uv install
# -----------------------------------------------------------------------------
# Embeds the raw uv.lock [[package]] uv TOML block below. sed/awk parse it at
# runtime -> version + mirror registry + wheels with url/hash.
#
# Install destination:
#     $UV_INSTALL_DIR    when set
#     $(pwd)/.tools/uv   otherwise
#
# Never touches PATH and never writes a shell profile - the only consumer is
# the HomeAnt online installer, which pins UV_INSTALL_DIR to .tools/uv.
#
# Environment variables (same names on both .sh and .ps1):
#
#   UV_INSTALL_DIR     Override install directory entirely.
#
#   UV_MIRROR          PyPI packages base URL override.
#                      Example: UV_MIRROR=https://pypi.tuna.tsinghua.edu.cn
#                      (auto appends /packages if not already present)
#                      Default: derived from uv.lock registry field.
#
#   UV_DOWNLOAD_URL    Full wheel URL, skips the mirror fallback chain.
# =============================================================================

set -u

# Same two timeouts as install.ps1 / uvinstall.ps1 / install.sh.
CONNECT_TIMEOUT=15
TRANSFER_TIMEOUT=600

# ==============================================================
# BEGIN uv.lock [[package]] uv  (synchronized from project uv.lock)
# ==============================================================

UVLOCK_TOML=$(cat <<'UVLOCK'
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
UVLOCK
)

# ==============================================================
# END uv.lock block
# ==============================================================

# --- Parse uv.lock TOML -> shell variables ---------------------------------
# sed for the scalar fields, awk for the per-wheel url/hash pairs.
# Output: APP_NAME= / APP_VERSION= / REGISTRY_URL= / PACKAGES_BASE=
#         WHEEL_COUNT= / WHEEL_N_URL= / WHEEL_N_HASH=
_parse_uvlock() {
    _t="$UVLOCK_TOML"

    # name
    echo "APP_NAME=$(echo "$_t" | sed -n 's/^name = "\([^"]*\)".*/\1/p' | head -1)"

    # version
    echo "APP_VERSION=$(echo "$_t" | sed -n 's/^version = "\([^"]*\)".*/\1/p' | head -1)"

    # registry = "https://.../simple"
    _reg=$(echo "$_t" | sed -n 's/.*registry = "\([^"]*\)".*/\1/p' | head -1)
    _reg_no_simple=${_reg%/simple}
    echo "REGISTRY_URL=$_reg"
    echo "PACKAGES_BASE=${_reg_no_simple}/packages"

    # wheels: each line has both url = "..." and hash = "sha256:..."
    # Process each wheel line -> one WHEEL_N_URL= and WHEEL_N_HASH=
    echo "$_t" | sed -n '/^wheels = \[/,/^\]/p' | grep 'url = ' | \
    awk 'BEGIN { i = 0 } {
        i++
        # extract url
        u = $0
        sub(/.*url = "/, "", u)
        sub(/".*/, "", u)
        # extract hash
        h = $0
        sub(/.*sha256:/, "", h)
        sub(/[^0-9a-f].*/, "", h)
        print "WHEEL_" i "_URL=" u
        print "WHEEL_" i "_HASH=" h
    }'
    _count=$(echo "$_t" | sed -n '/^wheels = \[/,/^\]/p' | grep -c 'url = ')
    echo "WHEEL_COUNT=$_count"
}

# --- Detect platform ------------------------------------------------------

# The tables below mirror get_architecture() in https://astral.sh/uv/install.sh
# entry for entry; only the output encoding differs (upstream emits a Rust
# target triple to pick a tarball, we emit os/arch/libc to pick a wheel tag).

_detect_os() {
    _ostype="$(uname -s)"
    if [ "$_ostype" = Linux ]; then
        if [ "$(uname -o)" = Android ]; then
            _ostype=Android
        fi
    fi
    if [ "$_ostype" = SunOS ]; then
        # Solaris and illumos both report SunOS; disambiguate via `uname -o`,
        # using the full path in case a coreutils uname comes first in PATH.
        if [ "$(/usr/bin/uname -o 2>/dev/null)" = illumos ]; then
            _ostype=illumos
        fi
    fi
    case "$_ostype" in
        Darwin)                          echo "darwin"  ;;
        Linux)                           echo "linux"   ;;
        MINGW*|MSYS*|CYGWIN*|Windows_NT) echo "windows" ;;
        # busybox-w32 reports `uname -s` as exactly "Windows_NT", hence above.
        # Android/FreeBSD/NetBSD/DragonFly/illumos do get uv tarballs upstream
        # but PyPI ships no wheel for them.
        *)                               echo "unsupported" ;;
    esac
}

_detect_libc() {
    # upstream: gnu unless `ldd --version` mentions musl
    if [ "$(_detect_os)" = linux ]; then
        if ldd --version 2>&1 | grep -q 'musl'; then
            echo "musl"
            return
        fi
    fi
    echo "gnu"
}

# Helpers for the 32-bit-userland check, same technique as upstream: read the
# ELF header of the running shell, nothing needed beyond coreutils.

_current_exe() {
    if [ -L /proc/self/exe ]; then
        echo "/proc/self/exe"
    elif [ -n "${SHELL:-}" ]; then
        echo "$SHELL"
    else
        echo "/bin/sh"
    fi
}

_bitness() {
    _h="$(head -c 5 "$1" 2>/dev/null)"
    if [ "$_h" = "$(printf '\177ELF\001')" ]; then
        echo 32
    elif [ "$_h" = "$(printf '\177ELF\002')" ]; then
        echo 64
    else
        echo 0
    fi
}

_host_is_amd64_elf() {
    # ELF e_machine (2 bytes at offset 0x12) == 0x3E identifies amd64.
    [ "$(head -c 19 "$1" 2>/dev/null | tail -c 1)" = "$(printf '\076')" ]
}

_detect_arch() {
    _cputype="$(uname -m)"

    if [ "$(_detect_os)" = darwin ]; then
        # `uname -m` lies under Rosetta 2 and the legacy i386 compat layer;
        # `sysctl` reports the real machine.
        case "$_cputype" in
            i386)
                if sysctl hw.optional.x86_64 2>/dev/null | grep -q ': 1'; then
                    _cputype=x86_64
                fi
                ;;
            x86_64)
                if sysctl hw.optional.arm64 2>/dev/null | grep -q ': 1'; then
                    _cputype=arm64
                fi
                ;;
        esac
    fi

    case "$_cputype" in
        i386|i486|i686|i786|x86) _cputype=i686         ;;
        xscale|arm)              _cputype=arm          ;;
        armv6l)                  _cputype=arm          ;;
        armv7l|armv8l)           _cputype=armv7        ;;
        aarch64|arm64)           _cputype=aarch64      ;;
        x86_64|x86-64|x64|amd64) _cputype=x86_64       ;;
        ppc)                     _cputype=powerpc      ;;
        ppc64)                   _cputype=powerpc64    ;;
        ppc64le)                 _cputype=powerpc64le  ;;
        s390x)                   _cputype=s390x        ;;
        riscv64)                 _cputype=riscv64gc    ;;
        loongarch64)             _cputype=loongarch64  ;;
        *)                       _cputype=unknown      ;;
    esac

    # 64-bit Linux kernel running a 32-bit userland keeps the 32-bit ABI.
    if [ "$(_detect_os)" = linux ] && [ "$(_detect_libc)" = gnu ]; then
        case "$_cputype" in
            x86_64|aarch64|powerpc64|riscv64gc)
                if [ "$(_bitness "$(_current_exe)")" = 32 ]; then
                    case "$_cputype" in
                        x86_64)
                            if _host_is_amd64_elf "$(_current_exe)"; then
                                _cputype=unsupported   # x32 ABI, no wheel
                            else
                                _cputype=i686
                            fi
                            ;;
                        aarch64)   _cputype=armv7       ;;
                        powerpc64) _cputype=powerpc     ;;
                        riscv64gc) _cputype=unsupported ;;
                    esac
                fi
                ;;
        esac
    fi

    # armv7 builds require NEON/ASIMD; upstream falls back to the arm build.
    if [ "$_cputype" = armv7 ] && [ "$(_detect_os)" = linux ]; then
        if ! (grep '^Features' /proc/cpuinfo 2>/dev/null | grep -Eq 'neon|simd'); then
            _cputype=arm
        fi
    fi

    echo "$_cputype"
}

# --- Match best wheel URL for current platform -----------------------------

_match_wheel() {
    _os=$(_detect_os)
    _arch=$(_detect_arch)
    _libc=$(_detect_libc)

    # Wheel tag patterns, most specific first (substring match on filename).
    # Keys are the arch names produced by _detect_arch(), i.e. the same
    # `_cputype` values upstream uses.
    _patterns=""
    if [ "$_os" = "windows" ]; then
        case "$_arch" in
            x86_64)  _patterns="win_amd64 win32" ;;
            aarch64) _patterns="win_arm64 win_amd64 win32" ;;
            i686)    _patterns="win32" ;;
            # upstream has no Windows ARM32 build either (Get-Arch yields
            # thumbv7a, which is absent from its platforms table -> hard error)
            *)       return 1 ;;
        esac
    elif [ "$_os" = "darwin" ]; then
        case "$_arch" in
            aarch64) _patterns="macosx_11_0_arm64 macosx_10_12_x86_64" ;;
            x86_64)  _patterns="macosx_10_12_x86_64 macosx_11_0_arm64" ;;
            *)       return 1 ;;
        esac
    elif [ "$_os" = "linux" ]; then
        if [ "$_libc" = "musl" ]; then
            case "$_arch" in
                x86_64)    _patterns="musllinux_1_1_x86_64 manylinux_2_17_x86_64.manylinux2014_x86_64" ;;
                aarch64)   _patterns="manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64" ;;
                i686)      _patterns="musllinux_1_1_i686 manylinux_2_17_i686.manylinux2014_i686" ;;
                armv7)     _patterns="manylinux_2_17_armv7l.manylinux2014_armv7l.musllinux_1_1_armv7l" ;;
                arm)       _patterns="linux_armv6l" ;;
                riscv64gc) _patterns="manylinux_2_31_riscv64.musllinux_1_1_riscv64" ;;
                *)         return 1 ;;
            esac
        else
            case "$_arch" in
                x86_64)      _patterns="manylinux_2_17_x86_64.manylinux2014_x86_64" ;;
                aarch64)     _patterns="manylinux_2_28_aarch64 manylinux_2_17_aarch64.manylinux2014_aarch64.musllinux_1_1_aarch64" ;;
                i686)        _patterns="manylinux_2_17_i686.manylinux2014_i686" ;;
                armv7)       _patterns="manylinux_2_17_armv7l.manylinux2014_armv7l.musllinux_1_1_armv7l manylinux_2_17_armv7l.manylinux2014_armv7l" ;;
                arm)         _patterns="linux_armv6l" ;;
                powerpc64le) _patterns="manylinux_2_17_ppc64le.manylinux2014_ppc64le" ;;
                s390x)       _patterns="manylinux_2_17_s390x.manylinux2014_s390x" ;;
                riscv64gc)   _patterns="manylinux_2_31_riscv64.musllinux_1_1_riscv64 manylinux_2_31_riscv64" ;;
                *)           return 1 ;;
            esac
        fi
    else
        return 1
    fi

    for _pat in $_patterns; do
        _i=1
        while [ "$_i" -le "$WHEEL_COUNT" ]; do
            _wurl=$(eval echo "\$WHEEL_${_i}_URL")
            if echo "$_wurl" | grep -q "$_pat"; then
                echo "$_wurl"
                eval echo "\$WHEEL_${_i}_HASH"
                return 0
            fi
            _i=$((_i + 1))
        done
    done
    return 1
}

# --- Utilities ------------------------------------------------------------

_verify_sha256() {
    _f="$1"; _exp="$2"
    if command -v sha256sum >/dev/null 2>&1; then
        _a=$(sha256sum "$_f" | cut -d' ' -f1)
    elif command -v shasum >/dev/null 2>&1; then
        _a=$(shasum -a 256 "$_f" | cut -d' ' -f1)
    else
        return 0
    fi
    [ "$_a" = "$_exp" ] || { echo "[install] hash mismatch: expected $_exp got $_a" >&2; return 1; }
}

_get_bins() {
    if [ "$(_detect_os)" = "windows" ]; then
        echo "uv.exe uvx.exe uvw.exe"
    else
        echo "uv uvx"
    fi
}

_get_install_dir() {
    if [ -n "${UV_INSTALL_DIR:-}" ]; then
        echo "$UV_INSTALL_DIR"
    else
        # Default: isolated under current working directory, never on PATH
        echo "$(pwd)/.tools/uv"
    fi
}

# =============================================================================
# MAIN
# =============================================================================

main() {
    # 1. Parse embedded TOML -> APP_VERSION, PACKAGES_BASE, WHEEL_* variables
    eval "$(_parse_uvlock)"

    if [ -z "${APP_VERSION:-}" ] || [ "$WHEEL_COUNT" -lt 1 ]; then
        echo "[install] failed to parse uv.lock TOML block" >&2; exit 1
    fi

    # --- Runtime URL override chain -----------------------------------------
    # Priority (highest first): UV_DOWNLOAD_URL (single full URL)
    #                         -> UV_MIRROR        (mirror base, auto append /packages)
    #                         -> PACKAGES_BASE     (from uv.lock registry)
    # Then fallback: Tsinghua -> pypi.org

    if [ -n "${UV_DOWNLOAD_URL:-}" ]; then
        # Direct URL - skip mirror fallback entirely
        _single_url="$UV_DOWNLOAD_URL"
    else
        if [ -n "${UV_MIRROR:-}" ]; then
            _base="${UV_MIRROR%/}"
            case "$_base" in
                */packages) PACKAGES_BASE="$_base" ;;
                *)          PACKAGES_BASE="$_base/packages" ;;
            esac
        fi

        # Fallback chain
        _fallbacks="$PACKAGES_BASE \
            https://pypi.tuna.tsinghua.edu.cn/packages \
            https://pypi.org/packages"
    fi

    # 2. Pick platform wheel
    _match=$(_match_wheel)
    if [ -z "$_match" ]; then
        echo "[install] no wheel available for $(_detect_os)/$(_detect_arch)/$(_detect_libc)" >&2
        exit 1
    fi
    _url=$(echo "$_match" | sed -n '1p')
    _hash=$(echo "$_match" | sed -n '2p')
    # basename without spawning rev(1) - rev is util-linux, absent on busybox/Alpine
    _filename=${_url##*/}

    # Same opening line as uvinstall.ps1's Download(): what + version + verb.
    # curl's progress bar below is this side's transfer feedback, so the verb
    # ends the line here instead of being followed by our own byte ticker.
    echo "[install] $APP_NAME $APP_VERSION downloading" >&2

    # 3. Download - curl's own progress bar is the transfer feedback on this
    # side (parity with install.sh), so no byte ticker is implemented here.
    _tmp=$(mktemp -d) || { echo "[install] mktemp failed" >&2; exit 1; }
    _wheel="$_tmp/$_filename"
    _ok=0
    _attempt=0

    if [ -n "${_single_url:-}" ]; then
        # UV_DOWNLOAD_URL
        if curl -fL --progress-bar --connect-timeout "$CONNECT_TIMEOUT" \
              --max-time "$TRANSFER_TIMEOUT" -o "$_wheel" "$_single_url" \
           || wget -q -O "$_wheel" "$_single_url" 2>/dev/null; then
            _ok=1
        else
            echo "[install] failed to download from $_single_url" >&2
        fi
    else
        # Mirror fallback
        _suffix=$(echo "$_url" | sed -n 's|.*/packages/\(.*\)|\1|p')
        for _base in $_fallbacks; do
            _try="$_base/$_suffix"
            _attempt=$((_attempt + 1))
            # Parity with uvinstall.ps1: only the retries announce themselves.
            if [ "$_attempt" -gt 1 ]; then
                echo "[install] retrying alternative download URL" >&2
            fi
            if curl -fL --progress-bar --connect-timeout "$CONNECT_TIMEOUT" \
                  --max-time "$TRANSFER_TIMEOUT" -o "$_wheel" "$_try" \
               || wget -q -O "$_wheel" "$_try" 2>/dev/null; then
                _ok=1; break
            fi
            echo "[install] failed to download from $_try" >&2
        done
    fi

    if [ "$_ok" -eq 0 ]; then
        echo "[install] download failed for $_filename" >&2; rm -rf "$_tmp"; exit 1
    fi

    # 4. Verify hash
    _verify_sha256 "$_wheel" "$_hash" || { rm -rf "$_tmp"; exit 1; }

    # 5. Unpack wheel (ZIP)
    echo "[install] files extracting ..." >&2
    if command -v unzip >/dev/null 2>&1; then
        unzip -q "$_wheel" -d "$_tmp" || { rm -rf "$_tmp"; exit 1; }
    else
        python3 -c "import zipfile; zipfile.ZipFile('$_wheel').extractall('$_tmp')" 2>/dev/null \
            || { echo "[install] need unzip or python3" >&2; rm -rf "$_tmp"; exit 1; }
    fi

    # Bins live under <name>-<version>.data/scripts/
    _scripts="$_tmp/${APP_NAME}-${APP_VERSION}.data/scripts"
    _bin_src="$_tmp"
    [ -d "$_scripts" ] && _bin_src="$_scripts"

    # 6. Install
    _dir=$(_get_install_dir)
    mkdir -p "$_dir" || { echo "[install] cannot create $_dir" >&2; rm -rf "$_tmp"; exit 1; }

    for _bin in $(_get_bins); do
        _src="$_bin_src/$_bin"
        [ -f "$_src" ] || { echo "[install] missing: $_src" >&2; rm -rf "$_tmp"; exit 1; }
        # Both branches must be checked: `install || cp` on its own swallows a
        # double failure and the script then reaches the final `rm -rf` and
        # exits 0 with no binary in place. install.sh happens to notice via its
        # own [ -x ] test, but uvinstall.sh is distributed as a standalone
        # script, so it cannot rely on a caller doing that.
        install -m 0755 "$_src" "$_dir/$_bin" 2>/dev/null \
            || cp "$_src" "$_dir/$_bin" 2>/dev/null \
            || { echo "[install] cannot install $_bin to $_dir" >&2; rm -rf "$_tmp"; exit 1; }
    done

    rm -rf "$_tmp"
}

main "$@"
