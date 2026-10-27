# SPDX-License-Identifier: AGPL-3.0-or-later
"""Adding a GameSir controller PadSquire doesn't know yet, on this computer.

1. Identify, read-only: each of its settings interfaces whose usage page belongs to a protocol
   family PadSquire speaks (devices.SPOKEN) gets that family's identity query, nothing else.
   A family counts only if it answers properly.
2. Keep it: an entry in added-controllers.json (devices.added_file), which devices.identify reads
   first. The app then connects to it with that family's pages.
3. Unlock its settings a group at a time: a test reads one value, writes a different one, reads
   it back, and puts the original back. Until a group's test passes, the server refuses writes
   to it. Groups without a test stay locked, and firmware is never offered for an added
   controller (hid.find, which update mode uses, doesn't list it).
4. Share: a report of the identity and the tests, for an issue, so it can join the built-in list.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, c2usb, devices, hid
from .client import NovaClient
from .gcm import DeadZone, Feature

# The groups of settings that can be unlocked, per family, and what the app calls them.
GROUPS = {
    "gcm": {"dead_zones": "Dead zones", "vibration": "Vibration", "quick_trigger": "Quick trigger",
            "light": "Home light", "features": "Stick and D-pad options"},
    "c2": {"vibration": "Vibration", "report_rate": "Report rate", "sticks": "Sticks", "triggers": "Triggers"},
}


class AddError(RuntimeError):
    pass


# -- identify ----------------------------------------------------------------------------

async def _identify_gcm(path: str) -> dict:
    async with hid.connect(path) as transport:
        info = await NovaClient(transport).info()
    return {"family": "gcm", "firmware": info.firmware_version, "protocol": f"GCM {info.gcm_version}",
            "details": asdict(info)}


async def _identify_c2(path: str) -> dict:
    def ask():
        t = c2usb.C2Transport.open(path)               # reads only
        try:
            t.heartbeat()
            fw = t.firmware_version()
            return {"family": "c2", "firmware": fw.controller, "protocol": "Cyclone 2 profiles",
                    "details": {"receiver": fw.receiver, "current_profile": t.current_profile()}}
        finally:
            t.close()
    return await asyncio.to_thread(ask)


IDENTIFY = {"gcm": _identify_gcm, "c2": _identify_c2}


async def identify(dev: dict, identify_with: dict | None = None) -> dict:
    """`dev`: an entry of devices.plugged_in(). Tries each family on its own interfaces only."""
    tried = []
    for iface in dev["interfaces"]:
        family = next((f for f, page in devices.SPOKEN.items() if page == iface["usage_page"]), None)
        if family is None:
            continue
        try:
            found = await (identify_with or IDENTIFY)[family](iface["path"])
        except Exception as e:                       # noqa: BLE001 - any failure means "not this family"
            tried.append(f"{family}: {e or type(e).__name__}")
            continue
        return {**found, "path": iface["path"], "usb_id": dev["usb_id"]}
    raise AddError("it didn't answer like any controller PadSquire knows"
                   + (f" ({'; '.join(tried)})" if tried else ": it has no settings channel PadSquire speaks"))


# -- keep ----------------------------------------------------------------------------------

def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data.get("controllers"), list) else {"controllers": []}
    except (OSError, ValueError):
        return {"controllers": []}


def _store(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    tmp.replace(path)


def save(found: dict, name: str, link: str = "cable", path: Path | None = None) -> dict:
    name = name.strip()
    if not name or len(name) > 60:
        raise AddError("give it a name of up to 60 characters")
    path = path or devices.added_file()
    pid = int(found["usb_id"].split(":")[1], 16)
    data = _load(path)
    data["controllers"] = [c for c in data["controllers"] if c.get("pid") != pid]
    entry = {"pid": pid, "name": name, "family": found["family"], "link": link,
             "firmware": found.get("firmware"), "protocol": found.get("protocol"),
             "added": datetime.now(UTC).isoformat(timespec="seconds"), "tested": {}}
    data["controllers"].append(entry)
    _store(path, data)
    return entry


def remove(pid: int, path: Path | None = None) -> None:
    path = path or devices.added_file()
    data = _load(path)
    data["controllers"] = [c for c in data["controllers"] if c.get("pid") != pid]
    _store(path, data)


def record_test(pid: int, group: str, result: str, path: Path | None = None) -> dict:
    """result: "works", or what went wrong."""
    path = path or devices.added_file()
    data = _load(path)
    for c in data["controllers"]:
        if c.get("pid") == pid:
            c.setdefault("tested", {})[group] = result
            _store(path, data)
            return c
    raise AddError("that controller isn't in the list of added ones any more")


def unlocked(entry: dict | None, group: str) -> bool:
    return entry is None or entry.get("tested", {}).get(group) == "works"


# -- test a group: change one value, read it back, put it back ------------------------------

async def _round_trip(read, write, other) -> None:
    before = await read()
    changed = other(before)
    await write(changed)
    if await read() != changed:
        await write(before)
        raise AddError("the controller didn't keep the change")
    await write(before)
    if await read() != before:
        raise AddError("the original value didn't come back; check this setting by hand")


async def test_gcm(nova: NovaClient, group: str) -> None:
    if group == "dead_zones":
        z = DeadZone.LEFT_STICK_INNER
        await _round_trip(lambda: nova.dead_zone(z), lambda v: nova.set_dead_zone(z, v),
                          lambda v: v + 1 if v < 20 else v - 1)
    elif group == "vibration":
        await _round_trip(nova.vibration, nova.set_vibration,
                          lambda v: replace(v, left_grip=50 if v.left_grip != 50 else 75))
    elif group == "quick_trigger":
        await _round_trip(nova.trigger_quick, lambda v: nova.set_trigger_quick(*v), lambda v: (not v[0], v[1]))
    elif group == "light":
        async def rgb():
            return (await nova.light()).rgb
        await _round_trip(rgb, nova.set_light, lambda v: (v[1], v[2], v[0]) if len(set(v)) > 1 else (255, 0, 0))
    elif group == "features":
        f = Feature.DPAD_DIAGONAL_LOCK
        await _round_trip(lambda: nova.feature(f), lambda v: nova.set_feature(f, v), lambda v: not v)
    else:
        raise AddError(f"no test for {group}")


C2_TESTS = {"vibration": "vibration.left", "report_rate": "report_rate",
            "sticks": "sticks.left.dead_zone", "triggers": "triggers.LT.dead_zone"}


async def test_c2(dev: c2usb.C2Client, group: str) -> None:
    if group not in C2_TESTS:
        raise AddError(f"no test for {group}")
    name = C2_TESTS[group]
    number = await dev.current_profile()

    async def read():
        p = await dev.profile(number)
        for part in name.split("."):
            p = p[part]
        return p

    def other(v):
        if name == "report_rate":
            return 500 if v != 500 else 1000
        if name.startswith("vibration"):
            return 50 if v != 50 else 75
        return {**v, "start": v["start"] + 1 if v["start"] < 20 else v["start"] - 1}

    async def write(v):
        await dev.set(name, {k: v[k] for k in ("start", "end", "anti_start", "anti_end")} if isinstance(v, dict) else v)

    await _round_trip(read, write, other)


# -- share --------------------------------------------------------------------------------

def report(entry: dict, dev: dict | None) -> dict:
    """What to attach to an issue: no serial numbers, no Bluetooth address."""
    return {
        "padsquire": __version__, "kind": "added-controller",
        "controller": {k: entry.get(k) for k in ("name", "family", "link", "firmware", "protocol", "tested")}
        | {"usb_id": f"3537:{entry['pid']:04x}"},
        "usb": {k: dev.get(k) for k in ("product", "interfaces")} if dev else None,
        "families": {f: devices.FAMILY_NAMES[f] for f in GROUPS},
    }
