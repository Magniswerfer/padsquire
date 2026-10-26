# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stick checks: measure drift and reach from the live input, and suggest dead zones.

Two tests, both read from the gamepad device Linux creates (liveinput.py), whose values come
after the controller's own dead zones. So for the length of a test the dead zones that would
hide what is measured are set to 0, and put back afterwards (`zeroed`):

- drift: the sticks are left alone. The furthest a stick wanders from the centre is its drift;
  the inner dead zone should be a little larger than that.
- reach: the sticks are turned around their edge. The direction a stick reaches least far
  decides the outer dead zone, so that every direction still gets to full tilt.

ASSUMPTION (unconfirmed on hardware): the controller applies dead zones radially, in percent
of the stick's travel (the same model as the Sticks page's circle).
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable
from contextlib import asynccontextmanager

from . import gcm
from .gcm import DeadZone

SECTORS = 36                 # 10 degree slices around the stick for the reach test
SAMPLE_INTERVAL = 0.01       # 100 samples a second
DRIFT_MARGIN = 2             # percent added on top of the measured drift
REACH_MARGIN = 1             # percent added to the outer dead zone
FULL_TILT = 0.97             # a direction counts as reaching full tilt from here
MIN_COVERAGE = 0.8           # share of the slices the reach test must visit

STICKS = {"left": ("lx", "ly"), "right": ("rx", "ry")}
INNER = {"left": DeadZone.LEFT_STICK_INNER, "right": DeadZone.RIGHT_STICK_INNER}
OUTER = {"left": DeadZone.LEFT_STICK_OUTER, "right": DeadZone.RIGHT_STICK_OUTER}


def drift(samples: Iterable[tuple[float, float]]) -> dict:
    """From positions (-1..1) of a stick left alone: its drift and a suggested inner dead zone."""
    points = list(samples)
    if not points:
        raise ValueError("no samples")
    radii = [math.hypot(x, y) for x, y in points]
    peak = 100 * max(radii)
    cx = 100 * sum(x for x, _ in points) / len(points)
    cy = 100 * sum(y for _, y in points) / len(points)
    # Even a stick that held still gets the margin: drift grows with wear and temperature.
    suggest = min(50, math.ceil(peak) + DRIFT_MARGIN)
    return {"peak": round(peak, 1), "centre": [round(cx, 1), round(cy, 1)], "samples": len(points),
            "suggested_inner": suggest}


def reach(samples: Iterable[tuple[float, float]]) -> dict:
    """From positions of a stick turned around its edge: how far it reaches in each direction
    and a suggested outer dead zone. A slice the stick never got near the edge in stays None."""
    best: list[float | None] = [None] * SECTORS
    for x, y in samples:
        r = math.hypot(x, y)
        if r < 0.5:                      # on its way to the edge, not at it
            continue
        i = int((math.degrees(math.atan2(y, x)) % 360) // (360 / SECTORS)) % SECTORS
        best[i] = r if best[i] is None else max(best[i], r)
    seen = [r for r in best if r is not None]
    coverage = len(seen) / SECTORS
    out = {"sectors": [None if r is None else round(min(r, 1.5), 3) for r in best],
           "coverage": round(coverage, 2), "complete": coverage >= MIN_COVERAGE,
           "weakest": None, "short": 0, "suggested_outer": None}
    if not seen:
        return out
    weakest = min(min(r, 1.0) for r in seen)
    out["weakest"] = round(100 * weakest, 1)
    out["short"] = sum(r < FULL_TILT for r in seen)
    if out["complete"]:
        # Full tilt at (100 - outer)% of travel: the weakest direction must still get there.
        need = 0 if weakest >= FULL_TILT else math.ceil(100 * (1 - weakest)) + REACH_MARGIN
        out["suggested_outer"] = min(gcm.OUTER_DEAD_ZONE_MAX, need)
        out["outer_too_small"] = need > gcm.OUTER_DEAD_ZONE_MAX
    return out


@asynccontextmanager
async def zeroed(nova, zones: Iterable[DeadZone]):
    """Set `zones` to 0 for the body, then put back what they were, even if the body fails.
    Yields the original values."""
    original = {z: await nova.dead_zone(z) for z in zones}
    changed = []
    try:
        for z, v in original.items():
            if v:
                await nova.set_dead_zone(z, 0)
                changed.append(z)
        yield original
    finally:
        errors = []
        for z in changed:
            try:
                await nova.set_dead_zone(z, original[z])
            except Exception as e:           # try every zone, then report
                errors.append(f"{z.name}: {e}")
        if errors:
            raise RuntimeError("couldn't put a dead zone back: " + "; ".join(errors))


async def sample(live, seconds: float, interval: float = SAMPLE_INTERVAL) -> dict[str, list[tuple[float, float]]]:
    """Positions of both sticks, `seconds` long, from a LiveInput (or anything with pump/state)."""
    out: dict[str, list[tuple[float, float]]] = {side: [] for side in STICKS}
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        pumped = live.pump()
        if asyncio.iscoroutine(pumped):          # the state-report reader (macOS) reads asynchronously
            await pumped
        st = live.state()
        for side, (ax, ay) in STICKS.items():
            out[side].append((st[ax], st[ay]))
        await asyncio.sleep(interval)
    return out


async def run(nova, live, test: str, seconds: float) -> dict:
    """One test on both sticks: "drift" (sticks left alone) or "reach" (turned around the edge)."""
    if test == "drift":
        zones, analyse = [INNER["left"], INNER["right"]], drift
    elif test == "reach":
        zones, analyse = [OUTER["left"], OUTER["right"]], reach
    else:
        raise ValueError("test must be drift or reach")
    async with zeroed(nova, zones) as original:
        samples = await sample(live, seconds)
    result = {side: analyse(points) for side, points in samples.items()}
    for side in STICKS:
        zone = (INNER if test == "drift" else OUTER)[side]
        result[side]["current"] = original[zone]
        result[side]["zone"] = zone.name
    return {"test": test, "seconds": seconds, **result}
