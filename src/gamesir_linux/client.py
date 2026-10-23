"""High-level controller configuration over any GCM transport.

A transport is anything with `async request(frame, accept) -> bytes`: the real
Bluetooth one (ble.py) or the simulator (simulator.py). Every setter writes,
then reads the value back and raises if the controller did not take it.
"""

from __future__ import annotations

from typing import Callable, Protocol

from . import gcm
from .gcm import Battery, BasicInfo, DeadZone, Feature, ProtocolError, Vibration


class Transport(Protocol):
    async def request(self, frame: bytes, accept: Callable[[bytes], bool] | None = None) -> bytes: ...


class NotApplied(RuntimeError):
    """The controller acknowledged a setting but reads back a different value."""


def _echoes(cmd: int) -> Callable[[bytes], bool]:
    return lambda reply: bool(reply) and reply[0] == cmd


class NovaClient:
    def __init__(self, transport: Transport):
        self.t = transport

    async def _ask(self, frame: bytes) -> bytes:
        return await self.t.request(frame, _echoes(frame[0]))

    async def _set(self, frame: bytes) -> None:
        gcm.verify(frame, await self._ask(frame))

    # -- info ---------------------------------------------------------------

    async def info(self) -> BasicInfo:
        reply = await self.t.request(gcm.basic_info_query(), lambda r: len(r) in (19, 20))
        return gcm.basic_info_parse(reply)

    async def battery(self) -> Battery:
        q = gcm.host_info_query()
        return gcm.host_info_parse(q, await self._ask(q))

    # -- dead zones ---------------------------------------------------------

    async def dead_zone(self, zone: DeadZone) -> int:
        q = gcm.dead_zone_query(zone)
        return gcm.dead_zone_parse(q, await self._ask(q))

    async def set_dead_zone(self, zone: DeadZone, value: int) -> None:
        await self._set(gcm.dead_zone_set(zone, value))
        if (now := await self.dead_zone(zone)) != value:
            raise NotApplied(f"{zone.name}: asked for {value}, controller reports {now}")

    # -- feature toggles ----------------------------------------------------

    async def feature(self, feature: Feature) -> bool:
        q = gcm.feature_query(feature)
        return gcm.feature_parse(q, await self._ask(q))

    async def set_feature(self, feature: Feature, enabled: bool) -> None:
        await self._set(gcm.feature_set(feature, enabled))
        if (now := await self.feature(feature)) != enabled:
            raise NotApplied(f"{feature.name}: asked for {enabled}, controller reports {now}")

    # -- vibration ----------------------------------------------------------

    async def vibration(self) -> Vibration:
        q = gcm.vibration_query()
        return gcm.vibration_parse(q, await self._ask(q))

    async def set_vibration(self, v: Vibration) -> None:
        await self._set(gcm.vibration_set(v))
        if (now := await self.vibration()) != v:
            raise NotApplied(f"vibration: asked for {v}, controller reports {now}")

    # -- auto sleep ---------------------------------------------------------

    async def auto_sleep(self) -> int:
        q = gcm.auto_sleep_query()
        return gcm.auto_sleep_parse(q, await self._ask(q))

    async def set_auto_sleep(self, minutes: int) -> None:
        await self._set(gcm.auto_sleep_set(minutes))
        if (now := await self.auto_sleep()) != minutes:
            raise NotApplied(f"auto sleep: asked for {minutes}, controller reports {now}")

    # -- restore ------------------------------------------------------------

    async def restore_defaults(self) -> None:
        await self._set(gcm.restore_all())


__all__ = ["NovaClient", "NotApplied", "ProtocolError", "Transport"]
