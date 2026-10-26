# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Cyclone 2 protocol (c2.py) and its read-only USB transport (c2usb.py)."""
from pathlib import Path

import pytest

from padsquire import c2, c2usb
from padsquire.simulator import SimulatedC2


def _report(hexdata: str) -> bytes:
    data = bytes.fromhex(hexdata)
    return data + bytes(64 - len(data))


# A 0x12 state report seen on hardware by cyclone2-linux (docs/protocol.md, "on battery, full",
# 2026-06-03, receiver 3537:100b); the rest of its 64 bytes were not published.
STATE_ON_BATTERY = _report("12808080800f00000000ed0d00feff00000e00a5009b20f9fd"
                           "00000000000000000000006400010118")


def test_profile_reads_come_in_58_byte_pieces():
    frames = c2.read_profile(1)
    assert len(frames) == 12 and frames[0] == bytes([0x0F, 0x04, 1, 0, 0, 58])
    assert frames[-1] == bytes([0x0F, 0x04, 1, 638 >> 8, 638 & 0xFF, 42])
    light = c2.read_profile(c2.LIGHT_PROFILE)
    assert len(light) == 11 and light[-1][5] == 635 - 580
    with pytest.raises(c2.C2Error):
        c2.read_profile(6)


def test_state_report_battery_matches_cyclone2_linux():
    s = c2.parse_state(STATE_ON_BATTERY)
    assert (s.battery, s.charging) == (100, False)
    assert s.sticks == (128, 128, 128, 128) and s.dpad is None and s.buttons == set()


def test_state_report_buttons_and_dpad():
    r = bytearray(STATE_ON_BATTERY)
    r[5] = 0x20 | 0x02                     # A, D-pad right
    r[7] = 0x08 | 0x20                     # L4, M
    s = c2.parse_state(bytes(r))
    assert s.dpad == "right" and s.buttons == {"A", "L4", "M"}


def test_firmware_version_reply():
    reply = _report("100a0000" + b"123".hex() + "0000" + "000000" + b"121".hex())
    assert vars(c2.parse_firmware_version(reply)) == {"controller": "1.2.3", "receiver": "1.2.1"}
    with pytest.raises(c2.C2Error):
        c2.parse_firmware_version(_report("100600"))


def test_profile_layout_adds_up():
    p = bytearray(c2.PROFILE_SIZE)
    p[0:4] = b"Test"
    p[32:36] = bytes([4, 2, 0, 1])
    p[550 + 2:550 + 5] = bytes([1, 12, 97])      # left stick dead zone
    p[494 + 12:494 + 15] = bytes([0x81, 3, 60])  # LT quick trigger: on, mode 1
    p[176 + 10] = 1                              # L4 macro: one step
    p[176 + 11:176 + 16] = bytes([0x20, 0x01, 0x00, 0, 50])
    d = c2.decode_profile(bytes(p))
    assert d["name"] == "Test"
    assert d["vibration"] == {"left": 100, "right": 50, "LT": 0, "RT": 25}
    assert d["sticks"]["left"]["dead_zone"] == {"enabled": 1, "start": 12, "end": 97,
                                                "anti_start": 0, "anti_end": 0}
    assert d["triggers"]["LT"]["quick_trigger"] == {"enabled": 1, "mode": 1, "start": 3, "end": 60}
    assert d["macros"]["L4"]["steps"] == [{"keys": 0x20, "hold_ms": 256, "delay_ms": 50}]
    assert list(d["buttons"]) == list(c2.BUTTON_ORDER) + ["L4", "R4"]
    with pytest.raises(c2.C2Error):
        c2.decode_profile(bytes(679))


def test_cyclone2_linux_lighting_writes_land_in_effect_0():
    """Its writes, seen working on hardware, read as writes into the lighting profile (32):
    `0f 03 20 00 04 01 32` brightness, `0f 03 20 00 05 03 ff 00 00` the first zone."""
    light = bytearray(c2.LIGHT_PROFILE_SIZE)
    for write in ("0f03200004 01 32", "0f03200005 03 ff0000", "0f03200008 03 00ff00",
                  "0f0320000e 03 0000ff", "0f03200011 03 ffffff"):
        w = bytes.fromhex(write.replace(" ", ""))
        assert w[:3] == bytes([0x0F, c2.Cmd.WRITE_PROFILE, c2.LIGHT_PROFILE])
        off, n = w[3] << 8 | w[4], w[5]
        light[off:off + n] = w[6:6 + n]
    effect = c2.decode_light(bytes(light))["effects"][0]
    assert effect["brightness"] == 50
    assert effect["frames"][0] == [(255, 0, 0), (0, 255, 0), (0, 0, 0), (0, 0, 255), (255, 255, 255)]


def test_profile_reader_checks_its_pieces():
    reader = c2.ProfileReader(1)
    assert not reader.add(_report("10050200003a"))                # another profile
    with pytest.raises(c2.C2Error):
        reader.add(_report("100501029a3a"))                      # 666 + 58 > 680
    for frame in c2.read_profile(1):
        reader.add(_report("1005" + frame[2:6].hex()))
    assert reader.complete


@pytest.mark.parametrize("frame", ["0f03200004 0132",       # write a profile
                                   "0ffc",                   # enter update mode
                                   "0f20665532 32",          # vibrate
                                   "0f0d0100",               # set the light's play state
                                   "0f07 02",                # switch profile
                                   "0ffd 01",                # calibration
                                   "1004 01"])               # not the request report
def test_transport_sends_only_reads(frame):
    sim = SimulatedC2()
    t = c2usb.C2Transport(sim.read, sim.write)
    with pytest.raises(c2usb.C2UsbError):
        t.send(bytes.fromhex(frame.replace(" ", "")))
    assert sim.writes == []


def test_everything_reads_back_from_the_simulator():
    sim = SimulatedC2(receiver="121", battery=64, cable=False)
    t = c2usb.C2Transport(sim.read, sim.write)
    out = c2usb.read_everything(t)
    assert out["firmware"] == {"controller": "1.2.3", "receiver": "1.2.1"}
    assert out["state"]["battery"] == 64 and out["state"]["charging"] is False
    assert set(out["profiles"]) == {"1", "2", "3", "4", "5", "32"}
    assert bytes.fromhex(out["profiles"]["32"]["raw"]) == sim.profiles[c2.LIGHT_PROFILE]
    assert out["profiles"]["5"]["decoded"]["name"] == "Shift"
    assert {w[1] for w in sim.writes} <= c2.READ_ONLY


def test_busy_means_ask_again():
    sim = SimulatedC2()
    t = c2usb.C2Transport(sim.read, sim.write)
    t.heartbeat()
    sim.busy_once = True
    assert t.current_profile() == 1
    assert [w[1] for w in sim.writes].count(c2.Cmd.READ_CURRENT_PROFILE) == 2


def _node(root: Path, name: str, hid_id: str, descriptor: bytes):
    dev = root / "class" / "hidraw" / name / "device"
    dev.mkdir(parents=True)
    (dev / "uevent").write_text(f"DRIVER=hid-generic\nHID_ID={hid_id}\nHID_NAME=Xbox 360 Controller\n")
    (dev / "report_descriptor").write_bytes(descriptor)


def test_find_picks_the_0xfff0_interface(tmp_path):
    _node(tmp_path, "hidraw4", "0003:00003537:0000100B", bytes.fromhex("05010905a101"))    # gamepad
    _node(tmp_path, "hidraw5", "0003:00003537:0000100B", bytes.fromhex("06f0ff0901a101"))  # config
    _node(tmp_path, "hidraw6", "0003:00003537:0000100F", bytes.fromhex("067aff0901a101"))  # Nova 2 Lite
    found = c2usb.find(tmp_path, Path("/dev"))
    assert [(d.path, d.pid, d.link) for d in found] == [("/dev/hidraw5", 0x100B, "2.4 GHz receiver")]
