# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Bluetooth transport, driven by a fake bleak client in front of the simulator."""

import asyncio

import pytest

from padsquire import ble
from padsquire.client import NovaClient
from padsquire.gcm import DeadZone
from padsquire.simulator import SimulatedNova


class FakeBleakClient:
    """Mimics bleak's write_gatt_char / read_gatt_char / start_notify."""

    def __init__(self, device, stale_reads=0, notify_fails=False):
        self.device = device
        self.characteristic = b""      # what a read of 0x865F returns
        self.stale_reads = stale_reads
        self.notify_fails = notify_fails
        self.calls = []

    async def start_notify(self, uuid, handler):
        self.calls.append(("notify", uuid))
        if self.notify_fails:
            raise RuntimeError("CCCD write failed")

    async def write_gatt_char(self, uuid, data, response=False):
        self.calls.append(("write", uuid, bytes(data), response))
        self._pending = self.device.handle(bytes(data))

    async def read_gatt_char(self, uuid):
        self.calls.append(("read", uuid))
        if self.stale_reads:
            self.stale_reads -= 1     # controller hasn't updated the value yet
        else:
            self.characteristic = self._pending
        return bytearray(self.characteristic)


def run(coro):
    return asyncio.run(coro)


async def _client(fake, **kw):
    t = ble.BleTransport(fake, read_delay=0, **kw)
    await t.start()
    return NovaClient(t)


def test_write_then_read_on_command_characteristic():
    fake = FakeBleakClient(SimulatedNova())

    async def go():
        nova = await _client(fake)
        return await nova.dead_zone(DeadZone.LEFT_STICK_INNER)

    assert run(go()) == 5
    assert ("notify", ble.COMMAND_UUID) in fake.calls and ("notify", ble.DATA_UUID) in fake.calls
    io = [c for c in fake.calls if c[0] in ("write", "read")]
    assert io[0] == ("write", ble.COMMAND_UUID, bytes.fromhex("07 01 06 00 00 0e"), True)
    assert io[1] == ("read", ble.COMMAND_UUID)


def test_stale_reply_is_retried():
    fake = FakeBleakClient(SimulatedNova(), stale_reads=2)
    fake.characteristic = bytes.fromhex("23 01 06 00 00 2a")  # left over from an earlier command

    async def go():
        nova = await _client(fake)
        return await nova.dead_zone(DeadZone.LEFT_STICK_INNER)

    assert run(go()) == 5
    assert sum(1 for c in fake.calls if c[0] == "read") == 3


def test_gives_up_after_attempts():
    fake = FakeBleakClient(SimulatedNova(), stale_reads=99)
    fake.characteristic = b"\x00"

    async def go():
        nova = await _client(fake, attempts=3)
        await nova.dead_zone(DeadZone.LEFT_STICK_INNER)

    with pytest.raises(ble.BleError, match="no valid reply"):
        run(go())


def test_notify_failure_is_not_fatal():
    fake = FakeBleakClient(SimulatedNova(), notify_fails=True)

    async def go():
        return await (await _client(fake)).battery()

    assert run(go()).level == 76


def test_config_name_filter():
    assert ble.is_config_name("GameSir-Nova 2 Lite_A1B2")
    assert ble.is_config_name("GameSir-Nova 2 Lite_G")
    assert not ble.is_config_name("GameSir-Nova 2 Lite_DFU")   # update mode: never configure
    assert not ble.is_config_name("GameSir-Nova 2 Lite")       # the HID gamepad identity
    assert not ble.is_config_name(None)


def test_cli_simulated(capsys):
    from padsquire.cli import main

    assert main(["ble", "--simulate", "info"]) == 0
    out = capsys.readouterr().out
    assert "firmware      8201" in out and "76% (charging)" in out
    assert main(["ble", "--simulate", "deadzone", "left-stick-inner", "9"]) == 0
    assert "left-stick-inner = 9" in capsys.readouterr().out
    assert main(["ble", "--simulate", "restore"]) == 2  # needs --yes
