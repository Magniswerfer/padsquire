# SPDX-License-Identifier: AGPL-3.0-or-later
"""Update mode in the settings app on macOS: a small helper that runs as administrator.

macOS only lets an administrator take the update-mode disk away from its own disk driver, and it
resets the controller (which then leaves update mode) whenever a program lets go of it. So for one
update session the app starts this helper once, through macOS's own password dialog
(`osascript ... with administrator privileges`). The helper takes the disk, keeps it, and passes
the app's SCSI commands to it over a Unix socket that only the user can reach, until the
controller restarts.

It forwards only what Linux's tools may send too: INQUIRY and JieLi's commands in jlboot.ALLOWED
plus jlwrite's sector erase and flash write. Everything that decides what gets written (the
reserved areas, the backup checks) stays in the app, as on Linux. FC12, the chip-key write, is
refused here as everywhere else.
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

from . import jlboot, jlwrite, paths

SOCKET_DIR = paths.cache_home() / paths.NAME / "helper"
SOCKET = SOCKET_DIR / "helper.sock"
LOG = SOCKET_DIR / "helper.log"

INQUIRY = 0x12
FORWARDED = jlboot.ALLOWED | {jlwrite.ERASE_SECTOR, jlwrite.WRITE_FLASH}
NEVER = 0xFC12                                   # chip key write: burns eFuses, irreversible
WAIT_FOR_DEVICE = 90.0                            # seconds after start for update mode to appear
SETTLE = 2.0                                      # seconds to leave a new update-mode disk to macOS first
IDLE = 20 * 60                                    # quit after this long without a command
KEEPALIVE = 2.0                                   # seconds between INQUIRYs while nothing else is sent
INQUIRY_CDB = bytes([INQUIRY, 0, 0, 0, 36, 0])

# What macOS's password dialog says, under its own "PadSquire wants to make changes."
PROMPT = ("PadSquire needs administrator rights to use the controller's update mode: only an "
          "administrator may take a USB disk away from macOS. It asks once for this update.")


class HelperError(jlboot.JLBootError):
    """Raised on the app's side; a JLBootError, so a firmware step that hits it fails cleanly."""


def allowed(cdb: bytes) -> bool:
    if not cdb:
        return False
    if cdb[0] == INQUIRY and len(cdb) == 6:
        return True
    cmd = int.from_bytes(cdb[:2], "big")
    return cmd != NEVER and cmd in FORWARDED


# -- the app's side ------------------------------------------------------------------------------

def running(sock_path: Path = SOCKET) -> bool:
    try:
        with _connect(sock_path, timeout=2) as s:
            return _call(s, {"op": "ping"}).get("ok", False)
    except (OSError, HelperError):
        return False


def start(sock_path: Path = SOCKET, timeout: float = 180.0) -> None:
    """Start the helper through macOS's password dialog, unless it runs already. Returns once it
    answers. Raises HelperError when the dialog is cancelled or the helper doesn't come up."""
    if running(sock_path):
        return
    folder = sock_path.parent
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)                       # only this user (and root) can reach the socket
    # launchctl asuser: macOS's password dialog starts programs as administrator but outside the
    # user's login session, and there macOS wouldn't hand the USB device over ("out of resources",
    # 2026-10-25); `sudo` in a terminal stays inside it and works. asuser moves the helper back
    # into this user's session, keeping the administrator rights.
    command = " ".join(shlex.quote(a) for a in [
        "/bin/launchctl", "asuser", str(os.getuid()),
        sys.executable, "-m", "padsquire", "uboot", "helper", "--socket", str(sock_path),
        "--owner", f"{os.getuid()}:{os.getgid()}"])
    log = sock_path.parent / LOG.name
    # LIBUSB_DEBUG (0-4) from the app's environment goes on to the helper, whose log gets it.
    level = os.environ.get("LIBUSB_DEBUG", "")
    if level.isdigit():
        command = f"LIBUSB_DEBUG={level} {command}"
    shell = f"{command} > {shlex.quote(str(log))} 2>&1 &"
    script = f"do shell script {_applescript(shell)} with prompt {_applescript(PROMPT)} with administrator privileges"
    app = _applet(script, folder)
    if app is not None:                           # the dialog then says "PadSquire wants to make changes"
        done = subprocess.run([str(app / "Contents" / "MacOS" / "applet")], capture_output=True, text=True,
                              timeout=timeout)
        if not running(sock_path):
            _wait_for(sock_path, 15)
        if running(sock_path):
            return
        raise HelperError("the password dialog was cancelled, or the helper didn't start; nothing was changed")
    done = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        if "-128" in done.stderr or "canceled" in done.stderr.lower():
            raise HelperError("the password dialog was cancelled, so nothing was changed")
        raise HelperError(f"the helper didn't start: {done.stderr.strip() or 'no reason given'}")
    if not _wait_for(sock_path, 15):
        raise HelperError(f"the helper started but doesn't answer; its log is {log}")


def _wait_for(sock_path: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if running(sock_path):
            return True
        time.sleep(0.2)
    return False


def _applet(script: str, folder: Path) -> Path | None:
    """A tiny AppleScript app named PadSquire that asks for the password, so macOS's dialog names
    PadSquire instead of osascript. Built with macOS's own osacompile, once per script (it holds
    the helper's command), in the helper's folder; None if the tools are missing."""
    osacompile, codesign = shutil.which("osacompile"), shutil.which("codesign")
    if not (osacompile and codesign):
        return None
    app = folder / "PadSquire.app"
    stamp = app / "Contents" / "Resources" / "padsquire.applescript"
    try:
        if stamp.read_text() == script:
            return app
    except OSError:
        pass
    shutil.rmtree(app, ignore_errors=True)
    if subprocess.run([osacompile, "-o", str(app), "-e", script], capture_output=True).returncode != 0:
        return None
    info_path = app / "Contents" / "Info.plist"
    with open(info_path, "rb") as f:
        info = plistlib.load(f)
    info.update({"CFBundleIdentifier": "com.padsquire.PadSquire.admin", "CFBundleDisplayName": "PadSquire",
                 "LSUIElement": True, "NSSystemAdministrationUsageDescription": PROMPT})
    with open(info_path, "wb") as f:
        plistlib.dump(info, f)
    from . import desktop
    desktop._mac_icon(app / "Contents" / "Resources" / "applet.icns")
    stamp.write_text(script)
    # editing the bundle broke osacompile's ad-hoc signature: sign it again, ad hoc
    if subprocess.run([codesign, "--force", "--sign", "-", str(app)], capture_output=True).returncode != 0:
        shutil.rmtree(app, ignore_errors=True)
        return None
    return app


def stop(sock_path: Path = SOCKET) -> None:
    try:
        with _connect(sock_path, timeout=5) as s:
            _call(s, {"op": "quit"})
    except (OSError, HelperError):
        pass


class HelperDevice:
    """execute(cdb, data_out, data_in_len) -> data_in through the helper, like SgDevice.
    close() keeps the helper and its hold on the device: the next step uses them again."""

    def __init__(self, sock_path: Path = SOCKET, wait: float = WAIT_FOR_DEVICE):
        try:
            self.sock = _connect(sock_path, timeout=wait + 5)
        except OSError as e:
            raise jlboot.JLBootError(f"the update-mode helper isn't running ({e.strerror})") from None
        reply = _call(self.sock, {"op": "ready", "wait": wait})
        if not reply.get("ok"):
            self.sock.close()
            raise jlboot.JLBootError(reply.get("error") or "the update-mode helper has no device")
        self.sock.settimeout(60)

    def execute(self, cdb: bytes, data_out: bytes | None = None, data_in_len: int = 0) -> bytes:
        if not allowed(cdb):                       # the helper checks too
            raise jlboot.JLBootError(f"command {cdb[:2].hex()} is not allowed")
        reply = _call(self.sock, {"op": "exec", "cdb": cdb.hex(), "out": data_out.hex() if data_out else None,
                                  "n": data_in_len})
        if not reply.get("ok"):
            raise jlboot.JLBootError(reply.get("error") or "the helper refused the command")
        return bytes.fromhex(reply.get("data", ""))

    def close(self) -> None:
        self.sock.close()


def _applescript(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _connect(sock_path: Path, timeout: float) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock_path))
    except OSError:
        s.close()
        raise
    return s


def _call(s: socket.socket, message: dict) -> dict:
    try:
        s.sendall(json.dumps(message).encode() + b"\n")
        line = _readline(s)
    except OSError as e:
        raise HelperError(f"lost the update-mode helper: {e}") from e
    if not line:
        raise HelperError("the update-mode helper went away")
    return json.loads(line)


def _readline(s: socket.socket) -> bytes:
    buf = bytearray()
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


# -- the helper's side (as root) -------------------------------------------------------------------

def serve(sock_path: Path, owner: tuple[int, int], log=print) -> int:
    """Run as root: take the update-mode disk once it appears, then answer the app until the
    controller restarts, the app says quit, or nothing happens for IDLE seconds."""
    folder = sock_path.parent
    info = folder.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != owner[0] or stat.S_IMODE(info.st_mode) & 0o077):
        log(f"refusing: {folder} must be a folder owned by uid {owner[0]} that only it can open")
        return 2
    if sock_path.exists() or sock_path.is_symlink():
        sock_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    os.chown(sock_path, *owner)
    os.chmod(sock_path, 0o600)
    server.listen(2)
    log(f"listening on {sock_path}")
    device = None
    started = last = last_io = time.monotonic()
    try:
        while True:
            server.settimeout(1)
            try:
                conn, _ = server.accept()
            except socket.timeout:
                # Left alone, the chip leaves update mode by itself after a short while (seen on
                # macOS, 2026-10-25). An INQUIRY now and then, which only reads its name, may
                # keep it there between the app's steps.
                if device is not None and time.monotonic() - last_io > KEEPALIVE:
                    try:
                        device.execute(INQUIRY_CDB, None, 36)
                        last_io = time.monotonic()
                    except jlboot.JLBootError as e:
                        log(f"keepalive failed ({e}); the controller seems gone, quitting")
                        return 0
                if time.monotonic() - last > IDLE:
                    log("idle; quitting")
                    return 0
                if device is not None and not jlboot.find():
                    log("the controller left update mode; quitting")
                    return 0
                if device is None and time.monotonic() - started > WAIT_FOR_DEVICE + 60:
                    log("no update-mode device appeared; quitting")
                    return 1
                continue
            with conn:
                conn.settimeout(None)
                while True:
                    line = _readline(conn)
                    if not line:
                        break
                    last = time.monotonic()
                    message = json.loads(line)
                    op = message.get("op")
                    if op == "ping":
                        reply = {"ok": True, "device": device is not None}
                    elif op == "quit":
                        conn.sendall(b'{"ok": true}\n')
                        log("asked to quit")
                        return 0
                    elif op == "ready":
                        error = None
                        if device is None:
                            device, error = _take_device(float(message.get("wait", WAIT_FOR_DEVICE)), log)
                        reply = {"ok": device is not None, "error": error}
                    elif op == "exec":
                        reply, gone = _exec(device, message)
                        last_io = time.monotonic()
                        if gone:
                            conn.sendall(json.dumps(reply).encode() + b"\n")
                            log("the device is gone (restarted or unplugged); quitting")
                            return 0
                    else:
                        reply = {"ok": False, "error": f"unknown request {op!r}"}
                    conn.sendall(json.dumps(reply).encode() + b"\n")
    finally:
        if device is not None:
            device.close()
        server.close()
        try:
            sock_path.unlink()
        except OSError:
            pass


def _take_device(wait: float, log):
    """Take the update-mode disk once it appears. Right after it appears macOS is still attaching
    its disk driver, and taking it then fails ("Other error", seen 2026-10-25), so keep trying."""
    deadline = time.monotonic() + wait
    error, seen = None, None
    while time.monotonic() < deadline:
        found = jlboot.find()
        if found and seen is None:
            seen = time.monotonic()
        # Taking it in the first second left it stuck ("out of resources" for every libusb
        # program until replugged, 2026-10-25): let macOS finish attaching its driver first.
        if found and time.monotonic() - seen >= SETTLE:
            try:
                device = jlboot.open_device(found[0].path)
            except jlboot.JLBootError as e:
                if error is None:
                    log(f"{found[0].path} not ready yet ({e}); trying again")
                error = e
            else:
                log(f"took {found[0].path} ({found[0].vendor} {found[0].product})")
                return device, None
        time.sleep(0.5)
    if error is not None:
        log(f"gave up: {error}")
        return None, (f"the helper couldn't take the controller's update-mode disk from macOS ({error}). "
                      "Unplug the controller, plug it in again, and try once more.")
    return None, "no controller in update mode appeared"


def _exec(device, message: dict) -> tuple[dict, bool]:
    if device is None:
        return {"ok": False, "error": "no device yet"}, False
    cdb = bytes.fromhex(message.get("cdb", ""))
    if not allowed(cdb):
        return {"ok": False, "error": f"command {cdb[:2].hex()} is not allowed"}, False
    out = bytes.fromhex(message["out"]) if message.get("out") else None
    try:
        data = device.execute(cdb, out, int(message.get("n", 0)))
    except jlboot.JLBootError as e:
        gone = not jlboot.find()
        return {"ok": False, "error": str(e)}, gone
    return {"ok": True, "data": data.hex()}, False


def owner_arg(text: str) -> tuple[int, int]:
    uid, _, gid = text.partition(":")
    return int(uid), int(gid)


def main_serve(sock_path: Path, owner: tuple[int, int]) -> int:
    def log(text: str) -> None:
        print(time.strftime("%H:%M:%S"), text, flush=True)
    if os.geteuid() != 0:
        log("the helper needs administrator rights")
        return 2
    try:
        return serve(sock_path, owner, log)
    except Exception as e:                        # the log is all there is to read afterwards
        log(f"stopped: {e!r}")
        return 1
    finally:
        try:                                      # the log goes back to the user, readable by the app
            os.chown(sock_path.parent / LOG.name, *owner)
        except OSError:
            pass
