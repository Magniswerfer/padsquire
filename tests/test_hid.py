# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import socket
import threading
from pathlib import Path

import pytest

from padsquire import gcm, hid
from padsquire.client import NovaClient
from padsquire.gcm import DeadZone

VENDOR_DESC = bytes.fromhex("067aff0901a1010902a1000903150025ff75089540810209059102c0c0")
KEYBOARD_DESC = bytes.fromhex("05010906a101850105071900")


def _report(hexdata: str) -> bytes:
    data = bytes.fromhex(hexdata)
    return data + bytes(64 - len(data))


# Replies captured from a real Nova 2 Lite (firmware 7701) over the cable.
INFO_REPLY = _report("01df25f1388d" "0100" "0000" "7701" "00000000" "0402" "ff")
DEAD_ZONE_REPLY = _report("07010600050e")[:63] + b"\x21"   # 0x21: sum over the report
BATTERY_REPLY = _report("240105527c")


def _node(root: Path, name: str, hid_id: str, descriptor: bytes, hid_name: str = "2.4G XBOX 360 For Windows"):
    dev = root / "class" / "hidraw" / name / "device"
    dev.mkdir(parents=True)
    (dev / "uevent").write_text(f"DRIVER=hid-generic\nHID_ID={hid_id}\nHID_NAME={hid_name}\n")
    (dev / "report_descriptor").write_bytes(descriptor)


def test_find_picks_only_known_config_interfaces(tmp_path):
    _node(tmp_path, "hidraw10", "0003:00003537:0000100F", KEYBOARD_DESC)      # keyboard interface
    _node(tmp_path, "hidraw11", "0003:00003537:0000100F", VENDOR_DESC)        # the config channel
    _node(tmp_path, "hidraw8", "0003:00003537:00002106", VENDOR_DESC, "Zikway HID zk")   # idle dongle
    _node(tmp_path, "hidraw3", "0003:0000054C:000009CC", VENDOR_DESC)         # not GameSir
    found = hid.find(tmp_path, Path("/dev"))
    assert [(d.path, d.pid, d.link) for d in found] == [("/dev/hidraw11", 0x100F, "cable")]
    assert hid.find(tmp_path / "nowhere") == []


def test_trim_handles_both_reply_shapes():
    assert hid._trim(BATTERY_REPLY) == bytes.fromhex("240105527c")   # framed: own checksum
    assert hid._trim(DEAD_ZONE_REPLY) == DEAD_ZONE_REPLY             # whole-report checksum
    assert hid._trim(INFO_REPLY) == INFO_REPLY
    assert hid._trim(_report("1f000100000020")) == bytes.fromhex("1f000100000020")   # no length byte


class FakeHidraw:
    """The device end of a stand-in hidraw node: answers each command from a table."""

    def __init__(self, replies: dict[bytes, list[bytes]], queued: list[bytes] = ()):
        self.host, self.dev = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.replies, self.writes = replies, []
        for r in queued:
            self.dev.send(r)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while data := self.dev.recv(65):
            self.writes.append(data)
            assert data[0] == 0 and len(data) == 65   # report ID 0 + one 64-byte report
            cmd = data[1:]
            for prefix, answers in self.replies.items():
                if cmd.startswith(prefix):
                    for a in answers:
                        self.dev.send(a)
                    break

    def close(self):
        self.dev.shutdown(socket.SHUT_RDWR)
        self.host.close()


def test_client_reads_a_real_controller_over_hid():
    fake = FakeHidraw({
        bytes([0x01, 0x01]): [INFO_REPLY],
        bytes.fromhex("070106"): [_report("f4" + "11" * 10), DEAD_ZONE_REPLY],   # state report first
        bytes.fromhex("24010429"): [BATTERY_REPLY],
    }, queued=[_report("aa")])   # stale report, must be drained before writing
    try:
        nova = NovaClient(hid.HidTransport(fake.host.fileno(), timeout=0.5))
        info = asyncio.run(nova.info())
        assert (info.firmware_version, info.mac, info.gcm_version) == ("7701", "01:DF:25:F1:38:8D", "2.4")
        assert asyncio.run(nova.dead_zone(DeadZone.LEFT_STICK_INNER)) == 5
        assert asyncio.run(nova.battery()).raw == 0x52
        assert fake.writes[1][1:7] == gcm.dead_zone_query(DeadZone.LEFT_STICK_INNER)
    finally:
        fake.close()


def test_no_reply_raises():
    fake = FakeHidraw({})
    try:
        transport = hid.HidTransport(fake.host.fileno(), timeout=0.01, attempts=3)
        with pytest.raises(hid.HidError, match="no valid reply to 26 01"):
            asyncio.run(transport.request(bytes.fromhex("26010600002d")))
    finally:
        fake.close()


def test_update_mode_command_is_one_padded_report():
    fake = FakeHidraw({})
    hid.send_update_mode(fake.host.fileno())
    fake.close()
    fake.thread.join(1)
    # As GameSir's DLL sends it: report ID 0, then f2 00 04 f6 and zeros.
    assert fake.writes == [b"\x00\xf2\x00\x04\xf6" + bytes(60)]


def test_controller_firmware_from_a_state_report():
    report = _report("f40017" + "00" * 16 + "ff82018d")
    fake = FakeHidraw({
        bytes.fromhex("f3010502"): [_report("f3010502fb"), report, report],
        bytes.fromhex("f3010500"): [_report("f3010500f9")],
    })
    try:
        nova = NovaClient(hid.HidTransport(fake.host.fileno(), timeout=0.5))
        assert asyncio.run(nova.controller_firmware()) == "8201"
        assert [w[1:5].hex() for w in fake.writes] == ["f3010502", "f3010500"]   # switched off again
    finally:
        fake.close()
