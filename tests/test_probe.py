# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import json
import os
from pathlib import Path

from gamesir_linux import ble, probe
from gamesir_linux.simulator import SimulatedNova

# A HID report descriptor fragment (Usage Page: Generic Desktop, Usage: Game Pad).
HID_DESC = bytes.fromhex("05010905a101850181020600ffc0")


def _w(path: Path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text) if isinstance(text, bytes) else path.write_text(text + "\n")


def fake_sysfs(root: Path) -> Path:
    """A /sys tree shaped like the real one: a wired Nova 2 Lite, a controller in
    update mode (shows up as a disk), and an unrelated keyboard."""
    devs = root / "bus" / "usb" / "devices"
    drivers = root / "bus" / "usb" / "drivers"
    for d in ("usbhid", "usb-storage", "hid-generic"):
        (drivers / d).mkdir(parents=True)

    pad = devs / "1-2"
    for k, v in dict(idVendor="3537", idProduct="100f", bcdDevice="0100", manufacturer="GameSir",
                     product="GameSir-Nova 2 Lite", speed="12", version=" 2.00", bMaxPower="500mA",
                     bConfigurationValue="1", serial="SECRET123").items():
        _w(pad / k, v)
    i0 = pad / "1-2:1.0"
    for k, v in dict(bInterfaceNumber="00", bInterfaceClass="03", bInterfaceSubClass="00",
                     bInterfaceProtocol="00").items():
        _w(i0 / k, v)
    os.symlink(drivers / "usbhid", i0 / "driver")
    for k, v in dict(bEndpointAddress="81", type="Interrupt", wMaxPacketSize="0040", bInterval="01").items():
        _w(i0 / "ep_81" / k, v)
    hid = i0 / "0003:3537:100F.0001"
    _w(hid / "report_descriptor", HID_DESC)
    (hid / "hidraw" / "hidraw3").mkdir(parents=True)
    os.symlink(drivers / "hid-generic", hid / "driver")

    upd = devs / "2-1"
    for k, v in dict(idVendor="3537", idProduct="105a", manufacturer="JieLi", product="UDISK",
                     speed="12").items():
        _w(upd / k, v)
    u0 = upd / "2-1:1.0"
    for k, v in dict(bInterfaceNumber="00", bInterfaceClass="08", bInterfaceSubClass="06",
                     bInterfaceProtocol="50").items():
        _w(u0 / k, v)
    os.symlink(drivers / "usb-storage", u0 / "driver")
    lun = u0 / "host3" / "target3:0:0" / "3:0:0:0"
    for k, v in dict(vendor="JIELI", model="AC695X_TRANS", rev="N2LT").items():
        _w(lun / k, v)
    blk = lun / "block" / "sdb"
    _w(blk / "size", "2048")
    _w(blk / "removable", "1")
    os.symlink(lun, blk / "device")
    (lun / "scsi_generic" / "sg2").mkdir(parents=True)

    kbd = devs / "3-1"
    for k, v in dict(idVendor="046d", idProduct="c31c", manufacturer="Logitech", product="Keyboard").items():
        _w(kbd / k, v)
    return root


PROC_INPUT = """I: Bus=0003 Vendor=046d Product=c31c Version=0110
N: Name="Logitech Keyboard"
H: Handlers=sysrq kbd event2

I: Bus=0003 Vendor=3537 Product=100f Version=0100
N: Name="GameSir-Nova 2 Lite"
H: Handlers=event5 js0
"""


def test_usb_probe(tmp_path):
    devices = probe.probe_usb(fake_sysfs(tmp_path / "sys"))
    assert [d["path"] for d in devices] == ["1-2", "2-1"]   # keyboard filtered out
    pad, upd = devices
    assert (pad["vid"], pad["pid"], pad["product"]) == ("3537", "100f", "GameSir-Nova 2 Lite")
    assert pad["has_serial"] and "SECRET123" not in json.dumps(devices)
    iface = pad["interfaces"][0]
    assert iface["driver"] == "usbhid" and iface["endpoints"][0]["address"] == "81"
    assert iface["hid"][0]["report_descriptor"] == HID_DESC.hex()
    assert iface["hid"][0]["hidraw"] == ["hidraw3"]
    disk = upd["interfaces"][0]["storage"][0]
    assert (disk["model"], disk["rev"], disk["bytes"]) == ("AC695X_TRANS", "N2LT", 2048 * 512)
    assert upd["interfaces"][0]["scsi_generic"] == ["sg2"]


def test_input_probe(tmp_path):
    _w(tmp_path / "bus" / "input" / "devices", PROC_INPUT)
    assert probe.probe_input(tmp_path) == [
        {"vid": "3537", "pid": "100f", "name": "GameSir-Nova 2 Lite", "handlers": ["event5", "js0"]}]


def test_extra_ids_include_emulated_identity(tmp_path):
    # The 2.4 GHz dongle takes on the controller mode's identity, e.g. a DualShock 4.
    sys_root = fake_sysfs(tmp_path / "sys")
    _w(tmp_path / "bus" / "input" / "devices", PROC_INPUT)
    devices = probe.probe_usb(sys_root, extra_ids=[("046d", "c31c")])
    assert [d["path"] for d in devices] == ["1-2", "2-1", "3-1"]
    found = probe.probe_input(tmp_path, extra_ids=[("046d", "c31c")])
    assert [d["pid"] for d in found] == ["c31c", "100f"]


def test_summary_mentions_everything(tmp_path):
    snap = probe.snapshot("wired", sys_root=fake_sysfs(tmp_path / "sys"), proc_root=tmp_path)
    text = "\n".join(probe.summarize(snap))
    assert "3537:100f" in text and "hidraw3" in text and "AC695X_TRANS" in text


def test_missing_sysfs_is_harmless(tmp_path):
    assert probe.probe_usb(tmp_path / "nowhere") == []
    assert probe.probe_input(tmp_path / "nowhere") == []


# -- Bluetooth ------------------------------------------------------------------------

class Char:
    def __init__(self, uuid, props, handle):
        self.uuid, self.properties, self.handle, self.descriptors = uuid, props, handle, []


class Service:
    def __init__(self, uuid, chars, handle):
        self.uuid, self.characteristics, self.handle = uuid, chars, handle


class FakeNovaGatt:
    """GATT table as the app expects it, answering from the simulator."""

    def __init__(self, notify_mode=False):
        self.device = SimulatedNova()
        self.notify_mode = notify_mode
        self.handlers = {}
        self.values = {ble.COMMAND_UUID: b"", ble.DATA_UUID: b"\x00" * 8}
        self.writes = []
        self.services = [
            Service("0000180a-0000-1000-8000-00805f9b34fb",
                    [Char("00002a26-0000-1000-8000-00805f9b34fb", ["read"], 3)], 1),
            Service(ble.SERVICE_UUID, [Char(ble.COMMAND_UUID, ["read", "write", "notify"], 10),
                                       Char(ble.DATA_UUID, ["read", "write", "notify"], 13)], 9),
        ]

    async def start_notify(self, uuid, handler):
        self.handlers[uuid] = handler

    async def write_gatt_char(self, uuid, data, response=False):
        self.writes.append((uuid, bytes(data)))
        reply = self.device.handle(bytes(data))
        if self.notify_mode:
            self.handlers[uuid](None, bytearray(reply))
        else:
            self.values[uuid] = reply

    async def read_gatt_char(self, uuid):
        if uuid == "00002a26-0000-1000-8000-00805f9b34fb":
            return bytearray(b"V8201\x00")
        return bytearray(self.values[uuid])


def test_gatt_probe_read_mode():
    gatt = FakeNovaGatt()
    report = asyncio.run(probe.probe_gatt(gatt, settle=0))
    assert report["reads"]["00002a26-0000-1000-8000-00805f9b34fb"]["text"] == "V8201"
    assert [e["verdict"] for e in report["experiments"]] == ["read:865f"] * 3
    # Only queries, only to 0x865F.
    assert {u for u, _ in gatt.writes} == {ble.COMMAND_UUID}
    assert [f for _, f in gatt.writes] == [f for _, f in probe.READ_ONLY_QUERIES]
    for _, frame in gatt.writes:
        if len(frame) == 6:                # query/set shape: action byte 0 means query
            assert frame[3] == 0


def test_gatt_probe_notify_mode():
    report = asyncio.run(probe.probe_gatt(FakeNovaGatt(notify_mode=True), settle=0))
    assert [e["verdict"] for e in report["experiments"]] == ["notify:865f"] * 3


def test_gatt_probe_without_gcm_service():
    gatt = FakeNovaGatt()
    gatt.services = gatt.services[:1]
    report = asyncio.run(probe.probe_gatt(gatt, settle=0))
    assert report["experiments"] == [] and "not found" in report["note"]
    assert gatt.writes == []


def test_guided_cli_writes_report(tmp_path, monkeypatch):
    from gamesir_linux import cli_probe
    from gamesir_linux.cli import main

    answers = iter(["", "q"])
    monkeypatch.setattr(cli_probe, "_ask", lambda _p: next(answers))
    out = tmp_path / "report.json"
    assert main(["probe", "--guided", "-o", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["probe_format"] == 1 and report["system"]["gamesir_linux"]
    assert [s["label"] for s in report["snapshots"]] == ["wired-xinput"]
