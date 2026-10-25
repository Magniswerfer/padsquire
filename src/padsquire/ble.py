# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bluetooth LE transport for the GCM protocol, via bleak (BlueZ on Linux).

What the Android app does for a Nova 2 Lite (BluetoothLeService):
  - finds the BLE config identity "GameSir-Nova 2 Lite_xxxx" (not "_DFU")
  - service 0x8650; enables notifications on 0x865F and 0x8655
  - writes each command to 0x865F (write with response)
  - waits ~60 ms (bleWriteAfterReadIntervalTime) and reads the reply from 0x865F

ASSUMPTION: replies are read from 0x865F rather than delivered by notification
(the Nova 2 Lite config sets bleIsNotifyCallback = false). If hardware proves
otherwise, NOTIFY_REPLIES switches to notification mode.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

SERVICE_UUID = "00008650-0000-1000-8000-00805f9b34fb"
COMMAND_UUID = "0000865f-0000-1000-8000-00805f9b34fb"
DATA_UUID = "00008655-0000-1000-8000-00805f9b34fb"
NAME_PREFIX = "GameSir-Nova 2 Lite_"
DFU_NAME = "GameSir-Nova 2 Lite_DFU"

READ_DELAY = 0.06
READ_ATTEMPTS = 4
NOTIFY_REPLIES = False


class BleError(RuntimeError):
    pass


def _bleak():
    try:
        import bleak
    except ImportError:
        raise BleError("Bluetooth support needs bleak: pip install 'padsquire[ble]'") from None
    return bleak


def is_config_name(name: str | None) -> bool:
    return bool(name) and name.startswith(NAME_PREFIX) and name != DFU_NAME


async def scan(timeout: float = 6.0) -> list[tuple[str, str]]:
    """(address, name) of every Nova 2 Lite config channel in range."""
    bleak = _bleak()
    try:
        found = await bleak.BleakScanner.discover(timeout=timeout)
    except FileNotFoundError:
        raise BleError("Bluetooth is unavailable: is bluetoothd running? "
                       "(systemctl status bluetooth)") from None
    except Exception as e:
        raise BleError(f"Bluetooth scan failed: {e or type(e).__name__} "
                       "(is the adapter powered on? bluetoothctl power on)") from e
    return sorted((d.address, d.name) for d in found if is_config_name(d.name))


class BleTransport:
    """Wraps a connected bleak client (or anything with the same methods)."""

    def __init__(self, client, read_delay: float = READ_DELAY, attempts: int = READ_ATTEMPTS):
        self.client = client
        self.read_delay = read_delay
        self.attempts = attempts
        self._notified: asyncio.Queue[bytes] = asyncio.Queue()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        # The app subscribes to both characteristics; some firmware only answers
        # once that has happened. Failures here are not fatal.
        for uuid in (COMMAND_UUID, DATA_UUID):
            try:
                await self.client.start_notify(uuid, self._on_notify if uuid == COMMAND_UUID else _ignore)
            except Exception:
                pass

    def _on_notify(self, _sender, data: bytearray) -> None:
        if NOTIFY_REPLIES:
            self._notified.put_nowait(bytes(data))

    async def request(self, frame: bytes, accept: Callable[[bytes], bool] | None = None) -> bytes:
        accept = accept or (lambda _r: True)
        async with self._lock:
            await self.client.write_gatt_char(COMMAND_UUID, frame, response=True)
            last = b""
            for _ in range(self.attempts):
                await asyncio.sleep(self.read_delay)
                if NOTIFY_REPLIES:
                    last = await asyncio.wait_for(self._notified.get(), timeout=1.0)
                else:
                    last = bytes(await self.client.read_gatt_char(COMMAND_UUID))
                if accept(last):
                    return last
            raise BleError(f"no valid reply to {frame.hex(' ')} (last read: {last.hex(' ') or 'nothing'})")


def _ignore(_sender, _data) -> None:
    pass


class connect:
    """async with connect(address) as transport: ..."""

    def __init__(self, address: str, timeout: float = 15.0):
        self.address, self.timeout = address, timeout

    async def __aenter__(self) -> BleTransport:
        bleak = _bleak()
        self._client = bleak.BleakClient(self.address, timeout=self.timeout)
        try:
            await self._client.connect()
        except FileNotFoundError:
            raise BleError("Bluetooth is unavailable: is bluetoothd running? "
                           "(systemctl status bluetooth)") from None
        except Exception as e:
            raise BleError(f"could not connect to {self.address}: {e or type(e).__name__}") from e
        transport = BleTransport(self._client)
        await transport.start()
        return transport

    async def __aexit__(self, *exc) -> None:
        await self._client.disconnect()
