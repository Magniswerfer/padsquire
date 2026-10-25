# SPDX-License-Identifier: AGPL-3.0-or-later
"""USB HID transport for the GCM protocol: the cable (and, presumably, the 2.4 GHz dongle).

In PC/Xbox mode (green LED) the controller enumerates as 3537:100F with a vendor HID interface
on usage page 0xFF7A: 64-byte input and output reports, no report IDs. GameSir Connect's DLL
(sapjoyuniapis_t4n2) talks to it like this, and so do we:
  - drop any input reports already queued
  - write the command, zero-padded to one 64-byte report
  - read reports for up to 11 x 200 ms, skipping ones that start with 0xF4

Replies come in two shapes, both confirmed on hardware: a GCM frame with its own length byte
and checksum (host info), or the request echoed back with the value filled in and a checksum
over the whole 64-byte report in its last byte (dead zones). `_trim` turns the first shape into
the bare frame, so gcm.verify() checks the right byte in both cases.

On Linux the interface is a hidraw node found through sysfs. On macOS the system's own HID driver
serves it, and we reach it through hidapi (IOHIDManager underneath): no driver, no sudo.
"""

from __future__ import annotations

import asyncio
import logging
import os
import select
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import gcm

VENDOR_ID = 0x3537
# Known config identities. 0x2106 (the dongle with no controller linked) has the same vendor
# interface, but we don't know its protocol, so it is deliberately left out.
CONFIG_PIDS = {0x100F: "cable", 0x1098: "2.4 GHz dongle"}
DESCRIPTOR_PREFIX = bytes.fromhex("067aff")   # Usage Page (0xFF7A)

log = logging.getLogger("padsquire.hid")   # PADSQUIRE_DEBUG=1: every frame, sent and received

REPORT_SIZE = 64
READ_TIMEOUT = 0.2
READ_ATTEMPTS = 11
STATE_REPORT = 0xF4
USAGE_PAGE = 0xFF7A
MACOS = sys.platform == "darwin"


class HidError(RuntimeError):
    pass


@dataclass
class HidDevice:
    path: str          # /dev/hidrawN, or hidapi's path on macOS
    pid: int
    name: str

    @property
    def link(self) -> str:
        return CONFIG_PIDS.get(self.pid, f"PID {self.pid:#06x}")


def find(sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev")) -> list[HidDevice]:
    """Every Nova 2 Lite config interface currently plugged in."""
    if MACOS:
        return _find_hidapi()
    base = sys_root / "class" / "hidraw"
    found = []
    if not base.exists():
        return found
    for node in sorted(base.iterdir()):
        try:
            uevent = (node / "device" / "uevent").read_text()
            descriptor = (node / "device" / "report_descriptor").read_bytes()
        except OSError:
            continue
        fields = dict(line.split("=", 1) for line in uevent.splitlines() if "=" in line)
        try:
            _bus, vid, pid = (int(x, 16) for x in fields.get("HID_ID", "").split(":"))
        except ValueError:
            continue
        if vid == VENDOR_ID and pid in CONFIG_PIDS and descriptor.startswith(DESCRIPTOR_PREFIX):
            found.append(HidDevice(str(dev_root / node.name), pid, fields.get("HID_NAME", "")))
    return found


def _hidapi():
    try:
        import hid as hidapi
    except ImportError:
        raise HidError("USB on macOS needs the hidapi package: pip install hidapi") from None
    return hidapi


def _find_hidapi(lister=None) -> list[HidDevice]:
    if lister is None:
        try:
            lister = _hidapi().enumerate
        except HidError:
            return []
    found = {}
    for d in lister(VENDOR_ID, 0):
        if d["product_id"] in CONFIG_PIDS and d["usage_page"] == USAGE_PAGE:
            path = d["path"].decode() if isinstance(d["path"], bytes) else d["path"]
            name = " ".join(x for x in (d.get("manufacturer_string"), d.get("product_string")) if x)
            found.setdefault(path, HidDevice(path, d["product_id"], name))
    return sorted(found.values(), key=lambda d: d.path)


def _trim(reply: bytes) -> bytes:
    """Cut a padded report down to the GCM frame inside it, where one can be found."""
    n = reply[2] if len(reply) > 2 else 0
    if 3 <= n < len(reply) and reply[n - 1] == gcm.checksum(reply[:n]):
        return reply[:n]                     # framed reply with a length byte
    if reply[-1:] == bytes([gcm.checksum(reply)]):
        return reply                         # checksum over the whole report
    # Commands without a length byte (key mapping 0x1F): the shortest zero-padded frame
    # that ends in a valid checksum.
    end = len(reply.rstrip(b"\x00"))
    for n in range(max(end, 3), len(reply)):
        if reply[n - 1] == gcm.checksum(reply[:n]):
            return reply[:n]
    return reply


class HidTransport:
    """GCM over a hidraw file descriptor (or anything that behaves like one)."""

    def __init__(self, fd: int, timeout: float = READ_TIMEOUT, attempts: int = READ_ATTEMPTS):
        self.fd = fd
        self.timeout = timeout
        self.attempts = attempts
        self._lock = asyncio.Lock()

    def _read(self, timeout: float) -> bytes | None:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        return os.read(self.fd, REPORT_SIZE) if ready else None

    def _write(self, report: bytes) -> None:
        os.write(self.fd, report)

    def _exchange(self, frame: bytes, accept: Callable[[bytes], bool]) -> bytes:
        if len(frame) > REPORT_SIZE:
            raise HidError(f"command longer than one report ({len(frame)} bytes)")
        while self._read(0.001) is not None:
            pass
        # Report ID 0 first: the kernel strips it, since the interface has no report IDs.
        log.debug("send %s", frame.hex(" "))
        self._write(b"\x00" + frame + bytes(REPORT_SIZE - len(frame)))
        last = b""
        for _ in range(self.attempts):
            report = self._read(self.timeout)
            if report is None or not report or report[0] == STATE_REPORT:
                continue
            last = _trim(report)
            if accept(last):
                log.debug("recv %s", last.hex(" "))
                return last
        raise HidError(f"no valid reply to {frame.hex(' ')} (last report: {last.hex(' ') or 'nothing'})")

    async def request(self, frame: bytes, accept: Callable[[bytes], bool] | None = None) -> bytes:
        async with self._lock:
            return await asyncio.to_thread(self._exchange, frame, accept or (lambda _r: True))

    def _state_report(self) -> bytes:
        echo = lambda r: r[:2] == bytes([gcm.CMD_STATE_REPORT, 0x01])
        self._exchange(gcm.state_report_set(gcm.StateReport.MAPPED), echo)
        try:
            for _ in range(self.attempts):
                report = self._read(self.timeout)
                if report and report[0] == STATE_REPORT and len(report) > 2 and report[1] == 0:
                    return report
            raise HidError("no state report arrived")
        finally:
            self._exchange(gcm.state_report_set(gcm.StateReport.OFF), echo)

    def _send(self, frame: bytes) -> None:
        log.debug("send %s", frame.hex(" "))
        self._write(b"\x00" + frame + bytes(REPORT_SIZE - len(frame)))

    async def send(self, frame: bytes) -> None:
        """One report that gets no reply (the vibration test)."""
        async with self._lock:
            await asyncio.to_thread(self._send, frame)

    async def state_report(self) -> bytes:
        """One 0xF4 state report: switched on for the moment it takes, then off again."""
        async with self._lock:
            return await asyncio.to_thread(self._state_report)


class HidapiTransport(HidTransport):
    """The same over a hidapi device (macOS). hidapi drops the leading report ID 0, as the
    kernel does."""

    def __init__(self, device, timeout: float = READ_TIMEOUT, attempts: int = READ_ATTEMPTS):
        super().__init__(-1, timeout, attempts)
        self.device = device

    def _read(self, timeout: float) -> bytes | None:
        report = self.device.read(REPORT_SIZE, max(1, round(timeout * 1000)))
        return bytes(report) if report else None

    def _write(self, report: bytes) -> None:
        if self.device.write(report) < 0:
            raise HidError(f"could not write to the controller: {self.device.error()}")


def _open_hidapi(path: str):
    device = _hidapi().device()
    try:
        device.open_path(path.encode())
    except OSError:
        # macOS opens HID devices exclusively: one program at a time.
        raise HidError("could not open the controller. Close other programs using it (another "
                       "PadSquire window, GameSir software), or unplug it and plug it in again") from None
    return device


# GameSir's UpdateFirmware for PID 0x100F (sapjoyuniapis_t4n2, SAPJoyUniDevice_UpdateFirmware):
# one zero-padded report, no reply awaited, then the handle is closed and the controller drops off
# USB to come back as JieLi's USB download disk. Nothing is erased or written by this command.
UPDATE_MODE = bytes.fromhex("f20004f6")


def send_update_mode(fd: int) -> None:
    os.write(fd, b"\x00" + UPDATE_MODE + bytes(REPORT_SIZE - len(UPDATE_MODE)))


def enter_update_mode(path: str) -> None:
    """Switch the controller on `path` into its USB download (firmware update) mode."""
    if MACOS:
        # Nothing here can talk to the update-mode disk yet (no SG_IO): don't strand the controller.
        raise HidError("update mode isn't available on macOS yet; use a Linux computer for this")
    try:
        fd = os.open(path, os.O_RDWR)
    except PermissionError:
        raise HidError(f"no permission to open {path}. Install the udev rules (the "
                       "settings app's Setup and help page explains how), then replug the controller") from None
    except OSError as e:
        raise HidError(f"could not open {path}: {e.strerror}") from e
    try:
        send_update_mode(fd)
    except OSError as e:
        raise HidError(f"could not write to {path}: {e.strerror}") from e
    finally:
        os.close(fd)


class connect:
    """async with connect("/dev/hidraw11") as transport: ..."""

    def __init__(self, path: str):
        self.path = path

    async def __aenter__(self) -> HidTransport:
        if MACOS:
            self._device = _open_hidapi(self.path)
            return HidapiTransport(self._device)
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise HidError(f"no permission to open {self.path}. Install the udev rules (the "
                           "settings app's Setup and help page explains how), then replug the controller") from None
        except OSError as e:
            raise HidError(f"could not open {self.path}: {e.strerror}") from e
        return HidTransport(self._fd)

    async def __aexit__(self, *exc) -> None:
        if MACOS:
            self._device.close()
        else:
            os.close(self._fd)
