# SPDX-License-Identifier: AGPL-3.0-or-later
"""USB transport for the Cyclone 2 (`c2.py`): its vendor hidraw interface, read-only for now.

Requests are 64-byte output reports starting with report ID 0x0F; replies and the 0x12 state
reports come back on the same node. Like Connect, a request that gets no reply within 200 ms is
sent again (Connect gives up after 10 sends), and a "busy" acknowledgement means try again.

Only the requests in `c2.READ_ONLY` leave this module: no settings are written and the
controller is never put into update mode, until the protocol is confirmed on hardware.
"""

from __future__ import annotations

import asyncio
import logging
import os
import select
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import c2

log = logging.getLogger("padsquire.c2")    # PADSQUIRE_DEBUG=1: every report, sent and received

RESEND_AFTER = 0.2
SENDS = 10


class C2UsbError(RuntimeError):
    pass


@dataclass
class C2Device:
    path: str
    pid: int
    name: str

    @property
    def link(self) -> str:
        return c2.PIDS.get(self.pid, f"PID {self.pid:#06x}")


def find(sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev")) -> list[C2Device]:
    """Every Cyclone 2 configuration interface (usage page 0xFFF0) plugged in."""
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
        if vid == c2.VENDOR_ID and pid in c2.PIDS and descriptor.startswith(c2.DESCRIPTOR_PREFIX):
            found.append(C2Device(str(dev_root / node.name), pid, fields.get("HID_NAME", "")))
    return found


class C2Transport:
    """Request and reply over a report reader and writer: a hidraw node, or a simulator."""

    def __init__(self, read: Callable[[float], bytes | None], write: Callable[[bytes], object],
                 close: Callable[[], None] = lambda: None):
        self._read, self._write, self.close = read, write, close
        self.last_state: bytes | None = None

    @classmethod
    def open(cls, path: str) -> C2Transport:
        fd = os.open(path, os.O_RDWR)

        def read(timeout: float) -> bytes | None:
            ready, _, _ = select.select([fd], [], [], timeout)
            return os.read(fd, c2.REPORT_SIZE) if ready else None

        return cls(read, lambda report: os.write(fd, report), lambda: os.close(fd))

    def send(self, frame: bytes) -> None:
        if len(frame) < 2 or frame[0] != c2.OUT_REPORT or frame[1] not in c2.READ_ONLY:
            raise C2UsbError(f"refusing to send {frame[:6].hex(' ')}: not one of the read-only requests")
        log.debug("send %s", frame.hex(" "))
        self._write(c2.pad(frame))

    def _next(self, timeout: float) -> bytes | None:
        report = self._read(timeout)
        if report and report[0] == c2.STATE_REPORT:
            self.last_state = bytes(report)
        elif report:
            log.debug("recv %s", bytes(report).rstrip(b"\0").hex(" "))
        return bytes(report) if report else None

    def request(self, frame: bytes, accept: Callable[[bytes], bool]) -> bytes:
        self.drain()
        for _ in range(SENDS):
            self.send(frame)
            deadline = time.monotonic() + RESEND_AFTER
            while (left := deadline - time.monotonic()) > 0:
                report = self._next(left)
                if report is None or report[0] == c2.STATE_REPORT:
                    continue
                if c2.is_busy(report):
                    break
                if accept(report):
                    return report
        raise C2UsbError(f"no reply to {frame.hex(' ')}")

    def drain(self) -> None:
        """Read what is queued (mostly 0x12 state reports, about a thousand a second)."""
        for _ in range(256):
            if self._next(0) is None:
                return

    def state(self, timeout: float = 1.0) -> bytes | None:
        """The newest 0x12 state report, waiting up to `timeout` for one."""
        self.drain()
        deadline = time.monotonic() + timeout
        while self.last_state is None and (left := deadline - time.monotonic()) > 0:
            self._next(left)
        return self.last_state

    # -- reads ------------------------------------------------------------------------

    def heartbeat(self) -> None:
        self.send(c2.heartbeat())

    def firmware_version(self) -> c2.FirmwareVersion:
        reply = self.request(c2.read_firmware_version(),
                             lambda r: c2.reply_type(r) == c2.Cmd.READ_FIRMWARE_VERSION_ACK)
        return c2.parse_firmware_version(reply)

    def current_profile(self) -> int:
        reply = self.request(c2.read_current_profile(),
                             lambda r: c2.reply_type(r) == c2.Cmd.READ_CURRENT_PROFILE_ACK)
        return c2.parse_current_profile(reply)

    def profile(self, number: int) -> bytes:
        reader = c2.ProfileReader(number)
        for frame in c2.read_profile(number):
            want = frame[2:6]
            self.request(frame, lambda r, w=want: c2.reply_type(r) == c2.Cmd.READ_PROFILE_ACK
                         and r[2:6] == w and reader.add(r))
        if not reader.complete:
            raise C2UsbError(f"profile {number} came back incomplete")
        return bytes(reader.data)


def read_everything(t: C2Transport) -> dict:
    """What `padsquire c2 dump` saves: versions, the state report and every profile, raw and
    decoded. Only reads."""
    t.heartbeat()
    out: dict = {"firmware": vars(t.firmware_version()), "current_profile": t.current_profile()}
    state = t.state()
    out["state_report"] = state.hex() if state else None
    if state:
        s = c2.parse_state(state)
        out["state"] = {"battery": s.battery, "charging": s.charging, "lighting_effect": s.lighting_effect,
                        "colours": s.colours}
    out["profiles"] = {}
    for number in c2.PROFILES:
        raw = t.profile(number)
        decoded = c2.decode_light(raw) if number == c2.LIGHT_PROFILE else c2.decode_profile(raw)
        out["profiles"][str(number)] = {"raw": raw.hex(), "decoded": decoded}
    return out


class C2Client:
    """The settings app's handle on a Cyclone 2: the transport's reads, run in a thread, and the
    profile images as last read (writes will be checked against them)."""

    family = "c2"

    def __init__(self, transport: C2Transport):
        self.t = transport
        self.images: dict[int, bytes] = {}
        self._awake = False

    async def _run(self, fn, *args):
        if not self._awake:
            await asyncio.to_thread(self.t.heartbeat)
            self._awake = True
        return await asyncio.to_thread(fn, *args)

    async def firmware(self) -> c2.FirmwareVersion:
        return await self._run(self.t.firmware_version)

    async def current_profile(self) -> int:
        return await self._run(self.t.current_profile)

    async def battery(self) -> dict:
        report = await self._run(self.t.state)
        if report is None:
            raise C2UsbError("the controller sent no state report")
        s = c2.parse_state(report)
        return {"level": s.battery, "charging": s.charging}

    async def profile(self, number: int) -> dict:
        raw = await self._run(self.t.profile, number)
        self.images[number] = raw
        return c2.decode_light(raw) if number == c2.LIGHT_PROFILE else c2.decode_profile(raw)

    def close(self) -> None:
        self.t.close()
