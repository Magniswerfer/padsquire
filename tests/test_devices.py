# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recognising GameSir controllers from Connect's table and the identities others found."""
import json
from pathlib import Path

from padsquire import devices

NOVA_DESC = bytes.fromhex("067aff0901a101")
C2_DESC = bytes.fromhex("06f0ff0901a101")
GAMEPAD_DESC = bytes.fromhex("05010905a101")


def _usb(root: Path, name: str, pid: int, product: str, interfaces: list[tuple[str, bytes]]):
    dev = root / "bus" / "usb" / "devices" / name
    dev.mkdir(parents=True)
    (dev / "idVendor").write_text("3537\n")
    (dev / "idProduct").write_text(f"{pid:04x}\n")
    (dev / "product").write_text(product + "\n")
    for n, (node, desc) in enumerate(interfaces):
        hid = dev / f"{name}:1.{n}" / f"0003:3537:{pid:04X}.000{n}"
        (hid / "hidraw" / node).mkdir(parents=True)
        (hid / "report_descriptor").write_bytes(desc)


def test_usage_page_from_a_descriptor():
    assert devices.usage_page(NOVA_DESC) == 0xFF7A
    assert devices.usage_page(GAMEPAD_DESC) == 0x01
    assert devices.usage_page(bytes.fromhex("a101")) is None


def test_every_connect_model_is_in_a_family():
    data = json.loads((Path(devices.__file__).parent / "data" / "connect-devices.json").read_text())
    assert {m["model"] for m in data["models"]} <= set(devices.FAMILY)


def test_identify_known_shared_and_unknown_ids():
    [nova] = devices.identify(0x100F, added=[])
    assert (nova.name, nova.family, nova.support, nova.link) == ("Nova 2 Lite", "gcm", "settings", "cable")
    assert {m.name for m in devices.identify(0x103E, added=[])} == {"Tarantula Pro", "Nova Pro"}
    # 0x0575: the product name tells a Cyclone 2 from an idle G7 Pro 8K receiver (Deadband)
    assert [m.model for m in devices.identify(0x0575, "GameSir-Cyclone 2", added=[])] == ["C2"]
    assert [m.model for m in devices.identify(0x0575, "Gamepad", added=[])] == ["G7ProCE"]
    assert devices.identify(0x7777, added=[]) == []


def test_plugged_in_lists_every_gamesir_device(tmp_path):
    _usb(tmp_path, "1-1", 0x100F, "XBOX 360 For Windows", [("hidraw1", GAMEPAD_DESC), ("hidraw2", NOVA_DESC)])
    _usb(tmp_path, "1-2", 0x100B, "Xbox 360 Controller", [("hidraw3", C2_DESC)])
    _usb(tmp_path, "1-3", 0x10C8, "Gamepad", [])
    _usb(tmp_path, "1-4", 0x1141, "Nova Vision", [("hidraw4", NOVA_DESC)])
    _usb(tmp_path, "1-5", 0x7777, "Gamepad", [("hidraw5", NOVA_DESC)])
    found = {d["usb_id"]: d for d in devices.plugged_in(tmp_path, Path("/dev"), added=[])}
    assert found["3537:100f"]["support"] == "settings" and not found["3537:100f"]["can_add"]
    assert found["3537:100f"]["interfaces"][1] == {"path": "/dev/hidraw2", "usage_page": 0xFF7A}
    assert found["3537:100b"]["name"] == "Cyclone 2" and found["3537:100b"]["support"] == "settings"
    assert found["3537:100b"]["elsewhere"][0]["name"] == "Deadband"
    eightk = found["3537:10c8"]
    assert (eightk["name"], eightk["support"], eightk["can_add"]) == ("G7 Pro 8K", "no", False)
    assert found["3537:1141"]["support"] == "try" and found["3537:1141"]["can_add"]
    unknown = found["3537:7777"]
    assert (unknown["name"], unknown["support"], unknown["can_add"]) == (None, "unknown", True)


def test_added_controllers_come_first(tmp_path):
    f = tmp_path / "added.json"
    f.write_text(json.dumps({"controllers": [{"pid": 0x7777, "name": "My pad", "family": "gcm"}]}))
    [m] = devices.identify(0x7777, added=devices.load_added(f))
    assert (m.name, m.family, m.support, m.added) == ("My pad", "gcm", "added", True)
    assert devices.load_added(tmp_path / "missing.json") == []


def test_macos_groups_interfaces_per_device():
    def lister(vid, pid):
        base = {"product_id": 0x100F, "serial_number": "", "product_string": "XBOX 360 For Windows"}
        return [{**base, "usage_page": 1, "path": b"A"}, {**base, "usage_page": 0xFF7A, "path": b"B"},
                {**base, "usage_page": 0xFF7A, "path": b"B"}]
    [dev] = devices._scan_hidapi(lister)
    assert [(i.path, i.usage_page) for i in dev.interfaces] == [("A", 1), ("B", 0xFF7A)]
