# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import math

import pytest

from padsquire import gcm, stickcheck
from padsquire.client import NovaClient
from padsquire.gcm import DeadZone
from padsquire.simulator import SimulatedNova


def circle(radius_at, steps=720):
    """Points around the edge, `radius_at(angle in degrees)` far out."""
    return [(radius_at(a) * math.cos(math.radians(a)), radius_at(a) * math.sin(math.radians(a)))
            for a in (i * 360 / steps for i in range(steps))]


def test_drift_of_a_still_stick():
    r = stickcheck.drift([(0.0, 0.0)] * 50)
    assert r["peak"] == 0 and r["suggested_inner"] == stickcheck.DRIFT_MARGIN


def test_drift_suggests_a_little_more_than_the_worst_wander():
    r = stickcheck.drift([(0.03, 0.0), (0.04, 0.03), (0.0, -0.02)])
    assert r["peak"] == 5.0                                   # hypot(0.04, 0.03)
    assert r["suggested_inner"] == 5 + stickcheck.DRIFT_MARGIN
    assert r["centre"] == [2.3, 0.3]


def test_drift_suggestion_is_capped():
    assert stickcheck.drift([(0.9, 0.0)])["suggested_inner"] == 50


def test_drift_needs_samples():
    with pytest.raises(ValueError):
        stickcheck.drift([])


def test_reach_of_a_stick_that_gets_everywhere():
    r = stickcheck.reach(circle(lambda a: 1.0))
    assert r["complete"] and r["coverage"] == 1.0
    assert r["short"] == 0 and r["suggested_outer"] == 0 and r["weakest"] == 100.0


def test_reach_square_corners_count_as_full():
    # xpad clamps each axis, so diagonals come out further than 1; that's not a shortfall
    r = stickcheck.reach(circle(lambda a: 1.0 / max(abs(math.cos(math.radians(a))), abs(math.sin(math.radians(a))))))
    assert r["short"] == 0 and r["suggested_outer"] == 0 and max(r["sectors"]) <= 1.5


def test_reach_suggests_an_outer_dead_zone_for_a_weak_direction():
    r = stickcheck.reach(circle(lambda a: 0.9 if 80 <= a < 100 else 1.0))
    assert r["weakest"] == 90.0 and r["short"] >= 2
    assert r["suggested_outer"] == 10 + stickcheck.REACH_MARGIN


def test_reach_suggestion_stops_at_the_firmware_limit():
    r = stickcheck.reach(circle(lambda a: 0.6))
    assert r["suggested_outer"] == gcm.OUTER_DEAD_ZONE_MAX and r["outer_too_small"]


def test_reach_needs_most_of_the_circle():
    r = stickcheck.reach(circle(lambda a: 1.0)[:200])            # a quarter turn
    assert not r["complete"] and r["suggested_outer"] is None
    assert r["sectors"].count(None) > 20


def test_reach_ignores_the_way_out_to_the_edge():
    r = stickcheck.reach([(0.1, 0.0), (0.3, 0.0)])
    assert r["weakest"] is None and r["coverage"] == 0


class FakeLive:
    def __init__(self, positions):
        self.positions = positions
        self.pumps = 0

    def pump(self):
        self.pumps += 1

    def state(self):
        return dict(self.positions)


def test_zeroed_puts_the_dead_zones_back():
    sim = SimulatedNova()
    nova = NovaClient(sim)
    sim.dead_zones[DeadZone.LEFT_STICK_INNER] = 7

    async def go():
        async with stickcheck.zeroed(nova, [DeadZone.LEFT_STICK_INNER, DeadZone.RIGHT_STICK_OUTER]) as original:
            assert original == {DeadZone.LEFT_STICK_INNER: 7, DeadZone.RIGHT_STICK_OUTER: 0}
            assert sim.dead_zones[DeadZone.LEFT_STICK_INNER] == 0
    asyncio.run(go())
    assert sim.dead_zones[DeadZone.LEFT_STICK_INNER] == 7
    # a zone that was 0 already isn't written at all
    assert not any(f[0] == gcm.CMD_DEAD_ZONE and f[1] == DeadZone.RIGHT_STICK_OUTER and f[3] == 1 for f in sim.log)


def test_zeroed_puts_them_back_when_the_test_fails():
    sim = SimulatedNova()
    nova = NovaClient(sim)

    async def go():
        async with stickcheck.zeroed(nova, [DeadZone.LEFT_STICK_INNER]):
            raise OSError("unplugged")
    with pytest.raises(OSError):
        asyncio.run(go())
    assert sim.dead_zones[DeadZone.LEFT_STICK_INNER] == 5


def test_run_drift_measures_with_the_inner_dead_zone_off():
    sim = SimulatedNova()
    nova = NovaClient(sim)
    seen = []

    class Watching(FakeLive):
        def state(self):
            seen.append(sim.dead_zones[DeadZone.LEFT_STICK_INNER])
            return super().state()

    live = Watching({"lx": 0.04, "ly": 0.0, "rx": 0.0, "ry": 0.0})
    r = asyncio.run(stickcheck.run(nova, live, "drift", 0.05))
    assert set(seen) == {0} and sim.dead_zones[DeadZone.LEFT_STICK_INNER] == 5
    assert r["left"]["peak"] == 4.0 and r["left"]["current"] == 5 and r["left"]["zone"] == "LEFT_STICK_INNER"
    assert r["right"]["suggested_inner"] == stickcheck.DRIFT_MARGIN


def test_run_refuses_an_unknown_test():
    with pytest.raises(ValueError):
        asyncio.run(stickcheck.run(NovaClient(SimulatedNova()), FakeLive({}), "spin", 0.01))
