"""High-level controller configuration over any GCM transport.

A transport is anything with `async request(frame, accept) -> bytes`: Bluetooth
(ble.py), USB HID (hid.py) or the simulator (simulator.py). Every setter writes,
then reads the value back and raises if the controller did not take it.
"""

from __future__ import annotations

import asyncio
from typing import Callable, Protocol

from . import gcm
from .gcm import Battery, BasicInfo, ButtonLight, DeadZone, Feature, Mapping, ProtocolError, Vibration


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
        reply = await self.t.request(gcm.basic_info_query(), lambda r: len(r) >= 19)
        return gcm.basic_info_parse(reply)

    async def controller_firmware(self) -> str:
        """The controller's own firmware version, from a state report. Through the 2.4 GHz
        receiver this is the only source: info() there reports the receiver's firmware."""
        read = getattr(self.t, "state_report", None)
        if read is None:
            raise ProtocolError("not available over this connection")
        version = gcm.state_report_version(await read())
        if version is None:
            raise ProtocolError("this firmware's state reports carry no version")
        return version

    async def test_vibration(self, left: int, right: int, seconds: float = 0.6) -> None:
        """Run the grip motors for a moment (strength in percent), then stop them."""
        send = getattr(self.t, "send", None)
        if send is None:
            raise ProtocolError("not available over this connection")
        await send(gcm.rumble(left, right))
        try:
            await asyncio.sleep(seconds)
        finally:
            await send(gcm.rumble(0, 0))

    async def battery(self) -> Battery:
        q = gcm.host_info_query()
        return gcm.host_info_parse(q, await self._ask(q))

    # -- dead zones ---------------------------------------------------------

    async def dead_zone(self, zone: DeadZone) -> int:
        q = gcm.dead_zone_query(zone)
        return gcm.dead_zone_parse(q, await self._ask(q))

    async def set_dead_zone(self, zone: DeadZone, value: int) -> None:
        if zone in gcm.OUTER_ZONES and value > gcm.OUTER_DEAD_ZONE_MAX:
            raise ValueError(f"outer dead zone above {gcm.OUTER_DEAD_ZONE_MAX}% breaks the stick at full tilt "
                             "(a bug in the controller's firmware)")
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

    # -- button mapping -----------------------------------------------------

    async def mapping(self, source: int) -> Mapping:
        q = gcm.mapping_query(source)
        return gcm.mapping_parse(q, await self._ask(q))

    async def set_mapping(self, source: int, target: int | None) -> None:
        """Map a back button to `target`, or disable it (None)."""
        await self._set(gcm.mapping_set(source, target))
        now = await self.mapping(source)
        ok = now.kind == "disabled" if target is None else (now.kind == "key" and now.targets[:1] == (target,))
        if not ok:
            raise NotApplied(f"{getattr(source, 'name', source)}: controller reports {now}")

    async def remap(self, key: int) -> tuple[int, ...]:
        """What `key` does (cable and receiver): its targets, or () for its own function."""
        q = gcm.remap_query(key)
        return gcm.remap_parse(q, await self._ask(q))

    async def set_remap(self, key: int, targets: list[int] | tuple[int, ...]) -> tuple[int, ...]:
        """Make `key` act as `targets` (1-3 keys pressed together), or its own function if empty.
        The controller answers a target it refuses with no targets; that raises NotApplied."""
        await self._set(gcm.remap_set(key, targets) if targets else gcm.remap_reset(key))
        now = await self.remap(key)
        if sorted(now) != sorted(targets):              # the order may come back differently
            raise NotApplied(f"{getattr(key, 'name', key)}: asked for {list(targets)}, controller reports {list(now)}")
        return now

    async def reset_mapping(self, source: int) -> Mapping:
        await self._set(gcm.mapping_reset(source))
        return await self.mapping(source)

    # -- triggers -----------------------------------------------------------

    async def trigger_quick(self) -> tuple[bool, bool]:
        q = gcm.trigger_quick_query()
        return gcm.trigger_quick_parse(q, await self._ask(q))

    async def set_trigger_quick(self, left: bool, right: bool) -> None:
        await self._set(gcm.trigger_quick_set(left, right))
        if (now := await self.trigger_quick()) != (left, right):
            raise NotApplied(f"quick trigger: asked for {(left, right)}, controller reports {now}")

    # -- profiles -----------------------------------------------------------

    async def slots(self) -> tuple[int, int]:
        """(number of profiles, current profile)."""
        q = gcm.slot_query()
        return gcm.slot_query_parse(q, await self._ask(q))

    async def switch_slot(self, slot: int) -> None:
        await self._set(gcm.slot_switch(slot))
        if (now := (await self.slots())[1]) != slot:
            raise NotApplied(f"profile: asked for {slot}, controller reports {now}")

    # -- lighting -----------------------------------------------------------

    async def light(self) -> ButtonLight:
        q = gcm.light_query()
        return gcm.light_parse(q, await self._ask(q))

    async def set_light(self, rgb: tuple[int, int, int]) -> None:
        await self._set(gcm.light_set(rgb))
        if (now := await self.light()).rgb != tuple(rgb):
            raise NotApplied(f"Home light: asked for {rgb}, controller reports {now.rgb}")

    # -- restore ------------------------------------------------------------

    async def restore_defaults(self) -> None:
        await self._set(gcm.restore_all())


__all__ = ["NovaClient", "NotApplied", "ProtocolError", "Transport"]
