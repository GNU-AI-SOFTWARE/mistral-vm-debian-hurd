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

ls -l /dev/eth0 2>&1 || true

rm -f /etc/resolv.conf
printf 'nameserver 10.0.2.3\noptions timeout:2 attempts:2\n' > /etc/resolv.conf
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
if ifconfig eth0 2>/dev/null | grep -q 'inet 10.0.2.15'; then
    echo "eth0 already 10.0.2.15 (boot DHCP via netdde)"
else
    ifdown eth0 2>/dev/null
    ifup eth0 2>&1
    echo "ifup eth0 rc=$?"
    sleep 2
fi
if ! ifconfig eth0 2>/dev/null | grep -q 'inet 10.0.2.15'; then
    echo "WARN: eth0 has no 10.0.2.15; device and server state:"
    ifconfig -a 2>&1 || true
    ls -l /dev/eth* 2>&1 || true
    showtrans /servers/socket/2 2>&1 || true
    if ! showtrans /servers/socket/2 2>&1 | grep -q .; then
        echo "WARN: socket server free; last resort: static pfinet"
        settrans -a /servers/socket/2 /hurd/pfinet \
            -i /dev/eth0 -a 10.0.2.15 -m 255.255.255.0 -g 10.0.2.2 2>&1
        echo "pfinet attach rc=$?"
        sleep 2
    else
        echo "WARN: /servers/socket/2 already has a translator; not touching it"
    fi
fi

sleep 2
ifconfig -a
showtrans /servers/socket/2 2>&1 || true

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
    printf 'nameserver 10.0.2.3\noptions usevc timeout:2 attempts:2\n' > /etc/resolv.conf
    if getent hosts deb.debian.org >/dev/null 2>&1; then
        echo "DNS-OK-TCP"
    else
        echo "DNS-KO"
    fi
fi
if timeout 5 bash -c 'exec 3<>/dev/tcp/10.0.2.3/53' 2>/dev/null; then
    echo "TCP-DNS-10.0.2.3-OK"
else
    echo "TCP-DNS-10.0.2.3-KO"
fi
ping -c1 -W2 10.0.2.2 2>&1 | tail -1
if wget -q -O /dev/null http://deb.debian.org/ 2>/dev/null; then
    echo "HTTP-OK"
else
    echo "HTTP-KO"
fi
echo NETFIX-DONE
