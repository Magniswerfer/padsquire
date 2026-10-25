# SPDX-License-Identifier: AGPL-3.0-or-later
import asyncio
import hashlib
import json

import pytest

pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from test_jlboot import CHIP_KEY, LOADER, LOADER_BLOB, FakeUboot  # noqa: E402
from test_jlflash import _ufw, make_dump, make_image  # noqa: E402

from padsquire import jlboot, jlwrite, ufw, updatemode  # noqa: E402
from padsquire.web import update  # noqa: E402
from padsquire.web.server import build_app  # noqa: E402

TOKEN = "t"
API = {"X-PadSquire": "1", "Content-Type": "application/json"}
JEDEC_1M = 0x1440C8
OLD = make_dump(make_image(key=CHIP_KEY, app_len=0x5E000))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setitem(jlboot.LOADERS, "br23", LOADER)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "br23loader.bin").write_bytes(LOADER_BLOB)
    chip = FakeUboot(OLD, jedec=JEDEC_1M, buffer_size=0x8000)
    monkeypatch.setattr(updatemode, "open_device", lambda path=None: (
        type("D", (), {"close": lambda s: None})(), jlboot.JLBoot(chip.execute)))
    monkeypatch.setattr(jlboot, "find", lambda: [jlboot.UbootDevice("/dev/sg9", "BR23", "UBOOT1.00")])
    fw_dir = tmp_path / "fw"
    fw_dir.mkdir()
    data = _ufw(("flash.bin", 0x00, make_image(code_start=0, code_end=0x63000, vm=(0x63000, 0x16000), key=CHIP_KEY)),
                ("flash2.bin", 0x20, make_image(key=CHIP_KEY, app_len=0x5F000)))
    (fw_dir / "ctrl.ufw").write_bytes(data)
    parsed = ufw.parse(data)
    (fw_dir / "manifest.json").write_text(json.dumps({"source": "test", "gamesir_connect_version": "1.16.7",
        "model": "T4N2", "firmware": [dict(file="ctrl.ufw", sha256=hashlib.sha256(data).hexdigest(),
            chip=parsed.chip, jl_pid=parsed.jl_pid, jl_vid=parsed.jl_vid, identifier="T4N2International",
            target="controller", region="international", version=8201, minimum_version=5601)]}))
    return dict(chip=chip, fw_dir=fw_dir, backups=tmp_path / "backups", cache=cache)


def run(env, fn):
    async def go():
        app = build_app(TOKEN, env["fw_dir"], backup_dir=env["backups"], loader_cache=env["cache"],
                        usb_finder=lambda: [], scanner=lambda: [])
        async with TestClient(TestServer(app, host="127.0.0.1")) as c:
            await c.get(f"/?token={TOKEN}", allow_redirects=False)
            return await fn(c, app)
    return asyncio.run(go())


async def get(c):
    return (await (await c.get("/api/update", headers=API)).json())["data"]


async def post(c, path, body=None):
    r = await c.post(path, data=json.dumps(body or {}), headers=API)
    return r.status, await r.json()


async def finish(c):
    for _ in range(400):
        d = await get(c)
        if d["job"]["state"] != "running":
            return d
        await asyncio.sleep(0.02)
    raise AssertionError("job did not finish")


def test_backup_restore_and_install(env):
    chip = env["chip"]

    async def go(c, app):
        d = await get(c)
        assert d["mode"] == "download" and d["backups"] == [] and d["job"] is None

        assert (await post(c, "/api/update/backup"))[0] == 200
        d = await finish(c)
        assert d["job"]["state"] == "done", d["job"]
        assert d["identity"]["chip_key"] == f"{CHIP_KEY:#06x}"
        (b,) = d["backups"]
        assert any("both reads are identical" in line for line in d["job"]["log"])

        # writes need a confirmation and a backup known by name
        assert (await post(c, "/api/update/restore", {"backup": b["name"]}))[0] == 400
        assert (await post(c, "/api/update/restore", {"backup": "../../etc/passwd", "confirm": True}))[0] == 400
        chip.flash[0x20000:0x20004] = b"\0\0\0\0"
        assert (await post(c, "/api/update/restore", {"backup": b["name"], "confirm": True}))[0] == 200
        assert (await finish(c))["job"]["state"] == "done"
        assert bytes(chip.flash) == OLD

        # installing needs the version the controller ran before update mode
        status, body = await post(c, "/api/update/check", {"file": "ctrl.ufw", "backup": b["name"]})
        assert body["data"]["allowed"] is False and "unknown" in body["data"]["errors"][0]
        assert (await post(c, "/api/update/install", {"file": "ctrl.ufw", "backup": b["name"], "confirm": True}))[0] == 400
        app[update.KEY].installed_version = 7701
        status, body = await post(c, "/api/update/check", {"file": "ctrl.ufw", "backup": b["name"]})
        assert body["data"]["allowed"] is True and body["data"]["partition"] == "flash2.bin"
        assert (await post(c, "/api/update/install", {"file": "ctrl.ufw", "backup": b["name"], "confirm": True}))[0] == 200
        d = await finish(c)
        assert d["job"]["state"] == "done" and d["job"]["result"] == {"version": 8201}
        assert d["installed_version"] == 8201
        expected = jlwrite.connect_record(make_image(key=CHIP_KEY, app_len=0x5F000), bytes(6))
        assert bytes(chip.flash[0x1000:0x64000]) == expected and bytes(chip.flash[0x64000:]) == OLD[0x64000:]

        # the same version again: refused, but the page may offer it as a reinstall
        status, body = await post(c, "/api/update/check", {"file": "ctrl.ufw", "backup": b["name"]})
        assert body["data"]["allowed"] is False and body["data"]["reinstall"] is True
        assert "already installed" in body["data"]["errors"][0]
        assert (await post(c, "/api/update/install", {"file": "ctrl.ufw", "backup": b["name"], "confirm": True}))[0] == 200
        assert (await finish(c))["job"]["state"] == "failed"          # not without asking for it
        assert (await post(c, "/api/update/install", {"file": "ctrl.ufw", "backup": b["name"], "confirm": True,
                                                      "reinstall": True}))[0] == 200
        assert (await finish(c))["job"]["state"] == "done"
    run(env, go)


def test_a_failed_write_blocks_restart(env):
    chip = env["chip"]

    async def go(c, app):
        await post(c, "/api/update/backup")
        name = (await finish(c))["backups"][0]["name"]
        chip.corrupt_write_at = 0x30010
        orig = chip.execute

        def always_corrupt(cdb, out, n):                 # every attempt writes a bad byte
            if cdb[:2] == b"\xfb\x04":
                chip.corrupt_write_at = 0x30010
            return orig(cdb, out, n)
        chip.execute = always_corrupt
        await post(c, "/api/update/restore", {"backup": name, "confirm": True})
        d = await finish(c)
        assert d["job"]["state"] == "failed" and d["job"]["stay"] and d["stay"]
        assert "Do not restart" in d["job"]["error"]
        status, _ = await post(c, "/api/update/restart")
        assert status == 409
    run(env, go)


def test_one_job_at_a_time_and_mode(env, monkeypatch):
    async def go(c, app):
        app[update.KEY].start("backup", lambda job: asyncio.run(asyncio.sleep(0.3)))
        status, body = await post(c, "/api/update/backup")
        assert status == 409 and "still running" in body["error"]
        monkeypatch.setattr(jlboot, "find", lambda: [])
        assert (await get(c))["mode"] == "none"
    run(env, go)


def test_the_receiver_goes_through_the_same_steps(tmp_path, monkeypatch):
    from test_jlflash import make_receiver_dump, make_receiver_image
    receiver_loader = jlboot.Loader("bd19", 0x2000, "https://example.invalid/bd19loader.bin",
                                    hashlib.sha256(LOADER_BLOB).hexdigest(), ram_end=0x10000)
    monkeypatch.setitem(jlboot.LOADERS, "bd19", receiver_loader)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "bd19loader.bin").write_bytes(LOADER_BLOB)
    flash = make_receiver_dump(make_receiver_image(key=CHIP_KEY))
    chip = FakeUboot(flash, vendor="BD19", jedec=0x1240CD, buffer_size=0x8000)   # 256 KB
    real_execute = chip.execute

    def execute(cdb, out, n):                 # the fake checks the BR23 loader address on jump
        if cdb[:2] == b"\xfb\x08":
            assert int.from_bytes(cdb[2:6], "big") == 0x2000
            chip.loader = True
            return (cdb[:2]).ljust(16, b"\0")
        if cdb[:2] == b"\xfb\x06":
            chip.cdbs.append(cdb)
            return b""
        return real_execute(cdb, out, n)
    monkeypatch.setattr(updatemode, "open_device", lambda path=None: (
        type("D", (), {"close": lambda s: None})(), jlboot.JLBoot(execute)))
    monkeypatch.setattr(jlboot, "find", lambda: [jlboot.UbootDevice("/dev/sg9", "BD19", "UBOOT1.00")])
    fw_dir = tmp_path / "fw"
    fw_dir.mkdir()
    env = dict(chip=chip, fw_dir=fw_dir, backups=tmp_path / "backups", cache=cache)

    async def go(c, app):
        d = await get(c)
        assert d["mode"] == "download" and d["device"] == "receiver"
        await post(c, "/api/update/backup")
        d = await finish(c)
        assert d["job"]["state"] == "done", d["job"]
        (b,) = d["backups"]
        chip.flash[0x5000:0x5004] = b"\0\0\0\0"
        assert (await post(c, "/api/update/restore", {"backup": b["name"], "confirm": True}))[0] == 200
        d = await finish(c)
        assert d["job"]["state"] == "done", d["job"]
        assert bytes(chip.flash) == flash
    run(env, go)
