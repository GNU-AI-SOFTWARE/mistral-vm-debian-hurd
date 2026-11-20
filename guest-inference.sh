#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Claire Ivanenka <claire@gnu-ai.org>
#
# Build and test GNU-AI-SOFTWARE/inference-translator on real
# GNU/Hurd, then mount the /inference translator and exercise it
# through plain POSIX commands.
#
#   python3 hurd_vm.py <image> guest-inference.sh --ram 2G
#
# Offline by default: the preinstalled image ships gcc and make but
# no autoconf/automake/git, and its network is unreliable (see
# guest-netfix.sh).  Pre-generate configure on the host, pack the
# tree and inject it into the CI image first:
#
#   git clone https://github.com/GNU-AI-SOFTWARE/inference-translator ~/inference-translator
#   cd ~/inference-translator && autoreconf -i && cd ~
#   tar czf inference-src.tar.gz inference-translator
#   ./inject-src.sh <image> ~/inference-src.tar.gz
#
# If /root/inference-src.tar.gz is missing but git and the network
# work, falls back to a git clone.

set -eu
export DEBIAN_FRONTEND=noninteractive

cd /root
if [ -f inference-src.tar.gz ]; then
    rm -rf inference-translator
    tar xf inference-src.tar.gz
elif command -v git >/dev/null 2>&1 \
     && getent hosts github.com >/dev/null 2>&1; then
    rm -rf inference-translator
    git clone https://github.com/GNU-AI-SOFTWARE/inference-translator
else
    echo "no /root/inference-src.tar.gz and no usable network:" >&2
    echo "run inject-src.sh on the host first (see header comment)" >&2
    exit 1
fi
cd inference-translator

./configure && make

# Mount the translator BEFORE make check: on Hurd, test_posix.sh then
# runs for real instead of skipping (exit 77); on other systems
# /inference stays unmounted and test_posix.sh skips as designed.
settrans -g /inference 2>/dev/null || true   # drop any stale mount
mkdir -p /inference
settrans -a /inference ./src/inference-translator

make check || { cat tests/test-suite.log 2>/dev/null || true; exit 1; }
# expected on Hurd: test_contract 36/36, test_fs 29/29, test_cli.sh OK,
#           test_posix.sh OK (mounted).  Note: test_posix.sh ends with
#           the task override set to "translate", which is sticky by
#           contract, so the manual demo below shows task=translate.

# Manual demo of the POSIX interface (make check already ran
# test_posix.sh against the mounted translator):
echo "résume https://example.org/article" | tee /inference/prompt
echo "--- request ---"; cat /inference/request; echo
echo "--- status ---"; cat /inference/status; echo
echo "--- urls ---";   cat /inference/urls; echo

echo translate | tee /inference/task
echo "--- request (override) ---"; cat /inference/request; echo   # must contain "task":"translate"

./src/inference-translator --version    # 0.1.0

settrans -g /inference
echo GUEST-INF-OK
