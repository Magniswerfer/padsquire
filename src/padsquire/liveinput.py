# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live stick, trigger and button state, from the gamepad device Linux creates for the controller.

Over the cable in PC/Xbox mode the kernel's xpad driver turns the controller into an evdev
node ("GameSir Nova 2 Lite"). Its values come after the controller's own dead zones, which is
what the settings app wants to show. No extra dependency: axis ranges come from EVIOCGABS and
events are plain `struct input_event`s.

macOS has no xpad (and Steam may own the Xbox interface there), so it reads the controller's own
0xF4 state reports on the settings channel instead: `state_from_report`.
"""

from __future__ import annotations

import fcntl
import os
import struct
from pathlib import Path

EV_KEY = 0x01
EV_ABS = 0x03
# xpad: ABS_X/ABS_Y left stick, ABS_RX/ABS_RY right stick, ABS_Z / ABS_RZ triggers.
AXES = {0x00: "lx", 0x01: "ly", 0x03: "rx", 0x04: "ry", 0x02: "lt", 0x05: "rt"}
STICKS = {"lx", "ly", "rx", "ry"}
# xpad's buttons, named like gcm.Key. L4, R4 and Capture are not reported in this mode.
BUTTONS = {0x130: "A", 0x131: "B", 0x133: "X", 0x134: "Y", 0x136: "LB", 0x137: "RB",
           0x13A: "SELECT", 0x13B: "START", 0x13C: "HOME", 0x13D: "L3", 0x13E: "R3"}
HATS = {0x10: ("LEFT", "RIGHT"), 0x11: ("UP", "DOWN")}   # the D-pad, as ABS_HAT0X / ABS_HAT0Y
TRIGGER_PRESSED = 0.5
KEY_BYTES = 96                                             # (KEY_MAX + 1) / 8

EVENT = struct.Struct("llHHi")          # struct input_event (timeval, type, code, value)
ABSINFO = struct.Struct("6i")           # value, minimum, maximum, fuzz, flat, resolution


# 0xF4 state reports, "mapped" mode (after remapping, like xpad sees it). Confirmed on firmware 8201
# by cable, 2026-10-25: (byte, bit) -> button; sticks are int16 little-endian at 8 (LX), 10 (LY),
# 12 (RX), 14 (RY) with up positive; triggers 0-255 at 16 and 17; byte 22 = sum of bytes 0-21.
REPORT_BUTTONS = {(4, 0x01): "A", (4, 0x02): "B", (4, 0x08): "X", (4, 0x10): "Y",
                  (4, 0x40): "LB", (4, 0x80): "RB", (5, 0x04): "SELECT", (5, 0x08): "START",
                  (5, 0x10): "HOME", (5, 0x20): "L3", (5, 0x40): "R3", (5, 0x80): "CAPTURE",
                  (7, 0x40): "M",                     # the M button: no gcm.Key, never remappable
                  (6, 0x01): "UP", (6, 0x02): "DOWN", (6, 0x04): "LEFT", (6, 0x08): "RIGHT"}
REPORT_STICKS = {"lx": (8, 1), "ly": (10, -1), "rx": (12, 1), "ry": (14, -1)}   # evdev: up is negative


def state_from_report(report: bytes) -> dict | None:
    """The same shape as LiveInput.state(), from one 0xF4 state report; None if it isn't one."""
    if len(report) < 23 or report[0] != 0xF4 or sum(report[:22]) & 0xFF != report[22]:
        return None
    out: dict = {}
    for name, (at, sign) in REPORT_STICKS.items():
        value = int.from_bytes(report[at:at + 2], "little", signed=True) / 32767
        out[name] = round(max(-1.0, min(1.0, sign * value)), 3) + 0.0                    # no -0.0
    out["lt"], out["rt"] = round(report[16] / 255, 3), round(report[17] / 255, 3)
    pressed = {name for (byte, bit), name in REPORT_BUTTONS.items() if report[byte] & bit}
    for trigger in ("lt", "rt"):
        if out[trigger] >= TRIGGER_PRESSED:
            pressed.add(trigger.upper())
    out["pressed"] = sorted(pressed)
    return out


class ReportInput:
    """LiveInput's pump() and state(), from the controller's 0xF4 state reports (macOS, where there is
    no gamepad device to read): for the stick check. pump() is a coroutine here; start() switches the
    reports on for this reader and aclose() off again (HidTransport.live_state counts viewers)."""

    REST = {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0, "pressed": []}

    def __init__(self, transport):
        self.transport = transport
        self.st: dict | None = None

    async def start(self) -> None:
        await self.transport.live_state(on=True)

    async def pump(self) -> bool:
        st = state_from_report(await self.transport.live_state() or b"")
        if st is not None:
            self.st = st
        return st is not None

    def state(self) -> dict:
        return dict(self.st or self.REST)

    async def aclose(self) -> None:
        await self.transport.live_state(on=False)


class Unavailable(RuntimeError):
    pass


def _eviocgabs(axis: int) -> int:
    return (2 << 30) | (ABSINFO.size << 16) | (ord("E") << 8) | (0x40 + axis)


def _eviocgkey() -> int:
    return (2 << 30) | (KEY_BYTES << 16) | (ord("E") << 8) | 0x18


def _usb_device(path: Path) -> Path | None:
    """The USB device directory (the one with idVendor) that a sysfs node hangs off."""
    for parent in [path, *path.parents]:
        if (parent / "idVendor").exists():
            return parent
    return None


def _has_triggers(event: Path) -> bool:
    try:
        words = (event / "device" / "capabilities" / "abs").read_text().split()
    except OSError:
        return False
    bits = int(words[-1], 16) if words else 0
    return all(bits >> axis & 1 for axis in AXES)


def event_node_for(hidraw: str, sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev")) -> str:
    """The evdev node of the gamepad on the same USB device as `hidraw` (/dev/hidrawN)."""
    node = sys_root / "class" / "hidraw" / Path(hidraw).name / "device"
    usb = _usb_device(node.resolve()) if node.exists() else None
    if usb is None:
        raise Unavailable("the controller is no longer connected")
    for event in sorted((sys_root / "class" / "input").glob("event*")):
        device = (event / "device").resolve()
        if usb in device.parents and _has_triggers(event):
            return str(dev_root / "input" / event.name)
    raise Unavailable("no gamepad device for this controller (is it in PC mode, LED green?)")


class LiveInput:
    """Positions as fractions: sticks -1..1 (up is negative), triggers 0..1; buttons held."""

    def __init__(self, path: str):
        try:
            self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            raise Unavailable(f"could not read {path}: {e.strerror}") from e
        self.range: dict[str, tuple[int, int]] = {}
        self.raw: dict[str, int] = {}
        for axis, name in AXES.items():
            buf = fcntl.ioctl(self.fd, _eviocgabs(axis), bytes(ABSINFO.size))
            value, lo, hi, *_ = ABSINFO.unpack(buf)
            self.range[name] = (lo, hi)
            self.raw[name] = value
        self.hats = {}
        for axis in HATS:
            try:
                self.hats[axis] = ABSINFO.unpack(fcntl.ioctl(self.fd, _eviocgabs(axis), bytes(ABSINFO.size)))[0]
            except OSError:
                self.hats[axis] = 0
        try:
            bits = int.from_bytes(fcntl.ioctl(self.fd, _eviocgkey(), bytes(KEY_BYTES)), "little")
        except OSError:
            bits = 0
        self.held = {name for code, name in BUTTONS.items() if bits >> code & 1}

    def pump(self) -> bool:
        """Read every queued event. Raises Unavailable once the device is gone."""
        changed = False
        while True:
            try:
                data = os.read(self.fd, EVENT.size * 64)
            except BlockingIOError:
                return changed
            except OSError as e:
                raise Unavailable("the controller was disconnected") from e
            if not data:
                return changed
            for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
                _s, _us, kind, code, value = EVENT.unpack_from(data, offset)
                if kind == EV_ABS and code in AXES:
                    self.raw[AXES[code]] = value
                    changed = True
                elif kind == EV_ABS and code in HATS:
                    self.hats[code] = value
                    changed = True
                elif kind == EV_KEY and code in BUTTONS:
                    (self.held.add if value else self.held.discard)(BUTTONS[code])
                    changed = True

    def state(self) -> dict[str, float]:
        out = {}
        for name, value in self.raw.items():
            lo, hi = self.range[name]
            span = (hi - lo) or 1
            if name in STICKS:
                out[name] = round(max(-1.0, min(1.0, (2 * (value - lo) / span) - 1)), 3) + 0.0   # no -0.0
            else:
                out[name] = round(max(0.0, min(1.0, (value - lo) / span)), 3)
        pressed = set(self.held)
        for axis, (neg, pos) in HATS.items():
            if self.hats[axis] < 0:
                pressed.add(neg)
            elif self.hats[axis] > 0:
                pressed.add(pos)
        for trigger in ("lt", "rt"):
            if out[trigger] >= TRIGGER_PRESSED:
                pressed.add(trigger.upper())
        out["pressed"] = sorted(pressed)
        return out

    def close(self) -> None:
        os.close(self.fd)
