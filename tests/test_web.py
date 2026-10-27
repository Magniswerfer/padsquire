# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import json

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from padsquire.web.server import build_app  # noqa: E402

TOKEN = "test-token"
API = {"X-PadSquire": "1"}


def run(fn, fw_dir, **kw):
    kw.setdefault("usb_finder", lambda: [])   # never the real controller, if one is plugged in
    kw.setdefault("gamesir_finder", lambda: [])
    async def go():
        app = build_app(TOKEN, fw_dir, **kw)
        async with TestClient(TestServer(app, host="127.0.0.1")) as client:
            return await fn(client)
    return asyncio.run(go())


async def login(c):
    r = await c.get(f"/?token={TOKEN}", allow_redirects=False)
    assert r.status == 302
    return r


def test_security_gates(tmp_path):
    async def go(c):
        assert (await c.get("/")).status == 403                                  # no cookie
        assert (await c.get("/?token=wrong", allow_redirects=False)).status == 403
        assert (await c.get("/", headers={"Host": "evil.example"})).status == 403  # DNS rebinding
        await login(c)
        assert (await c.get("/")).status == 200
        assert (await c.get("/static/app.js")).status == 200
        r = await c.get("/api/status")                                           # missing header
        assert r.status == 403
        r = await c.get("/api/status", headers=API)
        assert r.status == 200 and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    run(go, tmp_path)


def test_cookie_is_strict(tmp_path):
    async def go(c):
        r = await login(c)
        cookie = r.headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    run(go, tmp_path)


def test_needs_a_controller(tmp_path):
    async def go(c):
        await login(c)
        r = await c.get("/api/state", headers=API)
        assert r.status == 409
    run(go, tmp_path)


def test_full_state_and_settings(tmp_path):
    async def go(c):
        await login(c)
        post = lambda path, body: c.post(path, data=json.dumps(body), headers={**API, "Content-Type": "application/json"})
        r = await post("/api/connect", {"simulate": True, "link": "bluetooth"})
        status = (await r.json())["data"]
        assert status["mode"] == "ble" and status["simulated"] is True
        state = (await (await c.get("/api/state", headers=API)).json())["data"]
        assert state["info"]["value"]["firmware_version"] == "8201"
        assert state["battery"]["value"] == {"raw": 0xCC, "level": 76, "charging": True}
        assert state["capabilities"]["stick_targets"] is True

        r = await post("/api/mapping", {"source": "L4", "target": "B"})
        assert (await r.json())["data"] == {"source": "L4", "kind": "key", "targets": ["B"]}
        r = await post("/api/dead-zone", {"zone": "LEFT_STICK_OUTER", "value": 12})
        assert (await r.json())["data"] == 12
        r = await post("/api/light", {"rgb": [1, 2, 3]})
        assert (await r.json())["data"]["rgb"] == [1, 2, 3]
        r = await post("/api/vibration", {"left_grip": 50})
        assert (await r.json())["data"]["left_grip"] == 50
        r = await post("/api/slot", {"slot": 2})
        assert (await r.json())["data"]["slots"]["value"] == [3, 2]
    run(go, tmp_path)


@pytest.mark.parametrize("path,body,fragment", [
    ("/api/dead-zone", {"zone": "LEFT_STICK_INNER", "value": 300}, "0-255"),
    ("/api/dead-zone", {"zone": "LEFT_STICK_INNER", "value": True}, "0-255"),
    ("/api/dead-zone", {"zone": "NOPE", "value": 1}, "NOPE"),
    ("/api/mapping", {"source": "A", "target": "B"}, "cannot be remapped"),
    ("/api/mapping", {"source": "L4", "target": "HOME"}, "not a valid target"),
    ("/api/light", {"rgb": [1, 2]}, "rgb"),
    ("/api/restore", {}, "confirmation"),
])
def test_rejects_bad_input(tmp_path, path, body, fragment):
    async def go(c):
        await login(c)
        await c.post("/api/connect", data=json.dumps({"simulate": True}), headers={**API, "Content-Type": "application/json"})
        r = await c.post(path, data=json.dumps(body), headers={**API, "Content-Type": "application/json"})
        data = await r.json()
        assert r.status in (400, 422) and data["ok"] is False and fragment in data["error"], data
    run(go, tmp_path)


def test_invalid_json(tmp_path):
    async def go(c):
        await login(c)
        r = await c.post("/api/connect", data="{nope", headers={**API, "Content-Type": "application/json"})
        assert r.status == 400
    run(go, tmp_path)


def test_scan_uses_injected_scanner(tmp_path):
    async def fake_scan():
        return [("AA:BB:CC:DD:EE:FF", "GameSir-Nova 2 Lite_1234")]

    async def go(c):
        await login(c)
        r = await c.post("/api/scan", data="{}", headers={**API, "Content-Type": "application/json"})
        assert (await r.json())["data"] == [
            {"address": "AA:BB:CC:DD:EE:FF", "name": "GameSir-Nova 2 Lite_1234", "link": "ble"}]
    run(go, tmp_path, scanner=fake_scan, usb_finder=lambda: [])


def test_usb_controllers_are_listed_and_only_found_paths_open(tmp_path):
    from padsquire import ble, hid

    async def no_bluetooth():
        raise ble.BleError("Bluetooth is unavailable")

    found = [hid.HidDevice("/dev/hidraw99", 0x100F, "2.4G XBOX 360 For Windows"),
             hid.HidDevice("/dev/hidraw98", 0x1098, "2.4G XBOX 360 For Windows")]

    async def go(c):
        await login(c)
        h = {**API, "Content-Type": "application/json"}
        r = await c.post("/api/scan", data="{}", headers=h)
        assert (await r.json())["data"] == [
            {"address": "/dev/hidraw99", "name": "GameSir Nova 2 Lite", "link": "usb", "via": "cable"},
            {"address": "/dev/hidraw98", "name": "GameSir Nova 2 Lite", "link": "usb", "via": "dongle"}]
        r = await c.post("/api/connect", data=json.dumps({"address": "/dev/sda", "link": "usb"}), headers=h)
        assert r.status == 400 and "no longer connected" in (await r.json())["error"]
    run(go, tmp_path, scanner=no_bluetooth, usb_finder=lambda: found)


def test_firmware_status_against_device(fw_dir):
    async def go(c):
        await login(c)
        await c.post("/api/connect", data=json.dumps({"simulate": True}), headers={**API, "Content-Type": "application/json"})
        data = (await (await c.get("/api/firmware", headers=API)).json())["data"]
        assert data["device_version"] == "8201"
        by_region = {f["region"]: f for f in data["available"] if f["target"] == "controller"}
        assert by_region["international"]["matches_device_region"] is True
        assert by_region["china"]["matches_device_region"] is False
        assert by_region["international"]["newer_than_device"] is False
    run(go, fw_dir)


def test_simulate_flag_connects_on_start(tmp_path):
    async def go(c):
        await login(c)
        return (await (await c.get("/api/status", headers=API)).json())["data"]
    status = run(go, tmp_path, simulate="receiver")
    assert status["connected"] is True and status["mode"] == "usb" and status["via"] == "dongle" and status["simulated"]


def test_live_input_needs_the_cable(tmp_path):
    async def go(c):
        await login(c)
        await c.post("/api/connect", data=json.dumps({"simulate": True, "link": "bluetooth"}), headers={**API, "Content-Type": "application/json"})
        r = await c.get("/api/input", headers=API)
        assert r.status == 409 and "by cable" in (await r.json())["error"]
    run(go, tmp_path)


def test_remap_every_key(tmp_path):
    async def go(c):
        await login(c)
        post = lambda path, body: c.post(path, data=json.dumps(body), headers={**API, "Content-Type": "application/json"})
        await post("/api/connect", {"simulate": True})
        state = (await (await c.get("/api/state", headers=API)).json())["data"]
        assert len(state["remaps"]) == 19 and state["remaps"]["CAPTURE"] == {"value": []}
        r = await post("/api/remap", {"key": "CAPTURE", "targets": ["B"]})
        assert (await r.json())["data"] == ["B"]
        r = await post("/api/remap", {"key": "CAPTURE", "targets": []})
        assert (await r.json())["data"] == []
        r = await post("/api/remap", {"key": "HOME", "targets": ["B"]})
        assert r.status == 400
        r = await post("/api/remap", {"key": "A", "targets": ["LS_UP"]})
        assert r.status == 400
        r = await post("/api/remap", {"key": "CAPTURE", "targets": ["A", "B", "X"]})   # pressed together
        assert sorted((await r.json())["data"]) == ["A", "B", "X"]
        for bad in (["A", "A"], ["CAPTURE"], ["A", "B", "X", "Y"]):
            assert (await post("/api/remap", {"key": "CAPTURE", "targets": bad})).status == 400
        meta = (await (await c.get("/api/meta", headers=API)).json())["data"]
        assert meta["remap_max_targets"] == 3
    run(go, tmp_path)


@pytest.mark.parametrize("macos, ids", [
    (False, {"udev", "sg", "play", "7zip", "firmware", "bluetooth", "device"}),
    (True, {"hidapi", "play", "updates", "7zip", "firmware", "bluetooth", "device"}),
])
def test_setup_checks_explain_their_commands(tmp_path, monkeypatch, macos, ids):
    from padsquire import hid
    monkeypatch.setattr(hid, "MACOS", macos)

    async def go(c):
        await login(c)
        checks = (await (await c.get("/api/setup", headers=API)).json())["data"]
        assert {x["id"] for x in checks} >= ids
        for x in checks:
            if x["command"]:                         # every command says what it does
                assert x["explain"] and all(d["url"].startswith("https://") for d in x["docs"])
        meta = (await (await c.get("/api/meta", headers=API)).json())["data"]
        assert meta["platform"] == ("macos" if macos else "linux")
    run(go, tmp_path)




def test_devices_lists_what_is_plugged_in(tmp_path):
    from padsquire import hid
    plugged = [hid.HidDevice("/dev/hidraw98", 0x1098, "2.4G XBOX 360 For Windows")]

    async def go(c):
        await login(c)
        d = (await (await c.get("/api/devices", headers=API)).json())["data"]
        assert d["usb"] == [{"address": "/dev/hidraw98", "name": "GameSir Nova 2 Lite", "link": "usb",
                             "via": "dongle"}] and d["lost"] is False
        plugged.clear()
        d = (await (await c.get("/api/devices", headers=API)).json())["data"]
        assert d["usb"] == [] and d["lost"] is False         # nothing was connected, nothing lost
    run(go, tmp_path, usb_finder=lambda: plugged)


def test_devices_lists_other_gamesir_devices(tmp_path):
    from padsquire import hid
    nova = {"usb_id": "3537:100f", "name": "Nova 2 Lite", "support": "settings",
            "interfaces": [{"path": "/dev/hidraw2", "usage_page": 0xFF7A}]}
    cyclone = {"usb_id": "3537:100b", "name": "Cyclone 2", "support": "read",
               "interfaces": [{"path": "/dev/hidraw5", "usage_page": 0xFFF0}]}

    async def go(c):
        await login(c)
        d = (await (await c.get("/api/devices", headers=API)).json())["data"]
        assert [x["address"] for x in d["usb"]] == ["/dev/hidraw2"]
        assert d["gamesir"] == [cyclone]            # the Nova 2 Lite is connectable, so not repeated
    run(go, tmp_path, usb_finder=lambda: [hid.HidDevice("/dev/hidraw2", 0x100F, "x")],
        gamesir_finder=lambda: [nova, cyclone])


def test_app_links_its_source_code():
    from importlib.resources import files
    page = (files("padsquire.web") / "static" / "index.html").read_text()
    assert 'href="https://github.com/Magniswerfer/padsquire"' in page and "Source code" in page


def test_presence_stops_after_the_last_page_but_never_mid_update():
    from padsquire.web.server import Presence
    now = [0.0]
    p = Presence(grace=15, idle=600, clock=lambda: now[0])
    p.seen(); p.bye()
    now[0] = 10
    assert not p.gone(busy=False)              # a reload comes back within the grace
    p.seen(); now[0] = 30
    assert not p.gone(busy=False)              # the page came back: no stop
    p.bye(); now[0] = 50
    assert p.gone(busy=False)
    assert not p.gone(busy=True)               # never while a firmware step runs
    now[0] = 2000; p.bye_at = None
    assert p.gone(busy=False)                  # no page for 10 minutes


def test_bye_needs_the_cookie_but_no_header(tmp_path):
    from padsquire.web.server import PRESENCE

    async def go(c):
        r = await c.post("/api/bye")
        assert r.status == 403                     # no cookie: another site can't stop it
        await login(c)
        r = await c.post("/api/bye")
        assert r.status == 204
        assert c.server.app[PRESENCE].bye_at is not None
        await c.get("/api/status", headers=API)
        assert c.server.app[PRESENCE].bye_at is None
    run(go, tmp_path)


def test_one_go_route_checks_its_input(tmp_path):
    async def go(c):
        await login(c)
        r = await c.post("/api/update/run", json={"action": "erase"}, headers=API)
        assert r.status == 400 and "action" in (await r.json())["error"]
        r = await c.post("/api/update/run", json={"action": "install"}, headers=API)
        assert r.status == 400 and "confirmation" in (await r.json())["error"]
        r = await c.post("/api/update/run", json={"action": "backup"}, headers=API)
        assert r.status == 400 and "by cable" in (await r.json())["error"]     # nothing plugged in
    run(go, tmp_path, usb_finder=lambda: [])
def test_settings_file_export_preview_and_load(tmp_path):
    async def go(c):
        await login(c)
        post = lambda path, body: c.post(path, data=json.dumps(body), headers={**API, "Content-Type": "application/json"})
        await post("/api/connect", {"simulate": True})
        await post("/api/dead-zone", {"zone": "LEFT_STICK_INNER", "value": 9})
        await post("/api/remap", {"key": "CAPTURE", "targets": ["B"]})
        doc = (await (await c.get("/api/settings/export", headers=API)).json())["data"]
        assert doc["format"] == "padsquire-settings" and doc["controller"]["firmware"] == "8201"
        assert doc["settings"]["remaps"]["CAPTURE"] == ["B"]

        await post("/api/restore", {"confirm": True})
        preview = (await (await post("/api/settings/import", {"file": doc})).json())["data"]
        names = {ch["name"]: ch for ch in preview["changes"]}
        assert set(names) == {"LEFT_STICK_INNER", "CAPTURE"} and names["LEFT_STICK_INNER"]["from"] == 5
        assert "write" not in names["CAPTURE"] and names["CAPTURE"]["why_not"] is None
        state = (await (await c.get("/api/state", headers=API)).json())["data"]
        assert state["dead_zones"]["LEFT_STICK_INNER"] == {"value": 5}               # a preview writes nothing

        done = (await (await post("/api/settings/import", {"file": doc, "apply": True})).json())["data"]
        assert len(done["applied"]) == 2 and not done["failed"]
        assert done["state"]["dead_zones"]["LEFT_STICK_INNER"] == {"value": 9}
        assert done["state"]["remaps"]["CAPTURE"] == {"value": ["B"]}

        r = await post("/api/settings/import", {"file": {"format": "nope"}, "apply": True})
        assert r.status == 400 and "isn't a PadSquire settings file" in (await r.json())["error"]
    run(go, tmp_path)


class _StillSticks:
    def __init__(self, session):
        self.closed = False

    def pump(self):
        pass

    def state(self):
        return {"lx": 0.03, "ly": 0.0, "rx": 0.0, "ry": 0.0, "lt": 0.0, "rt": 0.0, "pressed": []}

    def close(self):
        self.closed = True


def test_stick_check(tmp_path):
    async def go(c):
        await login(c)
        post = lambda path, body: c.post(path, data=json.dumps(body), headers={**API, "Content-Type": "application/json"})
        await post("/api/connect", {"simulate": True})
        r = await post("/api/stick-check", {"test": "drift", "seconds": 0.05})
        data = (await r.json())["data"]
        assert data["left"]["peak"] == 3.0 and data["left"]["suggested_inner"] == 5 and data["left"]["current"] == 5
        state = (await (await c.get("/api/state", headers=API)).json())["data"]
        assert state["dead_zones"]["LEFT_STICK_INNER"] == {"value": 5}                # put back
        r = await post("/api/stick-check", {"test": "reach", "seconds": 0.05})
        assert (await r.json())["data"]["left"]["complete"] is False                   # never turned
        for bad in ({"test": "drift", "seconds": 60}, {"test": "drift", "seconds": True}):
            assert (await post("/api/stick-check", bad)).status == 400
        assert (await post("/api/stick-check", {"test": "spin", "seconds": 0.05})).status == 422
    run(go, tmp_path, live_open=_StillSticks)


def test_stick_check_needs_live_input(tmp_path):
    async def go(c):
        await login(c)
        post = lambda path, body: c.post(path, data=json.dumps(body), headers={**API, "Content-Type": "application/json"})
        await post("/api/connect", {"simulate": True, "link": "bluetooth"})
        r = await post("/api/stick-check", {"test": "drift", "seconds": 1})
        assert r.status == 409 and "by cable" in (await r.json())["error"]
    run(go, tmp_path)


def test_simulated_cable_behaves_like_the_real_one(tmp_path):
    async def go(c):
        await login(c)
        post = lambda path, body: c.post(path, data=json.dumps(body), headers={**API, "Content-Type": "application/json"})
        await post("/api/connect", {"simulate": True, "link": "cable"})
        state = (await (await c.get("/api/state", headers=API)).json())["data"]
        assert "value" in state["remaps"]["A"]                                    # every key remaps
        assert state["battery"]["value"]["level"] == 82
        r = await post("/api/feature", {"feature": "SWAP_STICKS", "enabled": True})  # refused by USB
        assert r.status == 422 and "controller reports False" in (await r.json())["error"]
        assert (await post("/api/feature", {"feature": "DPAD_DIAGONAL_LOCK", "enabled": True})).status == 200
        r = await post("/api/vibration", {"left_grip": 60})                        # stored as 50
        assert r.status == 422 and "left_grip=50" in (await r.json())["error"]
        assert (await post("/api/vibration/test", {"motor": "left"})).status == 200
        r = await c.get("/api/input", headers=API)                                # simulated sticks
        line = json.loads(await r.content.readline())
        assert line["lx"] == 0 and line["pressed"] == []                          # inside the 5% dead zone
        r.close()
        assert (await post("/api/update/reboot", {"confirm": True})).status == 409
        devices = (await (await c.get("/api/devices", headers=API)).json())["data"]
        assert devices["lost"] is False and devices["status"]["connected"]          # not on USB, still kept
    run(go, tmp_path, usb_finder=lambda: [])


def test_simulated_receiver_reports_both_firmware_versions(tmp_path):
    async def go(c):
        await login(c)
        await c.post("/api/connect", data=json.dumps({"simulate": True, "link": "receiver"}),
                     headers={**API, "Content-Type": "application/json"})
        state = (await (await c.get("/api/state", headers=API)).json())["data"]
        assert state["info"]["value"]["firmware_version"] == "2800"                # the receiver's own
        assert state["controller_firmware"] == {"value": "8201"}                  # from a state report
        assert "error" in state["auto_sleep"] and "error" in state["slots"]
    run(go, tmp_path)


def test_simulator_link_is_checked(tmp_path):
    async def go(c):
        await login(c)
        r = await c.post("/api/connect", data=json.dumps({"simulate": True, "link": "carrier pigeon"}),
                         headers={**API, "Content-Type": "application/json"})
        assert r.status == 400 and "link must be" in (await r.json())["error"]
    run(go, tmp_path)


def test_profiles(tmp_path):
    async def go(c):
        await login(c)
        post = lambda path, body: c.post(path, data=json.dumps(body), headers={**API, "Content-Type": "application/json"})
        get = lambda path: c.get(path, headers=API)
        data = lambda r: r.json()
        assert (await (await get("/api/profiles")).json())["data"]["profiles"] == []
        await post("/api/connect", {"simulate": True, "link": "cable"})

        a = (await (await post("/api/profiles", {"name": "Everyday"})).json())["data"]["id"]
        await post("/api/light", {"rgb": [255, 0, 0]})
        await post("/api/remap", {"key": "A", "targets": ["B"]})
        b = (await (await post("/api/profiles", {"name": "Red"})).json())["data"]["id"]
        listed = {p["name"]: p for p in (await data(await get("/api/profiles")))["data"]["profiles"]}
        assert listed["Red"]["active"] and not listed["Everyday"]["active"] and listed["Everyday"]["differs"] == 2

        r = (await data(await post("/api/profiles/switch", {"id": a})))["data"]
        assert r["name"] == "Everyday" and len(r["applied"]) == 2 and not r["failed"]
        assert r["state"]["light"]["value"]["rgb"] == [255, 255, 255] and r["state"]["remaps"]["A"] == {"value": []}
        listed = {p["name"]: p for p in (await data(await get("/api/profiles")))["data"]["profiles"]}
        assert listed["Everyday"]["active"] and listed["Red"]["differs"] == 2

        # keep a change in the active profile, rename, export, import, delete
        await post("/api/dead-zone", {"zone": "LEFT_STICK_INNER", "value": 8})
        assert (await post("/api/profiles/update", {"id": a})).status == 200
        assert (await post("/api/profiles/rename", {"id": a, "name": "Daily"})).status == 200
        exported = (await data(await get(f"/api/profiles/file?id={a}")))["data"]
        assert exported["name"] == "Daily" and exported["settings"]["dead_zones"]["LEFT_STICK_INNER"] == 8
        r = await post("/api/profiles", {"name": "Copy", "file": exported})
        assert r.status == 200
        assert (await post("/api/profiles", {"name": "Red"})).status == 422            # taken
        assert (await post("/api/profiles", {"name": "Bad", "file": {"format": "x"}})).status == 400
        assert (await post("/api/profiles/delete", {"id": b})).status == 200
        names = [p["name"] for p in (await data(await get("/api/profiles")))["data"]["profiles"]]
        assert names == ["Copy", "Daily"]
        assert (await post("/api/profiles/switch", {"id": "../../etc/passwd"})).status == 422
    run(go, tmp_path, profiles_dir=tmp_path / "profiles")


def test_profile_switch_skips_what_the_connection_cant_change(tmp_path):
    async def go(c):
        await login(c)
        post = lambda path, body: c.post(path, data=json.dumps(body), headers={**API, "Content-Type": "application/json"})
        await post("/api/connect", {"simulate": True, "link": "bluetooth"})
        await post("/api/feature", {"feature": "SWAP_STICKS", "enabled": True})        # allowed over Bluetooth
        pid = (await (await post("/api/profiles", {"name": "Swapped"})).json())["data"]["id"]
        await post("/api/connect", {"simulate": True, "link": "cable"})               # a fresh controller
        r = (await (await post("/api/profiles/switch", {"id": pid})).json())["data"]
        # the toggle the cable refuses, and what only Bluetooth has: sleep time, the back buttons' own command
        assert sorted(s["name"] for s in r["skipped"]) == ["L4", "R4", "SWAP_STICKS", "auto_sleep"] and not r["failed"]
        listed = (await (await c.get("/api/profiles", headers=API)).json())["data"]["profiles"]
        assert listed[0]["active"]                         # nothing left that this connection can change
    run(go, tmp_path, profiles_dir=tmp_path / "profiles")
