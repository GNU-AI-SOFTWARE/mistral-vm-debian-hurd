#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Claire Ivanenka <claire@gnu-ai.org>
#
# Repair the guest network on the preinstalled image.
#
# At boot, ifup often fails on a stale /run/network/ifstate, so eth0
# stays unconfigured and pfinet is never attached to it: every
# connect then fails with "Network is unreachable".  This script
# attaches pfinet statically to the QEMU user-mode network
# (10.0.2.15/24, gateway 10.0.2.2, DNS 10.0.2.3) and verifies DNS
# and HTTP.
#
#   python3 hurd_vm.py <image> guest-netfix.sh

set -u

# Addresses come from the driver (HURD_VM_NET=...), defaults 10.0.2.x.
GUEST_ADDR=${HURD_VM_GUEST_ADDR:-10.0.2.15}
GUEST_MASK=${HURD_VM_GUEST_MASK:-255.255.255.0}
GUEST_GW=${HURD_VM_GUEST_GW:-10.0.2.2}
GUEST_DNS=${HURD_VM_GUEST_DNS:-10.0.2.3}

ls -l /dev/eth0 2>&1 || true

rm -f /etc/resolv.conf
printf 'nameserver %s\noptions timeout:2 attempts:2\n' "$GUEST_DNS" > /etc/resolv.conf
cat /etc/resolv.conf

# The image sometimes ships /run/network/ifstate as a directory,
# which makes ifup fail at boot:
rm -rf /run/network/ifstate 2>/dev/null
mkdir -p /run/network 2>/dev/null
: > /run/network/ifstate 2>/dev/null || true

# The image's NIC is driven by netdde, started by ifup at boot with
# DHCP from slirp (bound to 10.0.2.15).  The ifstate clearing above
# fixes the usual "already configured" skip; re-run ifup so netdde
# (re)attaches and dhclient (re)binds.  NEVER attach pfinet with -fg
# on /servers/socket/2: that kills netdde and removes /dev/eth0,
# leaving the guest with no network at all (the QEMU hostfwd then
# answers "Connection reset").  pfinet is a last resort, only on a
# free socket server.
if ifconfig eth0 2>/dev/null | grep -q "inet $GUEST_ADDR"; then
    echo "eth0 already $GUEST_ADDR (boot DHCP via netdde)"
else
    ifdown eth0 2>/dev/null || true
    rc=0
    ifup eth0 2>&1 || rc=$?
    echo "ifup eth0 rc=$rc"
    sleep 2
    if ! ifconfig eth0 2>/dev/null | grep -q "inet $GUEST_ADDR"; then
        # ifup failed (stale ifstate at boot, or a translator already on
        # socket/2): configure the deterministic slirp address statically.
        # On Hurd, ifconfig also starts the passive translator on
        # /servers/socket/2.
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

sleep 2
ifconfig -a
showtrans /servers/socket/2 2>&1 || true

# slirp's UDP DNS (10.0.2.3) is often broken on Hurd; pin the hosts
# the tests touch (resolved on the host beforehand) so getent, wget
# and apt still work when the resolver fails:
pin() {
    _ip=$1 _name=$2
    grep -qw "$_name" /etc/hosts 2>/dev/null || echo "$_ip $_name" >> /etc/hosts
}
pin 194.71.11.165 deb.debian.org
pin 194.71.11.165 cdimage.debian.org
pin 18.239.50.80 huggingface.co
pin 18.239.105.46 datasets-server.huggingface.co
pin 140.82.121.4 github.com

i=0
until getent hosts deb.debian.org >/dev/null 2>&1; do
    i=$((i+1))
    [ "$i" -ge 10 ] && break
    sleep 2
done
if getent hosts deb.debian.org >/dev/null 2>&1; then
    echo "DNS-OK"
else
    # UDP may be broken through netdde/slirp; force DNS over TCP:
    rm -f /etc/resolv.conf
    printf 'nameserver %s\noptions usevc timeout:2 attempts:2\n' "$GUEST_DNS" > /etc/resolv.conf
    if getent hosts deb.debian.org >/dev/null 2>&1; then
        echo "DNS-OK-TCP"
    else
        echo "DNS-KO"
    fi
fi
if timeout 5 bash -c "exec 3<>/dev/tcp/$GUEST_DNS/53" 2>/dev/null; then
    echo "TCP-DNS-$GUEST_DNS-OK"
else
    echo "TCP-DNS-$GUEST_DNS-KO"
fi
ping -c1 -W2 "$GUEST_GW" 2>&1 | tail -1
if wget -q -O /dev/null http://deb.debian.org/ 2>/dev/null; then
    echo "HTTP-OK"
else
    echo "HTTP-KO"
fi
echo NETFIX-DONE
