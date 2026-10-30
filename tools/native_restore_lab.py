#!/usr/bin/env python3
"""Run a local, isolated HAOS recovery lab with QEMU on Apple Silicon."""
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2] / "native-backup-proof"
IMAGE = "haos_generic-aarch64-18.4.qcow2.xz"
IMAGE_SHA256 = "1616dd368542591f7b144fc4b21fae51c8d1d23b4545a5b3904011d11ed724a3"
FIRMWARE = Path("/opt/homebrew/share/qemu")
QEMU = Path("/opt/homebrew/bin/qemu-system-aarch64")
QEMU_IMG = Path("/opt/homebrew/bin/qemu-img")
ALLOWED = {IMAGE, "disk.qcow2", "vars.fd", "state.json", "qemu.log", "qemu.pid", "serial.sock", "qmp.sock"}


def fail(message):
    raise SystemExit(message)


def load_state(root):
    try:
        return json.loads((root / "state.json").read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit("Lab is not prepared or its state is invalid") from exc


def save_state(root, state):
    tmp = root / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(root / "state.json")


def qmp(root, command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(5)
        s.connect(str(root / "qmp.sock"))
        f = s.makefile("rwb", buffering=0)
        json.loads(f.readline())
        f.write(b'{"execute":"qmp_capabilities"}\n')
        json.loads(f.readline())
        f.write(json.dumps({"execute": command}).encode() + b"\n")
        while line := f.readline():
            result = json.loads(line)
            if "return" in result or "error" in result:
                if "error" in result:
                    raise RuntimeError("QMP command failed")
                return result["return"]
        raise RuntimeError("QMP disconnected")


def running(root):
    try:
        qmp(root, "query-status")
        return True
    except (OSError, ValueError, RuntimeError):
        return False


def prepare(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    if (root / "state.json").exists() or (root / "disk.qcow2").exists():
        fail("Lab already prepared; refusing to overwrite it")
    if running(root):
        fail("Lab is running")
    image = root / IMAGE
    h = hashlib.sha256()
    with image.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            h.update(chunk)
    if h.hexdigest() != IMAGE_SHA256:
        fail("HAOS image SHA-256 mismatch; image may be incomplete")
    disk = root / "disk.qcow2"
    try:
        with lzma.open(image, "rb") as source, disk.open("xb") as out:
            os.chmod(disk, 0o600)
            shutil.copyfileobj(source, out, 1024 * 1024)
        subprocess.run([str(QEMU_IMG), "resize", "-f", "qcow2", str(disk), "64G"], check=True)
        shutil.copyfile(FIRMWARE / "edk2-arm-vars.fd", root / "vars.fd")
        os.chmod(root / "vars.fd", 0o600)
        save_state(root, {"schema_version": 1, "restore_locked": False, "starts": 0, "last_network": None})
    except BaseException:
        disk.unlink(missing_ok=True)
        (root / "vars.fd").unlink(missing_ok=True)
        raise
    print(f"Prepared {disk}")


def preload_is_safe(root, state):
    # Only this explicit set of lab-created files can be present for preload.
    if state.get("restore_locked") or state.get("last_network") == "isolated":
        fail("Preload network is forbidden after isolated or restore state")
    unexpected = [p.name for p in root.iterdir() if p.name not in ALLOWED or p.is_symlink()]
    if unexpected:
        fail("Preload network forbidden: unrecognized files present in lab directory")


def start(root, preload, debug_ssh):
    state = load_state(root)
    if running(root):
        fail("Lab is already running")
    if preload:
        preload_is_safe(root, state)
    for name in ("qmp.sock", "serial.sock", "qemu.pid"):
        (root / name).unlink(missing_ok=True)
    net = "user,id=net0,hostfwd=tcp:127.0.0.1:18123-:8123"
    if not preload:
        net += ",restrict=on"
    if debug_ssh:
        net += ",hostfwd=tcp:127.0.0.1:22222-:22222"
    cmd = [str(QEMU), "-machine", "virt,accel=hvf", "-cpu", "host", "-smp", "4", "-m", "8192", "-display", "none", "-monitor", "none", "-no-reboot", "-drive", f"if=pflash,format=raw,readonly=on,file={FIRMWARE / 'edk2-aarch64-code.fd'}", "-drive", f"if=pflash,format=raw,file={root / 'vars.fd'}", "-drive", f"if=none,id=disk,format=qcow2,file={root / 'disk.qcow2'}", "-device", "virtio-blk-pci,drive=disk", "-netdev", net, "-device", "virtio-net-pci,netdev=net0", "-serial", f"unix:{root / 'serial.sock'},server=on,wait=off", "-qmp", f"unix:{root / 'qmp.sock'},server=on,wait=off", "-pidfile", str(root / "qemu.pid")]
    state.update(starts=state["starts"] + 1, last_network="preload" if preload else "isolated")
    # Persist before launch: failures also prevent returning to a less isolated mode.
    save_state(root, state)
    with (root / "qemu.log").open("ab") as log:
        os.chmod(root / "qemu.log", 0o600)
        process = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    for _ in range(100):
        if running(root):
            print(f"Lab started (pid {process.pid}, network {'preload' if preload else 'isolated'}); UI http://127.0.0.1:18123")
            return
        if process.poll() is not None:
            fail(f"QEMU exited with status {process.returncode}; inspect {root / 'qemu.log'}")
        time.sleep(0.1)
    fail(f"QEMU did not expose QMP; inspect {root / 'qemu.log'}")


def stop(root):
    if not running(root):
        print("Lab is not running")
        return
    qmp(root, "system_powerdown")
    for _ in range(150):
        if not running(root):
            print("Lab stopped")
            return
        time.sleep(0.2)
    qmp(root, "quit")
    print("Lab stopped after shutdown timeout")


def console_command(root, command, timeout):
    if "\n" in command or "\r" in command:
        fail("Console command must be a single line")
    if not running(root):
        fail("Lab is not running")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(root / "serial.sock"))
        s.settimeout(0.2)
        s.sendall(command.encode() + b"\n")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = s.recv(65536)
                if not data:
                    break
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except TimeoutError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("prepare")
    start_parser = sub.add_parser("start")
    start_parser.add_argument("--preload-network", action="store_true")
    start_parser.add_argument("--debug-ssh", action="store_true")
    sub.add_parser("stop")
    sub.add_parser("status")
    sub.add_parser("lock-restore", help="permanently forbid network preload before introducing restore material")
    console = sub.add_parser("console-command")
    console.add_argument("command")
    console.add_argument("--timeout", type=float, default=5)
    args = parser.parse_args()
    root = args.workdir.resolve()
    if args.action == "prepare":
        prepare(root)
    elif args.action == "start":
        start(root, args.preload_network, args.debug_ssh)
    elif args.action == "stop":
        stop(root)
    elif args.action == "status":
        state = load_state(root)
        print(json.dumps({**state, "running": running(root), "workdir": str(root)}, indent=2))
    elif args.action == "lock-restore":
        if running(root):
            fail("Stop the lab before locking it for restore")
        state = load_state(root)
        state["restore_locked"] = True
        save_state(root, state)
        print("Restore lock set; network preload is permanently disabled")
    else:
        console_command(root, args.command, args.timeout)


if __name__ == "__main__":
    main()
