import asyncio

import pytest

from gamesir_linux.client import NotApplied, NovaClient
from gamesir_linux.gcm import DeadZone, Feature, ProtocolError, Vibration
from gamesir_linux.simulator import SimulatedNova


def run(coro):
    return asyncio.run(coro)


def test_info_and_battery():
    nova = NovaClient(SimulatedNova(firmware="8201", battery=0x80 | 64))
    info = run(nova.info())
    assert info.firmware_version == "8201"
    bat = run(nova.battery())
    assert (bat.level, bat.charging) == (64, True)


def test_settings_roundtrip():
    sim = SimulatedNova()
    nova = NovaClient(sim)

    async def go():
        await nova.set_dead_zone(DeadZone.RIGHT_TRIGGER_FRONT, 12)
        await nova.set_feature(Feature.SWAP_STICKS, True)
        await nova.set_auto_sleep(30)
        v = Vibration(0, 0, False, False, 7, 7, True, True)
        await nova.set_vibration(v)
        return (await nova.dead_zone(DeadZone.RIGHT_TRIGGER_FRONT), await nova.feature(Feature.SWAP_STICKS),
                await nova.auto_sleep(), await nova.vibration())

    assert run(go()) == (12, True, 30, Vibration(0, 0, False, False, 7, 7, True, True))
    assert sim.dead_zones[DeadZone.LEFT_STICK_INNER] == 5  # untouched


def test_restore():
    sim = SimulatedNova()
    nova = NovaClient(sim)
    run(nova.set_dead_zone(DeadZone.LEFT_STICK_OUTER, 40))
    run(nova.restore_defaults())
    assert run(nova.dead_zone(DeadZone.LEFT_STICK_OUTER)) == 5


class StubbornNova(SimulatedNova):
    """Acknowledges writes but never applies them."""

    def handle(self, f):
        if len(f) > 3 and f[3] == 1:
            f = f[:3] + b"\x00\x00" + f[5:]
            f = f[:-1] + bytes([sum(f[:-1]) & 0xFF])
        return super().handle(f)


def test_setting_that_does_not_stick_is_reported():
    with pytest.raises(NotApplied):
        run(NovaClient(StubbornNova()).set_dead_zone(DeadZone.LEFT_STICK_INNER, 20))


def test_corrupt_reply_raises():
    sim = SimulatedNova()
    sim.corrupt_next = True
    with pytest.raises(ProtocolError):
        run(NovaClient(sim).dead_zone(DeadZone.LEFT_STICK_INNER))


def test_second_batch_roundtrip():
    from gamesir_linux.gcm import Key
    sim = SimulatedNova()
    nova = NovaClient(sim)

    async def go():
        await nova.set_mapping(Key.L4, Key.A)
        await nova.set_mapping(Key.R4, None)
        await nova.set_trigger_quick(True, False)
        await nova.switch_slot(2)
        await nova.set_light((10, 20, 30))
        return (await nova.mapping(Key.L4), await nova.mapping(Key.R4), await nova.trigger_quick(),
                await nova.slots(), (await nova.light()).rgb)

    l4, r4, quick, slots, rgb = run(go())
    assert l4.kind == "key" and l4.targets == (Key.A,)
    assert r4.kind == "disabled"
    assert quick == (True, False) and slots == (3, 2) and rgb == (10, 20, 30)


def test_invalid_slot_is_reported():
    with pytest.raises(NotApplied):
        run(NovaClient(SimulatedNova()).switch_slot(7))
