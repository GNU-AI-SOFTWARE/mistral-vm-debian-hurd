#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Claire Ivanenka <claire@gnu-ai.org>
#
# hurd-vm-clip.sh -- dedicated clipboard channel between the host and
# the kept Hurd VM, so copy-paste never goes through the terminal
# buffer (a paste into the wrong window once fed a whole heredoc into
# the host shell).
#
#   hurd-vm-clip.sh push   host clipboard  -> VM /root/.vmclipboard
#   hurd-vm-clip.sh pull   VM /root/.vmclipboard -> host clipboard
#   hurd-vm-clip.sh show   print the VM clipboard on the host
#
# In the VM, paste with:  $(cat /root/.vmclipboard)
# or just display it:    cat /root/.vmclipboard
#
# Requires on the host: ssh + xclip or xsel.  The VM must be running
# (hurd_vm.py --keep).  Env overrides:
#   HURD_VM_SSH_PORT   host port of the VM ssh forward (default 2222)
#   HURD_VM_SSH_KEY    private key (default ~/.ssh/hurd_vm_ed25519)

set -eu

PORT=${HURD_VM_SSH_PORT:-2222}
KEY=${HURD_VM_SSH_KEY:-$HOME/.ssh/hurd_vm_ed25519}
CLIP=/root/.vmclipboard
SSH="ssh -i $KEY -o IdentitiesOnly=yes -o PasswordAuthentication=no -o StrictHostKeyChecking=no -p $PORT root@127.0.0.1"

pick_clip_tool() {
    if command -v xclip >/dev/null 2>&1; then
        echo xclip
    elif command -v xsel >/dev/null 2>&1; then
        echo xsel
    else
        echo "hurd-vm-clip: need xclip or xsel on the host (apt install xclip)" >&2
        exit 1
    fi
}

clip_out() {    # host clipboard -> stdout
    if [ "$(pick_clip_tool)" = xclip ]; then
        xclip -o -selection clipboard
    else
        xsel -ob
    fi
}

clip_in() {     # stdin -> host clipboard
    if [ "$(pick_clip_tool)" = xclip ]; then
        xclip -selection clipboard -i
    else
        xsel -ib
    fi
}

case ${1:-} in
    push) clip_out | $SSH "cat > $CLIP" ;;
    pull) $SSH "cat $CLIP" | clip_in ;;
    show) $SSH "cat $CLIP" ;;
    *) echo "usage: $0 push|pull|show" >&2; exit 2 ;;
esac
