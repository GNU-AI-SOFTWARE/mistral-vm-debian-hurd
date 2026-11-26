#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Claire Ivanenka <claire@gnu-ai.org>
#
# Build and test ALL the GNU-AI-SOFTWARE translators on real
# GNU/Hurd in one guest run:
#
#   neuron-translator  orchestrator-translator  inference-translator
#   httpfs-translator  data-base-translator
#
#   python3 hurd_vm.py <image> guest-all-translators.sh --ram 2G
#
# The same run works from Termux on Android (QEMU there has no
# /dev/kvm; the driver falls back to TCG by itself) -- see
# docs/TERMUX.md.
#
# Sources: the pre-injected /root/all-translators-src.tar.gz when
# present (offline; host side: pack the five source trees as
# neuron-translator/ ... data-base-translator/ into one tarball and
# feed it with inject-src.sh), otherwise a git clone from GitHub
# (the guest network is repaired first, see guest_net below).
#
# Markers on the serial line: NET-OK/APT-OK, then per project
# BUILD-<name>-OK|KO and CHECK-<name>-OK|FAIL, and finally
# ALL-TRANSLATORS-BUILT or ALL-TRANSLATORS-KO.  The exit code is 0
# only if every project built; a `make check` failure is reported
# but does not change the exit code (the build itself succeeded).

set -u
export DEBIAN_FRONTEND=noninteractive
# locale-clean builds: an ssh-sent locale the guest has not generated
# breaks perl/dpkg mid-apt
export LANG=C.UTF-8 LC_ALL=C.UTF-8

PROJECTS="neuron-translator orchestrator-translator inference-translator httpfs-translator data-base-translator"

echo "=== guest: $(uname -a)"

# --- guest network (same logic as guest-netfix.sh) -------------------
# Addresses come from the driver (HURD_VM_NET=...), defaults 10.0.2.x.
# The image's NIC is driven by netdde, started by ifup at boot with
# DHCP from slirp; a stale /run/network/ifstate can make ifup skip
# eth0.  NEVER attach pfinet with -fg on /servers/socket/2: that kills
# netdde and removes /dev/eth0 (the hostfwd then answers "Connection
# reset").  pfinet is a last resort, only on a free socket server.
guest_net () {
    GUEST_ADDR=${HURD_VM_GUEST_ADDR:-10.0.2.15}
    GUEST_MASK=${HURD_VM_GUEST_MASK:-255.255.255.0}
    GUEST_GW=${HURD_VM_GUEST_GW:-10.0.2.2}
    GUEST_DNS=${HURD_VM_GUEST_DNS:-10.0.2.3}
    rm -f /etc/resolv.conf
    printf 'nameserver %s\noptions timeout:2 attempts:2\n' "$GUEST_DNS" \
        > /etc/resolv.conf
    if [ "${HURD_VM_MODE:-slirp}" = "tap" ]; then
        echo "tap mode: static $GUEST_ADDR gw $GUEST_GW dns $GUEST_DNS"
        ifconfig eth0 "$GUEST_ADDR" netmask "$GUEST_MASK" up 2>&1 || true
        route add default "$GUEST_GW" 2>&1 || true
        sleep 1
    elif ifconfig eth0 2>/dev/null | grep -q "inet $GUEST_ADDR"; then
        echo "eth0 already $GUEST_ADDR (boot DHCP via netdde)"
    else
        rm -rf /run/network/ifstate 2>/dev/null || true
        mkdir -p /run/network 2>/dev/null || true
        : > /run/network/ifstate 2>/dev/null || true
        ifdown eth0 2>/dev/null || true
        rc=0
        ifup eth0 2>&1 || rc=$?
        echo "ifup eth0 rc=$rc"
        sleep 2
        if ! ifconfig eth0 2>/dev/null | grep -q "inet $GUEST_ADDR"; then
            echo "WARN: ifup rc=$rc without $GUEST_ADDR; static ifconfig fallback"
            ifconfig eth0 "$GUEST_ADDR" netmask "$GUEST_MASK" up 2>&1 || true
            route add default "$GUEST_GW" 2>&1 || true
            sleep 1
        fi
    fi
    if ! ifconfig eth0 2>/dev/null | grep -q "inet $GUEST_ADDR"; then
        echo "WARN: eth0 has no $GUEST_ADDR; device and server state:"
        ifconfig -a 2>&1 || true
        ls -l /dev/eth* 2>&1 || true
        showtrans /servers/socket/2 2>&1 || true
        if [ -e /dev/eth0 ]; then
            echo "WARN: /dev/eth0 exists; dhclient -1 fallback"
            timeout 20 dhclient -1 /dev/eth0 2>&1 | tail -5 || true
            sleep 1
        fi
        if ! showtrans /servers/socket/2 2>&1 | grep -q .; then
            echo "WARN: socket server free; last resort: static pfinet"
            settrans -a /servers/socket/2 /hurd/pfinet \
                -i /dev/eth0 -a "$GUEST_ADDR" -m "$GUEST_MASK" -g "$GUEST_GW" 2>&1 \
                || echo "WARN: pfinet attach rc=$?"
            sleep 2
        else
            echo "WARN: /servers/socket/2 already has a translator; not touching it"
        fi
    fi
    i=0
    until getent hosts deb.debian.org >/dev/null 2>&1; do
        i=$((i+1))
        [ "$i" -ge 10 ] && break
        sleep 2
    done
    if ! getent hosts deb.debian.org >/dev/null 2>&1; then
        printf 'nameserver %s\noptions usevc timeout:2 attempts:2\n' "$GUEST_DNS" \
            > /etc/resolv.conf
    fi
    pin_host () {
        ip=$1 name=$2
        getent hosts "$name" >/dev/null 2>&1 && return 0
        grep -qw "$name" /etc/hosts 2>/dev/null && return 0
        echo "$ip $name" >> /etc/hosts
    }
    # slirp's UDP DNS (10.0.2.3) is often broken on Hurd; pin the hosts
    # the build touches so apt/git still work when the resolver fails:
    pin_host 194.71.11.165 deb.debian.org
    pin_host 194.71.11.165 cdimage.debian.org
    pin_host 18.239.50.80 huggingface.co
    pin_host 18.239.105.46 datasets-server.huggingface.co
    pin_host 140.82.121.4 github.com
    pin_host 140.82.121.3 github.com
    pin_host 185.199.108.133 raw.githubusercontent.com
    pin_host 185.199.108.133 objects.githubusercontent.com
    pin_host 140.82.121.10 codeload.github.com
    pin_host 172.65.251.78 gitlab.com
    pin_host 217.197.84.140 codeberg.org
    pin_host 96.47.72.84 freebsd.org
    pin_host 96.47.72.77 www.freebsd.org
    pin_host 163.237.194.45 cgit.freebsd.org
    pin_host 85.30.190.139 git.freebsd.org
    pin_host 209.51.188.168 git.savannah.gnu.org
    pin_host 142.132.237.69 repo.or.cz
    if getent hosts deb.debian.org >/dev/null 2>&1; then
        echo "DNS-OK"
    else
        echo "DNS-KO (hosts pins in place)"
    fi
    if wget -q -O /dev/null --timeout=5 http://deb.debian.org/ 2>/dev/null; then
        echo "HTTP-OK"
    else
        echo "HTTP-KO (offline mode)"
    fi
}
guest_net

# --- build dependencies ------------------------------------------------
echo "=== installing the build dependencies"
if apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
        build-essential autoconf automake libtool pkg-config git \
        ca-certificates libpq-dev libcurl4-openssl-dev libmicrohttpd-dev; then
    echo "APT-OK"
else
    echo "APT-KO (continuing: an injected source tarball may still build)"
fi

# --- sources ------------------------------------------------------------
cd /root
if [ -f all-translators-src.tar.gz ]; then
    echo "=== using the pre-injected /root/all-translators-src.tar.gz"
    mkdir -p src
    tar xf all-translators-src.tar.gz -C src
fi

# --- per-project build + check ------------------------------------------
fail=0
for p in $PROJECTS; do
    echo "=== [$p] fetching the sources"
    if [ -d "src/$p" ]; then
        cd "/root/src/$p"
    else
        cd /root
        rm -rf "$p"
        if git clone "https://github.com/GNU-AI-SOFTWARE/$p"; then
            cd "/root/$p"
        else
            echo "BUILD-$p-KO: no sources (no tarball entry, git clone failed)"
            fail=1
            continue
        fi
    fi
    echo "=== [$p] autogen + configure + make"
    if [ ! -x ./configure ]; then
        if ! autoreconf -i 2>&1 | tail -3; then
            echo "BUILD-$p-KO: autoreconf failed"
            fail=1
            continue
        fi
    fi
    # Normalize timestamps so make never re-runs the autotools through
    # ./missing (the guest image ships no automake unless APT-OK).
    touch aclocal.m4 configure config.h.in 2>/dev/null
    find . -name Makefile.in -exec touch {} + 2>/dev/null
    if ./configure && make -j2; then
        echo "BUILD-$p-OK"
    else
        echo "BUILD-$p-KO (configure or make failed)"
        fail=1
        continue
    fi
    echo "=== [$p] make check"
    if [ "$p" = "inference-translator" ]; then
        # mount the translator before make check so test_posix.sh runs
        # for real instead of skipping (exit 77)
        settrans -g /inference 2>/dev/null || true
        mkdir -p /inference
        settrans -a /inference ./src/inference-translator 2>/dev/null || true
    fi
    if make check; then
        echo "CHECK-$p-OK"
    else
        echo "CHECK-$p-FAIL (build is OK; see tests/test-suite.log)"
        tail -20 tests/test-suite.log 2>/dev/null || true
    fi
    if [ "$p" = "inference-translator" ]; then
        settrans -g /inference 2>/dev/null || true
    fi
    if [ "$p" = "httpfs-translator" ] && wget -q -O /dev/null --timeout=5 http://deb.debian.org/ 2>/dev/null; then
        echo "=== [$p] smoke test: mount a real URL through the translator"
        settrans -a /web ./src/httpfs https://www.gnu.org 2>/dev/null || true
        ls -l /web 2>/dev/null | head -5 || true
        settrans -a /web 2>/dev/null || true
    fi
done

echo "=== summary"
if [ "$fail" -eq 0 ]; then
    echo "ALL-TRANSLATORS-BUILT"
else
    echo "ALL-TRANSLATORS-KO"
fi
exit $fail
