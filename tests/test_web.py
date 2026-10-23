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
        r = await post("/api/connect", {"simulate": True})
        assert (await r.json())["data"]["mode"] == "simulated"
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
        r = await post("/api/vibration", {"left_grip": 4})
        assert (await r.json())["data"]["left_grip"] == 4
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
    assert run(go, tmp_path, simulate=True)["connected"] is True


def test_live_input_needs_the_cable(tmp_path):
    async def go(c):
        await login(c)
        await c.post("/api/connect", data=json.dumps({"simulate": True}), headers={**API, "Content-Type": "application/json"})
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


def test_setup_checks_explain_their_commands(tmp_path):
    async def go(c):
        await login(c)
        checks = (await (await c.get("/api/setup", headers=API)).json())["data"]
        assert {x["id"] for x in checks} >= {"udev", "sg", "7zip", "firmware", "bluetooth", "device"}
        for x in checks:
            if x["command"]:                         # every command says what it does
                assert x["explain"] and all(d["url"].startswith("https://") for d in x["docs"])
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
