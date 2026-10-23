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
"""

from __future__ import annotations

import asyncio
import os
import select
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import gcm

VENDOR_ID = 0x3537
# Known config identities. 0x2106 (the dongle with no controller linked) has the same vendor
# interface, but we don't know its protocol, so it is deliberately left out.
CONFIG_PIDS = {0x100F: "cable", 0x1098: "2.4 GHz dongle"}
DESCRIPTOR_PREFIX = bytes.fromhex("067aff")   # Usage Page (0xFF7A)

REPORT_SIZE = 64
READ_TIMEOUT = 0.2
READ_ATTEMPTS = 11
STATE_REPORT = 0xF4


class HidError(RuntimeError):
    pass


@dataclass
class HidDevice:
    path: str          # /dev/hidrawN
    pid: int
    name: str

    @property
    def link(self) -> str:
        return CONFIG_PIDS.get(self.pid, f"PID {self.pid:#06x}")


def find(sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev")) -> list[HidDevice]:
    """Every Nova 2 Lite config interface currently plugged in."""
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


def _trim(reply: bytes) -> bytes:
    n = reply[2] if len(reply) > 2 else 0
    if 3 <= n < len(reply) and reply[n - 1] == gcm.checksum(reply[:n]):
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

    def _exchange(self, frame: bytes, accept: Callable[[bytes], bool]) -> bytes:
        if len(frame) > REPORT_SIZE:
            raise HidError(f"command longer than one report ({len(frame)} bytes)")
        while self._read(0.001) is not None:
            pass
        # Report ID 0 first: the kernel strips it, since the interface has no report IDs.
        os.write(self.fd, b"\x00" + frame + bytes(REPORT_SIZE - len(frame)))
        last = b""
        for _ in range(self.attempts):
            report = self._read(self.timeout)
            if report is None or not report or report[0] == STATE_REPORT:
                continue
            last = _trim(report)
            if accept(last):
                return last
        raise HidError(f"no valid reply to {frame.hex(' ')} (last report: {last.hex(' ') or 'nothing'})")

    async def request(self, frame: bytes, accept: Callable[[bytes], bool] | None = None) -> bytes:
        async with self._lock:
            return await asyncio.to_thread(self._exchange, frame, accept or (lambda _r: True))


class connect:
    """async with connect("/dev/hidraw11") as transport: ..."""

    def __init__(self, path: str):
        self.path = path

    async def __aenter__(self) -> HidTransport:
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise HidError(f"no permission to open {self.path}. Install the udev rules in "
                           "contrib/udev/ (see HARDWARE.md), then replug the controller") from None
        except OSError as e:
            raise HidError(f"could not open {self.path}: {e.strerror}") from e
        return HidTransport(self._fd)

    async def __aexit__(self, *exc) -> None:
        os.close(self._fd)
