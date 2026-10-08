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
