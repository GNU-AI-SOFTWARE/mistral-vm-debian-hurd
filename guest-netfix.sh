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

# Attach pfinet to eth0 with a static slirp configuration; -g sets
# the default gateway (see pfinet/options.c in the Hurd sources):
settrans -fg /servers/socket/2 /hurd/pfinet \
    -i /dev/eth0 -a 10.0.2.15 -m 255.255.255.0 -g 10.0.2.2 2>&1 \
|| settrans -a /servers/socket/2 /hurd/pfinet \
    -i eth0 -a 10.0.2.15 -m 255.255.255.0 -g 10.0.2.2 2>&1 \
|| echo "WARN: pfinet attach failed"

sleep 2
ifconfig -a

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
