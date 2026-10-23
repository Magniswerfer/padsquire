import asyncio
import json

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from gamesir_linux.web.server import build_app  # noqa: E402

TOKEN = "test-token"
API = {"X-GameSir": "1"}


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
    from gamesir_linux import ble, hid

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
    run(go, tmp_path)
