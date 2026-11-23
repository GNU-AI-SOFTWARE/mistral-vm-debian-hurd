#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Claire Ivanenka <claire@gnu-ai.org>

"""Headless Debian GNU/Hurd driver: prepare a scriptable CI copy of
the preinstalled disk image, run it under QEMU, drive the serial
console, and run a script inside the guest.

The preinstalled Debian GNU/Hurd disk image (from
https://cdimage.debian.org/cdimage/ports/) is designed to be used
with a graphical console.  This tool turns it into a fully
headless, scriptable machine - WITHOUT building a custom GRUB:

    1. a private copy of the image is prepared once (and cached):
       the ext2 root partition is fsck'ed and its own grub.cfg is
       patched to boot GNU Mach with console=com0.  The kernel, the
       boot script AND the getty (inittab runs "getty 38400
       console") then all use the serial line.

       We deliberately boot the image's own GRUB.  A custom El
       Torito GRUB built by grub-mkimage reliably breaks this
       image: GNU Mach starts, loads every module, then freezes
       forever right after "start pci-arbiter: " (the CPU falls
       back to the kernel idle loop, the first Hurd server never
       resumes the next one).  The very same image boots fine from
       its on-disk GRUB - so we patch that one instead.

    2. this driver logs in as root over the raw serial TCP socket
       (the preinstalled image ships with no root password) and
       feeds it a script.

Usage:
    hurd_vm.py <image>                  # boot to a login prompt
    hurd_vm.py <image> <script.sh>      # boot, then run the script
    options: --ram 2G   guest memory (default 1G)
             --fresh    rebuild the CI copy even if cached
             --keep     keep the VM alive after the script (QEMU is
                                   started in its own session, so it also
                                   survives the driver and the terminal;
                                   the guest sshd is started before exit)
             --ssh-key FILE
                                   authorize FILE (*.pub) for root SSH
                                   logins on the CI copy; the host then
                                   connects with:
                                   ssh -i <private key> -p 2222 root@127.0.0.1
             --root-pass PASSWORD   the image's root password
                                   (only needed for custom images)
             --set-root-pass PW     set root's password to PW on the
                                   CI copy (hash in /etc/shadow, sshd
                                   configured for root password
                                   logins); the driver then logs in
                                   with it over the serial line

The script runs as root inside the guest; its output arrives on the
serial line (captured in serial.log and echoed here).  The guest
gets user-mode networking (10.0.2.0/24, DNS 10.0.2.3) and a 2222
host forward to its SSH port.  The prepared CI copy is kept between
runs (named <image>.ci.img next to this file): packages installed
by earlier scripts stay installed; pass --fresh to rebuild it.

Requires on the host: qemu-system-x86 (KVM used when /dev/kvm is
available), e2fsprogs (e2fsck, debugfs), python3.  The guest is
given 1 GB of RAM by default: it boots fine with that, and on a
memory-tight host a bigger allocation kills the guest silently
before even SeaBIOS runs.
"""

import hashlib
import os
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# ------------------------------------------------------------------
# Parameters.
#
# The image itself is described by its own GRUB configuration, so no
# kernel name or UUID is needed here: the patch below simply appends
# the serial console argument to every GNU Mach multiboot line.
IMAGE_URL = ("https://cdimage.debian.org/cdimage/ports/latest/"
             "hurd-amd64/debian-hurd-amd64-20260314.img.tar.xz")

CONSOLE_ARG = "console=com0"     # serial kernel console (COM1)

TIMEOUT_LOGIN = 900       # seconds until the login prompt
TIMEOUT_SCRIPT = 3600     # seconds for the whole guest script

# Prepared copy of the image.  Set HURD_VM_CI_IMAGE to boot a
# private one instead of the shared default — two drivers may run
# concurrently, each on its own copy (mind the RAM: 1 GB each).
CI_IMAGE = os.environ.get("HURD_VM_CI_IMAGE",
                          os.path.join(HERE, "debian-hurd-ci.img"))
SERIAL_LOG = os.environ.get("HURD_VM_SERIAL_LOG",
                            os.path.join(HERE, "serial.log"))
QEMU_LOG = os.environ.get("HURD_VM_QEMU_LOG",
                          os.path.join(HERE, "qemu.log"))

# ------------------------------------------------------------------
# Small host-tool helpers.


def find_tool(name):
    """Locate a host binary, looking in /usr/sbin and /sbin too
    (e2fsprogs lives there and is not always in PATH)."""
    candidates = ([shutil.which(name)] if shutil.which(name) else []) \
        + [os.path.join(d, name) for d in ("/usr/sbin", "/sbin")]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    sys.exit(f"[vm] FAIL: required tool not found: {name} "
             f"(Debian: apt install e2fsprogs)")


def sha512_crypt(password):
    """SHA-512 crypt hash ($6$...) of PASSWORD for /etc/shadow: via
    the host openssl (the password is fed on stdin, never on the
    command line), falling back to Python's crypt module."""
    openssl = shutil.which("openssl")
    if openssl:
        salt = secrets.token_hex(8)
        r = subprocess.run([openssl, "passwd", "-6", "-salt", salt,
                            "-stdin"], input=password,
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.startswith("$6$"):
            return r.stdout.strip()
    try:
        import crypt
        return crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    except ImportError:
        sys.exit("[vm] FAIL: no way to hash the password: need openssl "
                 "or Python's crypt module")


def replace_fs_file(debugfs, part, fs_path, content, needle):
    """Replace the file FS_PATH inside the ext2 image PART with
    CONTENT, then verify by reading it back: debugfs always exits 0,
    even when a command failed (rm + write, since "write" refuses to
    overwrite an existing file)."""
    local = part + ".file"
    with open(local, "w") as f:
        f.write(content)
    d, base = fs_path.rsplit("/", 1)
    cmds = part + ".cmds"
    with open(cmds, "w") as f:
        f.write(f"cd {d}\nrm {base}\nwrite {local} {base}\n")
    subprocess.run([debugfs, "-w", "-f", cmds, part],
                   check=True, capture_output=True)
    r = subprocess.run([debugfs, "-R", f"cat {fs_path}", part],
                       capture_output=True, text=True)
    for tmp in (local, cmds):
        os.unlink(tmp)
    if needle not in r.stdout:
        sys.exit(f"[vm] FAIL: the {fs_path} patch did not land in the "
                 f"image\n" + r.stdout[-2000:])


# ------------------------------------------------------------------
# Preparing the CI copy of the image.


def root_partition(image):
    """Return (start_lba, sectors) of the largest Linux (0x83)
    partition of an MBR-partitioned disk image: the Hurd root."""
    with open(image, "rb") as f:
        mbr = f.read(512)
    if len(mbr) < 512 or mbr[510:512] != b"\x55\xaa":
        sys.exit("[vm] FAIL: no MBR signature in the image")
    best = None
    for i in range(4):
        entry = mbr[446 + 16 * i: 446 + 16 * (i + 1)]
        if entry[4] == 0x83:                      # Linux filesystem
            lba, size = struct.unpack("<II", entry[8:16])
            if best is None or size > best[1]:
                best = (lba, size)
    if best is None:
        sys.exit("[vm] FAIL: no Linux partition in the image")
    return best


def patch_grub_cfg(text):
    """Append the serial console argument to every GNU Mach
    multiboot line of a grub.cfg (idempotent)."""
    out = []
    patched = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if (stripped.startswith("multiboot") and "gnumach" in line
                and CONSOLE_ARG not in line):
            line = line.rstrip() + " " + CONSOLE_ARG + "\n"
            patched += 1
        out.append(line)
    return "".join(out), patched


def prepare_ci_image(src, fresh=False, root_pass=None, ssh_key=None):
    """Create (or reuse) the bootable CI copy of the image.

    The marker file besides the copy holds two lines: the source
    fingerprint, and "clean" or "dirty" - whether the copy was shut
    down properly.  A dirty copy is fsck'ed again before use: an
    unclean shutdown (a killed VM, a crashed driver) leaves the
    root filesystem inconsistent, and the guest would drop to an
    fsck maintenance shell instead of the login prompt.

    With root_pass set, root's password hash in /etc/shadow is set
    to that password instead of being cleared, and sshd is allowed
    root password logins; the marker fingerprint then includes a
    hash of the password, so changing it rebuilds the copy.

    With ssh_key set, that public key is installed as
    /root/.ssh/authorized_keys for root key logins; the marker
    fingerprint then also includes a hash of the key, so changing
    either rebuilds the copy.
    """
    marker = CI_IMAGE + ".ready"
    src_stat = os.stat(src)
    tag = "nopass" if root_pass is None else \
        "pass-" + hashlib.sha256(root_pass.encode()).hexdigest()[:12]
    if ssh_key is not None:
        with open(ssh_key, "rb") as f:
            tag += "-key-" + hashlib.sha256(f.read()).hexdigest()[:12]
    fingerprint = f"{os.path.getsize(src)}:{int(src_stat.st_mtime)}:{tag}"
    e2fsck = find_tool("e2fsck")
    debugfs = find_tool("debugfs")
    lba, sectors = root_partition(src)

    def state_of_marker():
        try:
            with open(marker) as f:
                lines = f.read().split()
            return lines[0], (lines[1] if len(lines) > 1 else "dirty")
        except OSError:
            return None, "dirty"

    def write_marker(state):
        with open(marker, "w") as f:
            f.write(fingerprint + " " + state + "\n")

    def fsck_copy():
        """Repair the root filesystem of the CI copy in place."""
        part = CI_IMAGE + ".part"
        subprocess.run(["dd", f"if={CI_IMAGE}", f"of={part}",
                        "bs=512", f"skip={lba}", f"count={sectors}",
                        "status=none"], check=True)
        r = subprocess.run([e2fsck, "-fy", part],
                           capture_output=True, text=True)
        if r.returncode >= 4:        # 1-3 = fixed, >=4 = unfixable
            os.unlink(part)
            sys.exit("[vm] FAIL: e2fsck could not repair the root "
                     "filesystem:\n" + r.stdout[-2000:])
        subprocess.run(["dd", f"if={part}", f"of={CI_IMAGE}",
                        "bs=512", f"seek={lba}", "conv=notrunc",
                        "status=none"], check=True)
        os.unlink(part)

    old_print, old_state = state_of_marker()
    if not fresh and old_print == fingerprint and os.path.isfile(CI_IMAGE):
        if old_state != "clean":
            print("[vm] the CI copy was not shut down cleanly: "
                  "repairing its filesystem")
            fsck_copy()
            write_marker("clean")
        else:
            print(f"[vm] reusing the prepared CI copy: {CI_IMAGE}")
        return CI_IMAGE

    print(f"[vm] preparing the CI copy (once): {CI_IMAGE}")
    if os.path.exists(CI_IMAGE):
        os.unlink(CI_IMAGE)
    # A plain cp bypasses QEMU's image locking (an overlay would
    # take a lock the running VM refuses) and keeps the holes.
    subprocess.run(["cp", "--sparse=always", src, CI_IMAGE], check=True)

    # Extract the root partition, fsck it (a previously killed VM
    # leaves it dirty - the guest would drop to a maintenance
    # shell), patch grub.cfg, and put it back in place.
    part = CI_IMAGE + ".part"
    subprocess.run(["dd", f"if={CI_IMAGE}", f"of={part}",
                    "bs=512", f"skip={lba}", f"count={sectors}",
                    "status=none"], check=True)
    print("[vm] checking and patching the root filesystem")
    r = subprocess.run([e2fsck, "-fy", part],
                       capture_output=True, text=True)
    if r.returncode >= 4:        # 1-3 = fixed, >=4 = unfixable
        os.unlink(part)
        sys.exit("[vm] FAIL: e2fsck could not repair the root "
                 "filesystem:\n" + r.stdout[-2000:])

    cfg_path = part + ".grub.cfg"
    subprocess.run([debugfs, "-R", "dump /boot/grub/grub.cfg " + cfg_path,
                    part], check=True, capture_output=True)
    with open(cfg_path) as f:
        cfg = f.read()
    patched_cfg, count = patch_grub_cfg(cfg)
    if count == 0:
        os.unlink(part)
        sys.exit("[vm] FAIL: no GNU Mach multiboot line found in the "
                 "image grub.cfg - is this a Debian GNU/Hurd image?")
    print(f"[vm] serial console enabled on {count} boot entr"
          f"{'y' if count == 1 else 'ies'} ({CONSOLE_ARG})")
    with open(cfg_path, "w") as f:
        f.write(patched_cfg)

    # debugfs "write" creates the file in its *current* directory,
    # so cd to /boot/grub inside the same command file.
    cmds = part + ".debugfs.cmds"
    with open(cmds, "w") as f:
        f.write("cd /boot/grub\n"
                "rm grub.cfg\n"
                f"write {cfg_path} grub.cfg\n")
    subprocess.run([debugfs, "-w", "-f", cmds, part],
                   check=True, capture_output=True)

    # debugfs always exits 0, even when a command failed: verify
    # that the patched configuration really is in the filesystem.
    r = subprocess.run([debugfs, "-R", "cat /boot/grub/grub.cfg", part],
                       capture_output=True, text=True)
    if CONSOLE_ARG not in r.stdout:
        os.unlink(part)
        sys.exit("[vm] FAIL: the grub.cfg patch did not land in the "
                 "image\n" + r.stdout[-2000:])

    # The driver logs in as root: that is its CI contract.  By
    # default the older preinstalled images shipped an empty root
    # password, the newer ones do not - so the CI COPY (never the
    # source) gets root's password hash CLEARED in /etc/shadow,
    # exactly like the grub.cfg patch above.  With --set-root-pass
    # the SHA-512 hash of that password is written instead, and
    # sshd is configured for root password logins (the 2222 host
    # forward), so "ssh -p 2222 root@127.0.0.1" works while the VM
    # runs.  Idempotent, verified after the write; a copy without
    # /etc/shadow (none known) keeps whatever login policy it has.
    shadow_path = part + ".shadow"
    subprocess.run([debugfs, "-R", "dump /etc/shadow " + shadow_path,
                    part], check=True, capture_output=True)
    with open(shadow_path) as f:
        shadow = f.read()
    if root_pass is None:
        field, action = "", "cleared"
    else:
        field, action = sha512_crypt(root_pass), "set"
    new_shadow, n = re.subn(r"(?m)^root:[^:]*:",
                            lambda m: "root:" + field + ":",
                            shadow, count=1)
    if n == 1 and new_shadow != shadow:
        replace_fs_file(debugfs, part, "/etc/shadow", new_shadow,
                        "root:" + field + ":")
        print(f"[vm] root password {action} on the CI copy")
    else:
        print(f"[vm] root password already {action} on the CI copy")
    os.unlink(shadow_path)

    if root_pass is not None:
        # Root password logins over SSH: in sshd_config the FIRST
        # occurrence of a keyword wins, so prepend the directives -
        # that also overrides any sshd_config.d include.
        sshd_path = part + ".sshd_config"
        subprocess.run([debugfs, "-R",
                        "dump /etc/ssh/sshd_config " + sshd_path,
                        part], check=True, capture_output=True)
        if os.path.isfile(sshd_path) and os.path.getsize(sshd_path) > 0:
            with open(sshd_path) as f:
                sshd = f.read()
            header = "PermitRootLogin yes\nPasswordAuthentication yes\n"
            if not sshd.startswith(header):
                replace_fs_file(debugfs, part, "/etc/ssh/sshd_config",
                                header + sshd, "PermitRootLogin yes")
                print("[vm] sshd: root password logins enabled "
                      "on the CI copy")
            os.unlink(sshd_path)
        else:
            print("[vm] note: no /etc/ssh/sshd_config in the image; "
                  "root SSH password logins may stay disabled")

    if ssh_key is not None:
        # Root SSH key login: install the public key as
        # /root/.ssh/authorized_keys (dir 0700, file 0600) with debugfs,
        # like the shadow/sshd patches above.  PubkeyAuthentication is
        # on by default; the header above allows root logins.
        with open(ssh_key) as f:
            pub = f.read().strip()
        if not pub or " " not in pub:
            sys.exit(f"[vm] FAIL: {ssh_key} does not look like a public key")
        key_host = part + ".authorized_keys"
        cmds_key = part + ".ssh.cmds"
        with open(key_host, "w") as f:
            f.write(pub + "\n")
        with open(cmds_key, "w") as f:
            f.write("cd /root\n"
                    "mkdir .ssh\n"
                    "cd .ssh\n"
                    "rm authorized_keys\n"
                    f"write {key_host} authorized_keys\n"
                    "sif authorized_keys mode 0100600\n"
                    "sif /root/.ssh mode 040700\n")
        subprocess.run([debugfs, "-w", "-f", cmds_key, part],
                       check=True, capture_output=True)
        r = subprocess.run([debugfs, "-R", "cat /root/.ssh/authorized_keys",
                            part], capture_output=True, text=True)
        os.unlink(key_host)
        os.unlink(cmds_key)
        if pub not in r.stdout:
            sys.exit("[vm] FAIL: the authorized_keys patch did not land "
                     "in the image:\n" + r.stdout[-500:])
        print(f"[vm] ssh key {os.path.basename(ssh_key)} authorized "
              "for root on the CI copy")

    subprocess.run(["dd", f"if={part}", f"of={CI_IMAGE}",
                    "bs=512", f"seek={lba}", "conv=notrunc",
                    "status=none"], check=True)
    for tmp in (part, cfg_path, cmds):
        try:
            os.unlink(tmp)
        except OSError:
            pass          # already removed above (e.g. cmds after the shadow patch)

    with open(marker, "w") as f:
        f.write(fingerprint + " clean\n")
    return CI_IMAGE


# ------------------------------------------------------------------
# A virtual machine is already running with the image?


def find_running_vms(image):
    """Return the PIDs of the QEMU processes holding IMAGE open.

    Implemented by scanning /proc/*/fd (no external tools, works for
    the processes of the current user).
    """
    image = os.path.abspath(image)
    pids = []
    try:
        proc = os.listdir("/proc")
    except OSError:
        return pids
    for pid in proc:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
            if "qemu" not in comm:
                continue
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    target = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if target == image:
                    pids.append(int(pid))
                    break
        except OSError:
            continue
    return sorted(pids)


# ------------------------------------------------------------------


class SerialVM:
    """QEMU with the serial line on a local TCP socket (raw bytes,
    not Telnet: the telnet chardev's IAC negotiation proved flaky).

    QEMU's stderr is captured in qemu.log and echoed whenever the
    process dies - never swallow the reason again.
    """

    def __init__(self, disk: str, memory: str = "1G"):
        # Pick a free ephemeral port: no conflict with a lingering VM.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        self.port = probe.getsockname()[1]
        probe.close()
        self.disk = disk
        self.memory = memory
        self.proc = None
        self.log = b""
        self.sock = None

    def _base_cmd(self, accel):
        # Boot the disk itself: the image's own GRUB, with the
        # patched grub.cfg, handles everything from there.
        # The host port forwarded to the guest SSH is configurable:
        # two concurrent VMs cannot share it (HURD_VM_SSH_PORT).
        ssh_port = os.environ.get("HURD_VM_SSH_PORT", "2222")
        return [
            "qemu-system-x86_64", *accel, "-m", self.memory,
            # Two vCPUs: the guests compile (the Hurd, Mach) and a
            # single core makes every build painfully slow.
            "-smp", "2", "-no-reboot",
            "-drive", f"file={self.disk},cache=writeback",
            "-net", f"user,hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
            "-net", "nic,model=e1000",
            "-display", "none",
            "-serial", f"tcp:127.0.0.1:{self.port},server,nowait",
        ]

    def start(self, accel=None):
        # Bounded fallback: /dev/kvm may exist but be unusable (e.g. CI
        # runners: "failed to initialize kvm: Permission denied"), so
        # retry exactly once with TCG, never recursively.
        if accel is None:
            # accel is a LIST of argv items: "-accel tcg" must stay two
            # separate arguments, otherwise QEMU says "invalid option".
            accel = ["-enable-kvm"] if os.path.exists("/dev/kvm") else ["-accel", "tcg"]
        with open(QEMU_LOG, "wb") as errf:
            # start_new_session + stdin=DEVNULL: with --keep the driver
            # exits and leaves QEMU running; a new session makes that
            # QEMU immune to the SIGHUP sent to the terminal's process
            # group when the invoking shell or session ends, so the
            # SSH hostfwd stays up instead of dying with "connection
            # refused".
            self.proc = subprocess.Popen(self._base_cmd(accel),
                                         stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL,
                                         stderr=errf,
                                         start_new_session=True)
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    self.sock = socket.create_connection(
                        ("127.0.0.1", self.port), timeout=5)
                    self.sock.settimeout(5)
                    return
                except OSError:
                    if self.proc.poll() is not None:
                        reason = self._qemu_tail()
                        if (accel == ["-enable-kvm"]
                                and ("kvm" in reason.lower()
                                     or "could not access" in reason.lower())):
                            print(f"[vm] KVM failed, falling back to "
                                  f"TCG:\n{reason}")
                            accel = ["-accel", "tcg"]
                            errf.close()
                            return self.start(["-accel", "tcg"])
                        if "Failed to get" in reason and "lock" in reason:
                            pids = find_running_vms(self.disk)
                            raise RuntimeError(
                                "virtual machine already running: "
                                + (", ".join(f"pid {p}" for p in pids)
                                   if pids else "unknown pid")
                                + " is using " + self.disk
                                + "; close it first")
                        raise RuntimeError("QEMU exited early:\n" + reason)
                    time.sleep(0.5)
            raise RuntimeError("serial port never came up:\n" + self._qemu_tail())

    def _qemu_tail(self) -> str:
        try:
            with open(QEMU_LOG, "rb") as f:
                return f.read().decode("latin-1", "replace")[-2000:]
        except OSError:
            return "(no QEMU stderr captured)"

    def read_some(self, wait: float = 5.0) -> bytes:
        self.sock.settimeout(wait)
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return b""
        self.log += data
        return data

    def wait_for(self, pattern: str, timeout: int) -> bool:
        rx = re.compile(pattern.encode())
        deadline = time.time() + timeout
        while time.time() < deadline:
            if rx.search(self.log):
                return True
            self.read_some(5)
            if self.proc.poll() is not None:
                raise RuntimeError("QEMU exited during wait for " + pattern)
        return False

    def send(self, line: str):
        self.sock.sendall(line.encode() + b"\r")

    def stop(self):
        if self.sock:
            self.sock.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ------------------------------------------------------------------


def parse_args(argv):
    opts = {"ram": "1G", "fresh": False, "keep": False, "root_pass": None,
            "set_root_pass": None, "ssh_key": None}
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--fresh":
            opts["fresh"] = True
        elif a == "--keep":
            opts["keep"] = True
        elif a == "--ram":
            i += 1
            opts["ram"] = argv[i]
        elif a == "--root-pass":
            i += 1
            opts["root_pass"] = argv[i]
        elif a == "--set-root-pass":
            i += 1
            opts["set_root_pass"] = argv[i]
        elif a == "--ssh-key":
            i += 1
            opts["ssh_key"] = argv[i]
        else:
            rest.append(a)
        i += 1
    return rest, opts


def run(src, script, opts):

    if not os.path.isfile(src):
        print(f"[vm] FAIL: no such image: {src}")
        return 2
    if opts["set_root_pass"] is not None and opts["root_pass"] is None:
        # The driver logs in over the serial line with the password it
        # just set on the CI copy.
        opts["root_pass"] = opts["set_root_pass"]
    disk = prepare_ci_image(src, opts["fresh"], root_pass=opts["set_root_pass"],
                            ssh_key=opts["ssh_key"])
    marker = disk + ".ready"

    # From now on the copy is "dirty" until the guest has powered
    # off cleanly: a crash or a kill must not leave the marker
    # lying about a clean filesystem.
    with open(marker) as f:
        fingerprint = f.read().split()[0]
    with open(marker, "w") as f:
        f.write(fingerprint + " dirty\n")

    vm = SerialVM(disk, memory=opts["ram"])
    vm.start()
    rc = 1
    try:
        print(f"[vm] waiting for the login prompt "
              f"(up to {TIMEOUT_LOGIN}s)...")
        if not vm.wait_for(r"login:", TIMEOUT_LOGIN):
            print("[vm] FAIL: no login prompt on the serial console")
            return 1
        print("[vm] login prompt reached, logging in as root")
        vm.send("root")
        # The stock image has no root password; custom images may.
        if not vm.wait_for(r"Password:|root@|# ", 120):
            print("[vm] FAIL: no response after the login name")
            return 1
        if re.search(rb"Password:", vm.log) and opts["root_pass"]:
            vm.send(opts["root_pass"])
        elif re.search(rb"Password:", vm.log):
            print("[vm] FAIL: the image wants a root password; "
                  "pass --root-pass PASSWORD")
            return 1
        if not vm.wait_for(r"root@|# ", 120):
            print("[vm] FAIL: no shell prompt after login")
            return 1

        if script is None:
            print("[vm] interactive boot complete")
            rc = 0
        else:
            with open(script) as f:
                body = f.read()
            vm.log = b""
            # Feed the script through a heredoc, then run it.
            #
            # The serial line has no flow control: lines sent
            # back-to-back overflow the guest tty buffer and the
            # heredoc silently loses bytes (the shell then never
            # sees its GUESTEOF terminator).  Each line is
            # therefore paced by its own echo: we wait until the
            # guest has echoed it back before sending the next.
            def send_paced(line):
                before = len(vm.log)
                vm.send(line)
                needle = line.strip().encode("utf-8", "replace")[-40:]
                deadline = time.time() + 15
                while (needle and time.time() < deadline
                       and needle not in vm.log[before:]):
                    vm.read_some(1)
                time.sleep(0.05)

            send_paced("cat > /tmp/guest.sh <<'GUESTEOF'")
            for line in body.splitlines():
                send_paced(line)
            send_paced("GUESTEOF")
            vm.send("sh /tmp/guest.sh; echo GUESTRC=$?")
            if not vm.wait_for(r"GUESTRC=(\d+)", TIMEOUT_SCRIPT):
                print("[vm] FAIL: the guest script did not complete")
            else:
                m = re.search(rb"GUESTRC=(\d+)", vm.log)
                rc = 0 if int(m.group(1)) == 0 else 1
            print(vm.log.decode("latin-1", "replace")[-4000:])
        if opts["keep"]:
            # The serial TCP socket closes when this driver exits, so the
            # SSH hostfwd is the only way back into a kept VM: bring the
            # guest sshd up and verify it answers locally before leaving
            # (sshd down -> "Connection closed" on the host port; eth0
            # unconfigured -> "Connection reset" at kex time).
            vm.send("mkdir -p /run/sshd; ssh-keygen -A >/dev/null 2>&1; "
                    "service ssh start >/dev/null 2>&1 || /usr/sbin/sshd; "
                    "sleep 1; pgrep -l sshd; "
                    "ifconfig eth0 2>/dev/null | grep -E 'eth0|inet ' | head -3; "
                    "bash -c 'exec 3<>/dev/tcp/127.0.0.1/22 && head -c 30 <&3'; "
                    "echo; echo SSHD-RC=$?")
            try:
                if vm.wait_for(r"SSHD-RC=\d+", 90):
                    print("[vm] guest sshd check (serial tail):")
                    print(vm.log.decode("latin-1", "replace")[-500:])
                    key = opts.get("ssh_key")
                    hint = (" -i " + key) if key else ""
                    print("[vm] kept VM reachable: ssh" + hint + " -p "
                          + os.environ.get("HURD_VM_SSH_PORT", "2222")
                          + " root@127.0.0.1")
            except RuntimeError:
                print("[vm] warning: the guest sshd check did not answer")
        if not opts["keep"]:
            # Shut the guest down cleanly and WAIT for it, even when
            # the guest script failed: killing QEMU mid-shutdown
            # leaves the filesystem inconsistent and the next run
            # pays a full e2fsck repair.  A powered-off guest makes
            # QEMU exit by itself (ACPI), so watch the process
            # instead of guessing a delay.
            vm.send("poweroff")
            print("[vm] waiting for the guest to power off...")
            deadline = time.time() + 300
            while time.time() < deadline and vm.proc.poll() is None:
                vm.read_some(5)
            if vm.proc.poll() is None:
                print("[vm] warning: the guest did not power off in "
                      "time; the CI copy will be repaired next run")
            else:
                print("[vm] guest powered off cleanly")
                with open(marker, "w") as f:
                    f.write(fingerprint + " clean\n")
    finally:
        with open(SERIAL_LOG, "wb") as logf:
            logf.write(vm.log)
        # --keep leaves QEMU running when the driver exits (the guest
        # is NOT powered off: the marker stays "dirty" and the next
        # run repairs the filesystem); the serial TCP socket closes
        # with the driver but the 2222 SSH forward stays up.
        if not opts["keep"]:
            vm.stop()
        print("[vm] serial console saved to serial.log")
    return rc


def main():
    # Report progress live even when the output is redirected
    # (background runs), instead of buffering until the end.
    sys.stdout.reconfigure(line_buffering=True)
    rest, opts = parse_args(sys.argv[1:])
    if not rest:
        print(__doc__)
        return 2
    src = rest[0]
    script = rest[1] if len(rest) > 1 else None
    try:
        return run(src, script, opts)
    except Exception as e:
        print(f"[vm] {e}")
        try:
            with open(SERIAL_LOG, "rb") as f:
                tail = f.read()[-3000:]
            print("---- serial console (tail) ----")
            print(tail.decode("latin-1", "replace"))
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
