# SPDX-License-Identifier: AGPL-3.0-or-later
import os
from pathlib import Path

import pytest

from padsquire import liveinput


def _w(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n")


def fake_sysfs(root: Path) -> Path:
    """A controller on USB 1-3.3: hidraw11 on interface 2, the xpad gamepad (event15) and its
    keyboard (event26) on interfaces 0 and 1, plus another pad on a different port."""
    usb = root / "devices" / "pci0000:00" / "usb1" / "1-3" / "1-3.3"
    _w(usb / "idVendor", "3537")
    for name, iface, axes in [("event15", "1-3.3:1.0", "3003f"), ("event26", "1-3.3:1.1", "0")]:
        _input_node(root, name, usb / iface / "input" / f"input{name[5:]}", axes)
    hid = usb / "1-3.3:1.2" / "0003:3537:100F.0001"
    hid.mkdir(parents=True)
    (root / "class" / "hidraw" / "hidraw11").mkdir(parents=True)
    os.symlink(hid, root / "class" / "hidraw" / "hidraw11" / "device")

    other = root / "devices" / "pci0000:00" / "usb1" / "1-5"
    _w(other / "idVendor", "054c")
    _input_node(root, "event3", other / "1-5:1.0" / "input" / "input3", "3003f")
    return root


def _input_node(root: Path, name: str, dev: Path, axes: str):
    """class/input/eventN/device -> .../inputN, which has capabilities/abs (3003f: X Y Z RX RY RZ)."""
    _w(dev / "capabilities" / "abs", axes)
    event = root / "class" / "input" / name
    event.mkdir(parents=True)
    os.symlink(dev, event / "device")


def test_finds_the_gamepad_on_the_same_usb_device(tmp_path):
    root = fake_sysfs(tmp_path)
    assert liveinput.event_node_for("/dev/hidraw11", root, Path("/dev")) == "/dev/input/event15"
    with pytest.raises(liveinput.Unavailable, match="no longer connected"):
        liveinput.event_node_for("/dev/hidraw4", root)


def test_state_scales_to_fractions():
    live = liveinput.LiveInput.__new__(liveinput.LiveInput)
    stick, trigger = (-32768, 32767), (0, 255)
    live.range = {"lx": stick, "ly": stick, "rx": stick, "ry": stick, "lt": trigger, "rt": trigger}
    live.raw = {"lx": -32768, "ly": 32767, "rx": 0, "ry": 0, "lt": 0, "rt": 255}
    live.held, live.hats = {"A"}, {0x10: -1, 0x11: 0}             # A held, D-pad left
    st = live.state()
    assert (st["lx"], st["ly"], st["lt"], st["rt"]) == (-1.0, 1.0, 0.0, 1.0)
    assert st["pressed"] == ["A", "LEFT", "RT"]                   # RT pulled all the way
    assert abs(st["rx"]) < 0.001 and str(st["ry"]) != "-0.0"


# 0xF4 state reports captured from a real Nova 2 Lite (firmware 8201) by cable, "mapped" mode.
LEFT_STICK_RIGHT = bytes.fromhex("f400170000000000f97f15fb00000000000000ff820115")
LEFT_STICK_UP = bytes.fromhex("f400170000000000a403fc7f00000000000000ff8201af")
LT_FULL = bytes.fromhex("f4001700000100000000000000000000ff0000ff82018d")
A_HELD = bytes.fromhex("f4001700010000000000000000000000000000ff82018e")
DPAD_RIGHT = bytes.fromhex("f4001700000008000000000000000000000000ff820195")


def test_state_from_a_report():
    st = liveinput.state_from_report(LEFT_STICK_RIGHT)
    assert st["lx"] == 1.0 and abs(st["ly"]) < 0.05 and st["pressed"] == []
    assert liveinput.state_from_report(LEFT_STICK_UP)["ly"] == -1.0          # up is negative, as evdev
    lt = liveinput.state_from_report(LT_FULL)
    assert (lt["lt"], lt["rt"], lt["pressed"]) == (1.0, 0.0, ["LT"])
    assert liveinput.state_from_report(A_HELD)["pressed"] == ["A"]
    assert liveinput.state_from_report(DPAD_RIGHT)["pressed"] == ["RIGHT"]
    assert str(liveinput.state_from_report(A_HELD)["rx"]) == "0.0"


def test_state_from_a_report_refuses_others():
    assert liveinput.state_from_report(A_HELD[:-1] + b"\x00") is None          # bad checksum
    assert liveinput.state_from_report(bytes.fromhex("070106000510")) is None
    assert liveinput.state_from_report(b"") is None
