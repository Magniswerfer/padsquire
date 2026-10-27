# SPDX-License-Identifier: AGPL-3.0-or-later
"""USB transport for the Cyclone 2 (`c2.py`): its vendor hidraw interface, read-only for now.

Requests are 64-byte output reports starting with report ID 0x0F; replies and the 0x12 state
reports come back on the same node. Like Connect, a request that gets no reply within 200 ms is
sent again (Connect gives up after 10 sends), and a "busy" acknowledgement means try again.

By default only the requests in `c2.READ_ONLY` leave this module. A transport opened with
`writes=True` (the settings app) also sends profile switches, the vibration test and writes to
the settings in `c2.FIELDS`, each read back afterwards. Update mode is never sent.
"""

from __future__ import annotations

import asyncio
import logging
import os
import select
import time
from collections.abc import Callable
from dataclasses import dataclass, field
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
    # Every 0xFFF0 interface of the same USB device. By cable there are two, and only one
    # streams live state (Deadband); `open_device` picks it. The first is `path`.
    paths: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.paths:
            self.paths = [self.path]

    @property
    def link(self) -> str:
        return c2.PIDS.get(self.pid, f"PID {self.pid:#06x}")


def find(sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev")) -> list[C2Device]:
    """Every Cyclone 2 plugged in, with its configuration interfaces (usage page 0xFFF0)."""
    base = sys_root / "class" / "hidraw"
    found: dict[str, C2Device] = {}
    if not base.exists():
        return []
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
        name = fields.get("HID_NAME", "")
        if not (vid == c2.VENDOR_ID and pid in c2.PIDS and descriptor.startswith(c2.DESCRIPTOR_PREFIX)):
            continue
        if pid in c2.NAMED_PIDS and "cyclone" not in name.lower():
            continue                        # 0x0575 is also an idle G7 Pro 8K receiver ("Gamepad")
        try:
            usb = str((node / "device").resolve().parent.parent)    # .../1-2:1.1/0003:... -> 1-2
        except OSError:
            usb = node.name
        path = str(dev_root / node.name)
        if usb in found:
            found[usb].paths.append(path)
        else:
            found[usb] = C2Device(path, pid, name)
    return list(found.values())


def _live(report: bytes | None) -> bool:
    """A state report with something in it: sticks off zero, or a battery level."""
    return bool(report) and any(report[1:5]) or bool(report and len(report) > 36 and report[36])


def open_device(dev: C2Device, writes: bool = False, opener=None) -> C2Transport:
    """Open the interface that streams live state; failing that, the first that answers."""
    opener = opener or C2Transport.open
    fallback = None
    for path in dev.paths:
        t = opener(path, writes)
        try:
            t.heartbeat()
            if _live(t.state(timeout=0.5)):
                if fallback is not None:
                    fallback.close()
                return t
            if fallback is None:
                t.current_profile()
                fallback = t
                continue
        except (C2UsbError, OSError):
            pass
        t.close()
    if fallback is None:
        raise C2UsbError("the Cyclone 2 doesn't answer on any of its interfaces")
    return fallback


class C2Transport:
    """Request and reply over a report reader and writer: a hidraw node, or a simulator."""

    def __init__(self, read: Callable[[float], bytes | None], write: Callable[[bytes], object],
                 close: Callable[[], None] = lambda: None, writes: bool = False):
        self._read, self._write, self.close = read, write, close
        self.writes = writes
        self.last_state: bytes | None = None

    @classmethod
    def open(cls, path: str, writes: bool = False) -> C2Transport:
        fd = os.open(path, os.O_RDWR)

        def read(timeout: float) -> bytes | None:
            ready, _, _ = select.select([fd], [], [], timeout)
            return os.read(fd, c2.REPORT_SIZE) if ready else None

        return cls(read, lambda report: os.write(fd, report), lambda: os.close(fd), writes)

    def _allowed(self, frame: bytes) -> bool:
        if len(frame) < 2 or frame[0] != c2.OUT_REPORT:
            return False
        if frame[1] in c2.READ_ONLY:
            return True
        if not self.writes:
            return False
        if frame[1] == c2.Cmd.WRITE_PROFILE:
            return len(frame) >= 6 and len(frame) == 6 + frame[5] and c2.writable(frame[2], frame[3] << 8 | frame[4], frame[5])
        if frame[1] == c2.Cmd.SWITCH_PROFILE:
            return len(frame) == 3 and frame[2] in (1, 2, 3, 4)
        return frame[1] == c2.Cmd.VIBRATE and len(frame) == 6

    def send(self, frame: bytes) -> None:
        if not self._allowed(frame):
            raise C2UsbError(f"refusing to send {frame[:6].hex(' ')}: not a request PadSquire sends here")
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


    def read_range(self, profile: int, offset: int, length: int) -> bytes:
        frame = bytes([c2.OUT_REPORT, c2.Cmd.READ_PROFILE, profile, offset >> 8, offset & 0xFF, length])
        reply = self.request(frame, lambda r: c2.reply_type(r) == c2.Cmd.READ_PROFILE_ACK and r[2:6] == frame[2:6])
        return reply[6:6 + length]

    # -- changes (writes=True only) ----------------------------------------------------

    def switch_profile(self, number: int, tries: int = 3, wait: float = 2.5) -> None:
        """Make `number` the active profile and wait until the controller says it is."""
        for _ in range(tries):
            if self.current_profile() == number:
                time.sleep(SETTLE)
                return
            self.send(c2.switch_profile(number))
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                time.sleep(0.15)
                if self.current_profile() == number:
                    time.sleep(SETTLE)
                    return
        raise C2UsbError(f"the controller didn't switch to profile {number}")

    def write_profile(self, profile: int, offset: int, data: bytes) -> None:
        """Write into the active profile, then read it back: a write that didn't land raises."""
        if self.current_profile() != profile:
            raise C2UsbError(f"profile {profile} isn't the active one; switch to it first")
        for frame in c2.write_profile(profile, offset, data):
            self.send(frame)
            time.sleep(c2.WRITE_GAP)
        got = self.read_range(profile, offset, len(data))
        if got != data:
            raise C2UsbError(f"the controller kept {got.hex(' ')}, not {data.hex(' ')}")

    def rumble(self, left: int, right: int) -> None:
        self.send(c2.rumble(left, right))


SETTLE = 0.2     # after a switch, before the first profile command (Deadband)


def read_everything(t: C2Transport) -> dict:
    """What `padsquire c2 dump` saves: versions, the state report, and the active profile and the
    lighting profile, raw and decoded. Only reads."""
    t.heartbeat()
    out: dict = {"firmware": vars(t.firmware_version()), "current_profile": t.current_profile()}
    state = t.state()
    out["state_report"] = state.hex() if state else None
    if state:
        s = c2.parse_state(state)
        out["state"] = {"battery": s.battery, "charging": s.charging, "lighting_effect": s.lighting_effect,
                        "colours": s.colours}
    out["profiles"] = {}
    # A profile only answers while it is the active one (Deadband, on hardware), and switching
    # would change the controller: so the active profile and the lighting profile.
    for number in (out["current_profile"], c2.LIGHT_PROFILE):
        if number not in c2.PROFILES:
            continue
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

    async def switch(self, number: int) -> None:
        await self._run(self.t.switch_profile, number)

    async def set(self, name: str, value, shown: int | None = None) -> dict:
        """Write one setting (c2.FIELDS) into the active profile; returns that profile, read again.
        `shown`: the profile the page shows; if the controller has moved on, nothing is written."""
        offset, data = c2.field_bytes(name, value)
        number = await self.current_profile()
        if number not in (1, 2, 3, 4):
            raise C2UsbError("the controller is on its Shift layer; pick a profile first")
        if shown is not None and shown != number:
            raise C2UsbError(f"the controller switched to profile {number}; read the settings again first")
        await self._run(self.t.write_profile, number, offset, data)
        return await self.profile(number)

    async def rumble(self, left: int, right: int, seconds: float = 0.6) -> None:
        await self._run(self.t.rumble, left, right)
        await asyncio.sleep(seconds)
        await self._run(self.t.rumble, 0, 0)

    def close(self) -> None:
        self.t.close()
