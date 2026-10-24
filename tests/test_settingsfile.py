# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import copy
import json

import pytest

from padsquire import gcm, settingsfile
from padsquire.client import NovaClient
from padsquire.gcm import DeadZone, Feature, Key
from padsquire.simulator import SimulatedNova


def saved(sim: SimulatedNova) -> dict:
    async def go():
        s = await settingsfile.read(NovaClient(sim), remaps=True, mappings=True)
        return settingsfile.document(s, app_version="0.0.0", firmware=sim.firmware, now=0)
    return json.loads(json.dumps(asyncio.run(go())))          # through JSON, like a real file


def load(sim: SimulatedNova, doc: dict, skip=lambda c: None) -> dict:
    async def go():
        nova = NovaClient(sim)
        current = settingsfile.typed(await settingsfile.read(nova, remaps=True, mappings=True))
        return await settingsfile.apply(nova, settingsfile.changes(settingsfile.parse(doc), current), skip)
    return asyncio.run(go())


def customised() -> SimulatedNova:
    sim = SimulatedNova()
    sim.dead_zones[DeadZone.LEFT_STICK_INNER] = 9
    sim.dead_zones[DeadZone.RIGHT_STICK_OUTER] = 12
    sim.features[Feature.DPAD_DIAGONAL_LOCK] = True
    sim.vibration = gcm.Vibration(50, 0, False, False, 25, 0, False, False)
    sim.auto_sleep = 30
    sim.quick_trigger = (True, False)
    sim.light = (255, 40, 40)
    sim.remaps[Key.CAPTURE & 0xFF] = bytes([Key.A & 0xFF, Key.B & 0xFF])
    sim.mappings[Key.L4] = (gcm.scan_code(Key.X), 0)
    return sim


def test_round_trip_to_a_fresh_controller():
    doc = saved(customised())
    assert doc["format"] == "padsquire-settings" and doc["version"] == 1
    assert doc["controller"] == {"model": "Nova 2 Lite", "firmware": "8201"}
    s = doc["settings"]
    assert s["dead_zones"]["LEFT_STICK_INNER"] == 9 and s["features"]["DPAD_DIAGONAL_LOCK"] is True
    assert s["remaps"]["CAPTURE"] == ["A", "B"] and s["remaps"]["A"] == []
    assert s["mappings"]["L4"] == {"kind": "key", "target": "X"} and s["light"] == [255, 40, 40]

    fresh = SimulatedNova()
    result = load(fresh, doc)
    assert not result["failed"] and not result["skipped"]
    assert len(result["applied"]) == 9
    assert saved(fresh)["settings"] == s


def test_loading_the_same_settings_changes_nothing():
    sim = customised()
    doc = saved(sim)
    before = len(sim.log)
    result = load(sim, doc)
    assert result == {"applied": [], "skipped": [], "failed": []}
    assert not any(f[0] in (gcm.CMD_DEAD_ZONE, gcm.CMD_FEATURE) and len(f) > 3 and f[3] == 1 for f in sim.log[before:])


def test_changes_say_what_and_from_what():
    doc = saved(customised())

    async def go():
        nova = NovaClient(SimulatedNova())
        current = settingsfile.typed(await settingsfile.read(nova, remaps=True, mappings=True))
        return settingsfile.changes(settingsfile.parse(doc), current)
    todo = {c["name"]: c for c in asyncio.run(go())}
    assert todo["LEFT_STICK_INNER"]["label"] == "Left stick inner dead zone"
    assert (todo["LEFT_STICK_INNER"]["from"], todo["LEFT_STICK_INNER"]["to"]) == (5, 9)
    assert todo["CAPTURE"]["to"] == ["A", "B"] and todo["light"]["to"] == [255, 40, 40]


def test_a_skipped_change_is_reported_and_the_rest_written():
    fresh = SimulatedNova()
    result = load(fresh, saved(customised()), skip=lambda c: "not here" if c["section"] == "auto_sleep" else None)
    assert [s["name"] for s in result["skipped"]] == ["auto_sleep"] and result["skipped"][0]["why"] == "not here"
    assert fresh.auto_sleep == 10 and fresh.light == (255, 40, 40)


def test_a_failed_write_is_reported_and_the_rest_written():
    doc = saved(customised())
    doc["settings"]["remaps"]["A"] = ["B"]
    fresh = SimulatedNova()
    real = fresh.handle

    def refuse_remaps(f):                     # the controller answers a remap with no targets
        reply = real(f)
        if f[0] == gcm.CMD_REMAP and f[1] == 1:
            reply = gcm.seal(bytes([f[0], f[1], 9, f[3], 0, 0, 0, 0, 0]))
            fresh.remaps.pop(f[3], None)
        return reply
    fresh.handle = refuse_remaps
    result = load(fresh, doc)
    assert {f["name"] for f in result["failed"]} == {"A", "CAPTURE"}
    assert fresh.light == (255, 40, 40)


def test_a_macro_on_a_back_button_cant_be_written():
    doc = saved(SimulatedNova())
    doc["settings"]["mappings"]["R4"] = {"kind": "macro", "target": None}
    result = load(SimulatedNova(), doc)
    assert result["skipped"][0]["name"] == "R4" and "macro" in result["skipped"][0]["why"]


@pytest.mark.parametrize("change,fragment", [
    (lambda d: d.update(format="other"), "isn't a PadSquire settings file"),
    (lambda d: d.update(version=2), "version 2"),
    (lambda d: d.pop("settings"), "no settings"),
    (lambda d: d["settings"]["dead_zones"].update(LEFT_STICK_OUTER=30), "above 25%"),
    (lambda d: d["settings"]["dead_zones"].update(LEFT_STICK_INNER=300), "0-255"),
    (lambda d: d["settings"]["dead_zones"].update(NOPE=1), "unknown dead zone"),
    (lambda d: d["settings"]["features"].update(SWAP_STICKS=1), "true or false"),
    (lambda d: d["settings"]["vibration"].pop("left_grip"), "vibration needs"),
    (lambda d: d["settings"].update(light=[1, 2]), "light"),
    (lambda d: d["settings"]["remaps"].update(HOME=["A"]), "can't be remapped"),
    (lambda d: d["settings"]["remaps"].update(A=["A"]), "can't act as"),
    (lambda d: d["settings"]["remaps"].update(A=["LS_UP"]), "can't act as"),
    (lambda d: d["settings"]["remaps"].update(A=["B", "X", "Y", "LB"]), "up to 3"),
    (lambda d: d["settings"]["mappings"].update(L4={"kind": "key", "target": "HOME"}), "can't act as"),
    (lambda d: d["settings"]["mappings"].update(L4={"kind": "teleport"}), "unknown kind"),
])
def test_bad_files_are_refused_whole(change, fragment):
    doc = copy.deepcopy(saved(SimulatedNova()))
    change(doc)
    with pytest.raises(settingsfile.InvalidFile, match=fragment):
        settingsfile.parse(doc)


def test_not_even_an_object():
    with pytest.raises(settingsfile.InvalidFile):
        settingsfile.parse(["nope"])


def test_current_settings_beyond_file_limits_still_compare():
    # a controller set up elsewhere may hold an outer dead zone a file may not
    sim = SimulatedNova()
    sim.dead_zones[DeadZone.LEFT_STICK_OUTER] = 40
    result = load(sim, saved(SimulatedNova()))
    assert [c["name"] for c in result["applied"]] == ["LEFT_STICK_OUTER"]
    assert sim.dead_zones[DeadZone.LEFT_STICK_OUTER] == 0
