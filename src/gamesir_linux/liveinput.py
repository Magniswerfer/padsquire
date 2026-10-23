"""Live stick and trigger positions, from the gamepad device Linux creates for the controller.

Over the cable in PC/Xbox mode the kernel's xpad driver turns the controller into an evdev
node ("GameSir Nova 2 Lite"). Its values come after the controller's own dead zones, which is
what the settings app wants to show. No extra dependency: axis ranges come from EVIOCGABS and
events are plain `struct input_event`s.
"""

from __future__ import annotations

import fcntl
import os
import struct
from pathlib import Path

EV_ABS = 0x03
# xpad: ABS_X/ABS_Y left stick, ABS_RX/ABS_RY right stick, ABS_Z / ABS_RZ triggers.
AXES = {0x00: "lx", 0x01: "ly", 0x03: "rx", 0x04: "ry", 0x02: "lt", 0x05: "rt"}
STICKS = {"lx", "ly", "rx", "ry"}

EVENT = struct.Struct("llHHi")          # struct input_event (timeval, type, code, value)
ABSINFO = struct.Struct("6i")           # value, minimum, maximum, fuzz, flat, resolution


class Unavailable(RuntimeError):
    pass


def _eviocgabs(axis: int) -> int:
    return (2 << 30) | (ABSINFO.size << 16) | (ord("E") << 8) | (0x40 + axis)


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
    """Positions as fractions: sticks -1..1 (up is negative), triggers 0..1."""

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

    def state(self) -> dict[str, float]:
        out = {}
        for name, value in self.raw.items():
            lo, hi = self.range[name]
            span = (hi - lo) or 1
            if name in STICKS:
                out[name] = round(max(-1.0, min(1.0, (2 * (value - lo) / span) - 1)), 3) + 0.0   # no -0.0
            else:
                out[name] = round(max(0.0, min(1.0, (value - lo) / span)), 3)
        return out

    def close(self) -> None:
        os.close(self.fd)
