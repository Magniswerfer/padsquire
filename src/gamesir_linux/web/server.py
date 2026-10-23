"""Local web UI: an aiohttp server on 127.0.0.1 that the browser talks to.

Security model: the server binds to loopback only, rejects requests whose Host
header isn't its own loopback address (DNS rebinding), and requires a session
cookie (set once from the token in the launch URL, SameSite=Strict) plus a
custom header on every API call (forcing a CORS preflight, which is never
granted). Other websites open in the same browser therefore can't drive it.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path

from aiohttp import web

from .. import __version__, ble, gcm, hid, installer, liveinput, updater
from ..client import NotApplied, NovaClient
from ..gcm import DeadZone, Feature, Key, ProtocolError, Vibration
from ..simulator import SimulatedNova

STATIC = Path(__file__).parent / "static"
COOKIE = "gamesir_session"
API_HEADER = "X-GameSir"

ERRORS = (NotApplied, ProtocolError, ble.BleError, hid.HidError, ValueError, KeyError,
          asyncio.TimeoutError)

# Reads that get no reply over the USB cable (firmware 7701). Asking costs 11 x 200 ms each,
# so the state read skips them and the GUI shows them as unavailable.
USB_UNSUPPORTED = {"auto_sleep", "slots"}


def _key_name(code: int):
    try:
        return Key(code).name
    except ValueError:
        return code


def _json(obj):
    if isinstance(obj, gcm.Battery):
        return {"raw": obj.raw, "level": obj.level, "charging": obj.charging}
    if isinstance(obj, gcm.Mapping):
        return {"source": _key_name(obj.source), "kind": obj.kind,
                "targets": [_key_name(t) for t in obj.targets]}
    if isinstance(obj, gcm.ButtonLight):
        return {"key": _key_name(obj.key), "rgb": list(obj.rgb), "effect": obj.effect, "rate": obj.rate}
    if is_dataclass(obj):
        return {k: _json(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Enum):
        return obj.name
    if isinstance(obj, dict):
        return {str(_json(k)): _json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json(v) for v in obj]
    return obj


def ok(data=None, **extra) -> web.Response:
    return web.json_response({"ok": True, "data": _json(data), **extra})


def fail(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


KEY_LABELS = {
    Key.A: "A", Key.B: "B", Key.X: "X", Key.Y: "Y", Key.UP: "D-pad up", Key.DOWN: "D-pad down",
    Key.LEFT: "D-pad left", Key.RIGHT: "D-pad right", Key.LB: "LB", Key.RB: "RB", Key.LT: "LT",
    Key.RT: "RT", Key.L3: "Left stick click", Key.R3: "Right stick click", Key.SELECT: "View",
    Key.START: "Menu", Key.HOME: "Home", Key.L4: "Left back button", Key.R4: "Right back button",
    Key.CAPTURE: "Capture",
}
for _k in gcm.NOVA_STICK_TARGETS:
    _name = Key(_k).name  # e.g. LS_UP_LEFT
    _stick = "Left stick" if _name.startswith("LS") else "Right stick"
    KEY_LABELS[Key(_k)] = f"{_stick} {_name[3:].replace('_', '-').lower()}"


class Session:
    """The one controller this server talks to."""

    def __init__(self):
        self.client: NovaClient | None = None
        self.mode: str | None = None          # "ble" | "usb" | "simulated"
        self.address: str | None = None
        self.name: str | None = None
        self._ctx = None
        self.lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.client is not None

    async def connect_simulated(self):
        await self.disconnect()
        self.client, self.mode, self.address, self.name = (
            NovaClient(SimulatedNova()), "simulated", "simulator", "GameSir-Nova 2 Lite (simulated)")

    async def connect_ble(self, address: str, name: str | None = None):
        await self.disconnect()
        await self._open(ble.connect(address), "ble", address, name)

    async def connect_usb(self, path: str, name: str | None = None):
        await self.disconnect()
        await self._open(hid.connect(path), "usb", path, name)

    async def _open(self, ctx, mode: str, address: str, name: str | None):
        transport = await ctx.__aenter__()
        self._ctx = ctx
        self.client, self.mode, self.address, self.name = NovaClient(transport), mode, address, name

    async def disconnect(self):
        if self._ctx is not None:
            try:
                await self._ctx.__aexit__(None, None, None)
            except Exception:
                pass
        self._ctx = None
        self.client = self.mode = self.address = self.name = None

    def status(self) -> dict:
        return {"connected": self.connected, "mode": self.mode, "address": self.address, "name": self.name}


async def _try(coro):
    """Run one read; a failure marks just that feature unavailable."""
    try:
        return {"value": _json(await coro)}
    except ERRORS as e:
        return {"error": str(e)}


async def read_state(nova: NovaClient, skip: set[str] = frozenset()) -> dict:
    async def maybe(name, read):
        return {"error": "not available over this connection"} if name in skip else await _try(read())

    info = await _try(nova.info())
    state = {
        "info": info,
        "battery": await _try(nova.battery()),
        "dead_zones": {z.name: await _try(nova.dead_zone(z)) for z in DeadZone},
        "features": {f.name: await _try(nova.feature(f)) for f in Feature},
        "vibration": await _try(nova.vibration()),
        "auto_sleep": await maybe("auto_sleep", nova.auto_sleep),
        "mappings": {k.name: await _try(nova.mapping(k)) for k in gcm.NOVA_REMAPPABLE},
        "trigger_quick": await _try(nova.trigger_quick()),
        "slots": await maybe("slots", nova.slots),
        "light": await _try(nova.light()),
    }
    fw = info.get("value", {}).get("firmware_version") if "value" in info else None
    state["capabilities"] = {
        "stick_targets": bool(fw and fw.isdigit() and int(fw) >= gcm.STICK_TARGETS_MIN_FIRMWARE),
    }
    return state


def _firmware_status(fw_dir: Path, device_version: str | None) -> dict:
    out = {"dir": str(fw_dir), "extracted": None, "available": [], "device_version": device_version}
    try:
        bundled = installer.load_manifest(fw_dir)
        out["extracted"] = updater.installed_version(fw_dir)
    except (OSError, KeyError, ValueError):
        return out
    from ..safety import region_of
    region = region_of(int(device_version)) if device_version and device_version.isdigit() else None
    for f in bundled:
        newer = None
        if f.target == "controller" and device_version and device_version.isdigit():
            newer = f.version > int(device_version)
        out["available"].append({
            "file": f.file, "target": f.target, "region": f.region, "version": f.version,
            "matches_device_region": (f.region == region) if f.target == "controller" and region else None,
            "newer_than_device": newer,
        })
    return out


def build_app(token: str, fw_dir: Path, *, feed_url: str = updater.FEED_URL,
              simulate: bool = False, scanner=None, usb_finder=None,
              backup_dir: Path | None = None, loader_cache: Path | None = None) -> web.Application:
    session = Session()
    scan = scanner or ble.scan
    find_usb = usb_finder or hid.find

    @web.middleware
    async def guard(request: web.Request, handler):
        host = request.host.split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            return fail("bad host", 403)
        if request.path == "/" and request.query.get("token") is not None:
            if not secrets.compare_digest(request.query["token"], token):
                return fail("bad token", 403)
            resp = web.HTTPFound("/")
            resp.set_cookie(COOKIE, token, httponly=True, samesite="Strict")
            raise resp
        if not secrets.compare_digest(request.cookies.get(COOKIE, ""), token):
            return web.Response(status=403, text="Open the link printed by `gamesir-linux gui`.")
        if request.path.startswith("/api/") and request.headers.get(API_HEADER) != "1":
            return fail("missing header", 403)
        resp = await handler(request)
        if resp.prepared:      # a stream sets its own headers before it starts
            return resp
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'")
        return resp

    app = web.Application(middlewares=[guard])
    routes = web.RouteTableDef()

    async def state_of(nova: NovaClient) -> dict:
        return await read_state(nova, USB_UNSUPPORTED if session.mode == "usb" else frozenset())

    def need_client():
        if not session.connected:
            raise web.HTTPConflict(text=json.dumps({"ok": False, "error": "no controller connected"}),
                                   content_type="application/json")
        return session.client

    async def body(request) -> dict:
        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error": "invalid JSON"}),
                                     content_type="application/json")
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error": "expected an object"}),
                                     content_type="application/json")
        return data

    def guarded(fn):
        async def wrapper(request):
            try:
                async with session.lock:
                    return await fn(request)
            except ERRORS as e:
                return fail(str(e) or type(e).__name__, 422)
        return wrapper

    # -- static ---------------------------------------------------------------
    @routes.get("/")
    async def index(_):
        return web.FileResponse(STATIC / "index.html")

    # -- meta / connection -----------------------------------------------------
    @routes.get("/api/meta")
    async def meta(_):
        return ok({
            "version": __version__,
            "keys": {k.name: label for k, label in KEY_LABELS.items()},
            "targets": [k.name for k in gcm.NOVA_TARGETS],
            "stick_targets": [Key(k).name for k in gcm.NOVA_STICK_TARGETS],
            "remappable": [k.name for k in gcm.NOVA_REMAPPABLE],
        })

    @routes.get("/api/status")
    async def status(_):
        return ok(session.status())

    @routes.post("/api/scan")
    @guarded
    async def do_scan(_):
        usb = [{"address": d.path, "name": d.name or "GameSir Nova 2 Lite", "link": "usb"} for d in find_usb()]
        try:
            found = await scan()
        except ble.BleError:
            if not usb:
                raise
            found = []     # Bluetooth is off or missing; the cable still works
        return ok(usb + [{"address": a, "name": n, "link": "ble"} for a, n in found])

    @routes.post("/api/connect")
    @guarded
    async def connect(request):
        data = await body(request)
        if data.get("simulate"):
            await session.connect_simulated()
        else:
            address = data.get("address")
            if not isinstance(address, str) or not address:
                return fail("address required")
            if data.get("link") == "usb":
                # Only open device nodes we found ourselves, never a path from the request.
                match = next((d for d in find_usb() if d.path == address), None)
                if match is None:
                    return fail("that USB controller is no longer connected; search again")
                await session.connect_usb(match.path, data.get("name") or match.name)
            else:
                await session.connect_ble(address, data.get("name"))
        return ok(session.status())

    @routes.get("/api/input")
    async def input_stream(request):
        """Live stick and trigger positions as JSON lines, about 30 a second, until the page
        stops reading. Not `guarded`: it must not hold the session lock while it runs."""
        if session.mode != "usb":
            return fail("live input is only available by cable", 409)
        try:
            live = liveinput.LiveInput(liveinput.event_node_for(session.address))
        except liveinput.Unavailable as e:
            return fail(str(e), 409)
        resp = web.StreamResponse(headers={
            "Content-Type": "application/x-ndjson", "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff"})
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()
        loop.add_reader(live.fd, ready.set)
        try:
            await resp.prepare(request)
            while True:
                await resp.write(json.dumps(live.state()).encode() + b"\n")
                try:
                    await asyncio.wait_for(ready.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass       # nothing moved; send the same state as a keepalive
                ready.clear()
                live.pump()
                await asyncio.sleep(1 / 30)
        except (ConnectionResetError, liveinput.Unavailable):
            pass
        finally:
            loop.remove_reader(live.fd)
            live.close()
        return resp

    @routes.post("/api/disconnect")
    @guarded
    async def disconnect(_):
        await session.disconnect()
        return ok(session.status())

    @routes.get("/api/state")
    @guarded
    async def state(_):
        return ok(await state_of(need_client()))

    # -- settings ---------------------------------------------------------------
    def _byte(v, name):
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 255:
            raise ValueError(f"{name} must be an integer 0-255")
        return v

    @routes.post("/api/dead-zone")
    @guarded
    async def set_dead_zone(request):
        d = await body(request)
        zone = DeadZone[d["zone"]]
        nova = need_client()
        await nova.set_dead_zone(zone, _byte(d.get("value"), "value"))
        return ok(await nova.dead_zone(zone))

    @routes.post("/api/feature")
    @guarded
    async def set_feature(request):
        d = await body(request)
        feature = Feature[d["feature"]]
        nova = need_client()
        await nova.set_feature(feature, bool(d.get("enabled")))
        return ok(await nova.feature(feature))

    @routes.post("/api/vibration")
    @guarded
    async def set_vibration(request):
        d = await body(request)
        nova = need_client()
        current = await nova.vibration()
        fields = {k: v for k, v in d.items() if k in Vibration.__dataclass_fields__}
        for k, v in fields.items():
            if k.endswith(("_grip", "_trigger")):
                _byte(v, k)
        new = Vibration(**{**asdict(current), **fields})
        await nova.set_vibration(new)
        return ok(await nova.vibration())

    @routes.post("/api/auto-sleep")
    @guarded
    async def set_sleep(request):
        d = await body(request)
        nova = need_client()
        await nova.set_auto_sleep(_byte(d.get("value"), "value"))
        return ok(await nova.auto_sleep())

    @routes.post("/api/mapping")
    @guarded
    async def set_mapping(request):
        d = await body(request)
        source = Key[d["source"]]
        if source not in gcm.NOVA_REMAPPABLE:
            return fail(f"{source.name} cannot be remapped over Bluetooth")
        nova = need_client()
        if d.get("reset"):
            return ok(await nova.reset_mapping(source))
        target = d.get("target")
        if target is not None:
            target = Key[target]
            allowed = set(gcm.NOVA_TARGETS) | set(gcm.NOVA_STICK_TARGETS)
            if target not in allowed:
                return fail(f"{target.name} is not a valid target")
        await nova.set_mapping(source, target)
        return ok(await nova.mapping(source))

    @routes.post("/api/trigger-quick")
    @guarded
    async def set_quick(request):
        d = await body(request)
        nova = need_client()
        await nova.set_trigger_quick(bool(d.get("left")), bool(d.get("right")))
        return ok(await nova.trigger_quick())

    @routes.post("/api/slot")
    @guarded
    async def set_slot(request):
        d = await body(request)
        nova = need_client()
        await nova.switch_slot(_byte(d.get("slot"), "slot"))
        return ok(await state_of(nova))

    @routes.post("/api/light")
    @guarded
    async def set_light(request):
        d = await body(request)
        rgb = d.get("rgb")
        if not isinstance(rgb, list) or len(rgb) != 3:
            return fail("rgb must be [r, g, b]")
        nova = need_client()
        await nova.set_light(tuple(_byte(c, "colour") for c in rgb))
        return ok(await nova.light())

    @routes.post("/api/restore")
    @guarded
    async def restore(request):
        d = await body(request)
        if d.get("confirm") is not True:
            return fail("confirmation required")
        nova = need_client()
        await nova.restore_defaults()
        return ok(await state_of(nova))

    # -- firmware ---------------------------------------------------------------
    @routes.get("/api/firmware")
    @guarded
    async def firmware(_):
        version = None
        if session.connected:
            try:
                version = (await session.client.info()).firmware_version
            except ERRORS:
                pass
        return ok(_firmware_status(fw_dir, version))

    @routes.post("/api/firmware/check")
    async def firmware_check(_):
        loop = asyncio.get_running_loop()
        try:
            r = await loop.run_in_executor(None, lambda: updater.update(fw_dir, feed_url=feed_url,
                                                                           check_only=True))
        except updater.UpdateError as e:
            return fail(str(e), 502)
        newer = updater.version_key(r.latest) > updater.version_key(r.current)
        return ok({"extracted": r.current, "latest": r.latest, "update_available": newer})

    @routes.post("/api/firmware/download")
    async def firmware_download(_):
        loop = asyncio.get_running_loop()
        try:
            r = await loop.run_in_executor(None, lambda: updater.update(fw_dir, feed_url=feed_url))
        except (updater.UpdateError, installer.ExtractError, OSError) as e:
            return fail(str(e), 502)
        return ok({"updated": r.updated, "latest": r.latest})

    from . import update
    from .. import updatemode
    app[update.KEY] = update.add_routes(routes, session, fw_dir, find_usb, ok, fail, body,
                                        backup_dir or updatemode.BACKUP_DIR,
                                        loader_cache or updatemode.LOADER_CACHE)

    app.add_routes(routes)
    app.router.add_static("/static/", STATIC, show_index=False)

    async def on_startup(_):
        if simulate:
            await session.connect_simulated()

    async def on_cleanup(_):
        await session.disconnect()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def serve(fw_dir: Path, *, host: str = "127.0.0.1", port: int = 0, simulate: bool = False,
          open_browser: bool = True) -> None:
    import socket
    import webbrowser

    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing to listen on a non-loopback address")
    token = secrets.token_urlsafe(24)
    sock = socket.socket(socket.AF_INET6 if host == "::1" else socket.AF_INET)
    sock.bind((host, port))
    actual = sock.getsockname()[1]
    url = f"http://{'127.0.0.1' if host != '::1' else '[::1]'}:{actual}/?token={token}"
    app = build_app(token, fw_dir, simulate=simulate)

    def ready():
        print(f"GameSir Linux is running at:\n  {url}\nPress Ctrl+C to stop.", flush=True)
        if open_browser:
            webbrowser.open(url)

    async def announce(_app):
        asyncio.get_running_loop().call_soon(ready)

    app.on_startup.append(announce)
    web.run_app(app, sock=sock, print=None)
