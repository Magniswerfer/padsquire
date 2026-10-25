# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import socket
import sys
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


def test_find_picks_only_known_config_interfaces(tmp_path, monkeypatch):
    monkeypatch.setattr(hid, "MACOS", False)
    _node(tmp_path, "hidraw10", "0003:00003537:0000100F", KEYBOARD_DESC)      # keyboard interface
    _node(tmp_path, "hidraw11", "0003:00003537:0000100F", VENDOR_DESC)        # the config channel
    _node(tmp_path, "hidraw8", "0003:00003537:00002106", VENDOR_DESC, "Zikway HID zk")   # idle dongle
    _node(tmp_path, "hidraw3", "0003:0000054C:000009CC", VENDOR_DESC)         # not GameSir
    found = hid.find(tmp_path, Path("/dev"))
    assert [(d.path, d.pid, d.link) for d in found] == [("/dev/hidraw11", 0x100F, "cable")]
    assert hid.find(tmp_path / "nowhere") == []


def test_find_on_macos_lists_each_config_interface_once():
    def lister(vid, pid):
        assert (vid, pid) == (hid.VENDOR_ID, 0)
        base = {"vendor_id": vid, "manufacturer_string": "2.4G", "product_string": "XBOX 360 For Windows"}
        return [{**base, "product_id": 0x100F, "usage_page": 0x0001, "path": b"DevSrvsID:2"},     # keyboard
                {**base, "product_id": 0x100F, "usage_page": 0xFF7A, "path": b"DevSrvsID:1"},
                {**base, "product_id": 0x100F, "usage_page": 0xFF7A, "path": b"DevSrvsID:1"},     # same, twice
                {**base, "product_id": 0x2106, "usage_page": 0xFF7A, "path": b"DevSrvsID:3"}]     # idle dongle
    assert hid._find_hidapi(lister) == [hid.HidDevice("DevSrvsID:1", 0x100F, "2.4G XBOX 360 For Windows")]


class FakeHidapiDevice:
    """hidapi's device object, answering from a table like FakeHidraw below."""

    def __init__(self, replies: dict[bytes, list[bytes]]):
        self.replies, self.writes, self.queue = replies, [], []

    def write(self, report: bytes) -> int:
        assert report[0] == 0 and len(report) == 65
        self.writes.append(report[1:])
        for prefix, answers in self.replies.items():
            if report[1:].startswith(prefix):
                self.queue.extend(answers)
                break
        return len(report)

    def read(self, size: int, timeout_ms: int) -> list[int]:
        assert timeout_ms >= 1
        return list(self.queue.pop(0)) if self.queue else []


def test_client_reads_over_hidapi():
    fake = FakeHidapiDevice({gcm.basic_info_query(): [INFO_REPLY],
                             gcm.dead_zone_query(DeadZone.LEFT_STICK_INNER): [DEAD_ZONE_REPLY]})
    client = NovaClient(hid.HidapiTransport(fake, timeout=0.01, attempts=2))
    assert asyncio.run(client.info()).firmware_version == "7701"
    assert asyncio.run(client.dead_zone(DeadZone.LEFT_STICK_INNER)) == 5


STATE = _report("f4001700010000000000000000000000000000ff82018e")   # A held


def test_live_state_switches_reports_on_and_keeps_the_newest():
    on, off = gcm.state_report_set(gcm.StateReport.MAPPED), gcm.state_report_set(gcm.StateReport.OFF)
    fake = FakeHidapiDevice({on: [on.ljust(64, b"\0"), STATE], off: [off.ljust(64, b"\0")],
                             gcm.basic_info_query(): [STATE, INFO_REPLY]})
    t = hid.HidapiTransport(fake, timeout=0.01, attempts=2)

    async def go():
        await t.live_state(on=True)
        await t.live_state(on=True)                      # a second viewer: no second command
        assert await t.live_state() == STATE
        # a settings read in between skips the state report and keeps it
        assert (await NovaClient(t).info()).firmware_version == "7701"
        await t.live_state(on=False)
        assert fake.writes.count(off.ljust(64, b"\0")) == 0     # one viewer is still watching
        await t.live_state(on=False)

    asyncio.run(go())
    assert fake.writes.count(on.ljust(64, b"\0")) == 1 and fake.writes.count(off.ljust(64, b"\0")) == 1


def test_trim_handles_both_reply_shapes():
    assert hid._trim(BATTERY_REPLY) == bytes.fromhex("240105527c")   # framed: own checksum
    assert hid._trim(DEAD_ZONE_REPLY) == DEAD_ZONE_REPLY             # whole-report checksum
    assert hid._trim(INFO_REPLY) == INFO_REPLY
    assert hid._trim(_report("1f000100000020")) == bytes.fromhex("1f000100000020")   # no length byte


# One message per report, like hidraw. macOS has no SOCK_SEQPACKET; its datagrams keep the boundaries too.
SOCKET_TYPE = socket.SOCK_DGRAM if sys.platform == "darwin" else socket.SOCK_SEQPACKET


class FakeHidraw:
    """The device end of a stand-in hidraw node: answers each command from a table."""

    def __init__(self, replies: dict[bytes, list[bytes]], queued: list[bytes] = ()):
        self.host, self.dev = socket.socketpair(socket.AF_UNIX, SOCKET_TYPE)
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
        if SOCKET_TYPE == socket.SOCK_DGRAM:
            self.host.send(b"")    # ends _serve after what's queued; a shutdown would drop that on macOS
        else:
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


def test_update_mode_is_refused_on_macos(monkeypatch):
    monkeypatch.setattr(hid, "MACOS", True)
    with pytest.raises(hid.HidError, match="macOS"):
        hid.enter_update_mode("DevSrvsID:1")


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
