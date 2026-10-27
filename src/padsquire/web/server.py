# SPDX-License-Identifier: AGPL-3.0-or-later
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
import os
import secrets
import time
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path

from aiohttp import web

from .. import (
    __version__,
    appupdate,
    ble,
    gcm,
    hid,
    installer,
    liveinput,
    paths,
    profiles,
    settingsfile,
    stickcheck,
    updater,
)
from ..client import NotApplied, NovaClient
from ..gcm import DeadZone, Feature, Key, ProtocolError, Vibration
from ..simulator import LINKS, SimulatedSticks, simulated

STATIC = Path(__file__).parent / "static"
COOKIE = "padsquire_session"
API_HEADER = "X-PadSquire"

ERRORS = (NotApplied, ProtocolError, ble.BleError, hid.HidError, ValueError, KeyError,
          asyncio.TimeoutError)
STICK_CHECK_MAX_SECONDS = 15

# Reads that get no reply over the USB cable (firmware 7701). Asking costs 11 x 200 ms each,
# so the state read skips them and the GUI shows them as unavailable.
USB_UNSUPPORTED = {"auto_sleep", "slots"}
USB_READONLY_FEATURES = gcm.USB_READONLY_FEATURES


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
    Key.START: "Menu", Key.HOME: "Home", Key.L4: "Left back button (L4)", Key.R4: "Right back button (R4)",
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
        self.mode: str | None = None          # "ble" | "usb", also for a simulated controller
        self.simulated = False
        self.via: str | None = None           # for "usb": "cable" or "dongle" (the 2.4 GHz receiver)
        self.address: str | None = None
        self.name: str | None = None
        self._ctx = None
        self.lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self.client is not None

    async def connect_simulated(self, link: str = "cable"):
        """A simulated controller that behaves like the real one on `link` (simulator.LINKS)."""
        device = simulated(link)
        await self.disconnect()
        self.client, self.address, self.name = NovaClient(device), "simulator", "GameSir Nova 2 Lite (simulated)"
        self.mode = "ble" if link == "bluetooth" else "usb"
        self.via = {"cable": "cable", "receiver": "dongle"}.get(link)
        self.simulated = True

    async def connect_ble(self, address: str, name: str | None = None):
        await self.disconnect()
        await self._open(ble.connect(address), "ble", address, name)

    async def connect_usb(self, path: str, name: str | None = None, via: str = "cable"):
        await self.disconnect()
        await self._open(hid.connect(path), "usb", path, name)
        self.via = via

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
        self.client = self.mode = self.address = self.name = self.via = None
        self.simulated = False

    @property
    def through_dongle(self) -> bool:
        """Over the receiver, basic info is the receiver's (firmware) mixed with the controller's
        (MAC, battery); the controller's own firmware version is not in it."""
        return self.mode == "usb" and self.via == "dongle"

    def status(self) -> dict:
        return {"connected": self.connected, "mode": self.mode, "address": self.address, "name": self.name,
                "via": self.via, "simulated": self.simulated}


async def _try(coro):
    """Run one read; a failure marks just that feature unavailable."""
    try:
        return {"value": _json(await coro)}
    except ERRORS as e:
        return {"error": str(e)}


async def read_state(nova: NovaClient, skip: set[str] = frozenset(), through_dongle: bool = False,
                     remaps: bool = False) -> dict:
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
    if remaps:
        # Every key (f1), by cable or receiver; Bluetooth keeps the back-button mapping (0x1F).
        state["remaps"] = {k.name: await _try(nova.remap(k)) for k in gcm.NOVA_REMAP_SOURCES}
    fw = info.get("value", {}).get("firmware_version") if "value" in info else None
    if through_dongle:
        # Basic info is the receiver's there; the controller's version comes from a state report.
        state["controller_firmware"] = await _try(nova.controller_firmware())
        fw = state["controller_firmware"].get("value")
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


class Presence:
    """Is a page still open? Pages poll every 2 s (once a minute in a hidden tab) and send a
    beacon to /api/bye when they close. The server may stop once no page is left, never while a
    firmware step runs."""

    def __init__(self, grace: float = 15.0, idle: float = 600.0, clock=time.monotonic):
        self.grace, self.idle, self.clock = grace, idle, clock
        self.last = clock()
        self.bye_at: float | None = None

    def seen(self) -> None:
        self.last, self.bye_at = self.clock(), None

    def bye(self) -> None:
        self.bye_at = self.clock()

    def gone(self, busy: bool) -> bool:
        if busy:
            return False
        now = self.clock()
        return (self.bye_at is not None and now - self.bye_at >= self.grace) or now - self.last >= self.idle


PRESENCE = web.AppKey("presence", Presence)


def build_app(token: str, fw_dir: Path, *, feed_url: str = updater.FEED_URL,
              simulate: str | None = None, scanner=None, usb_finder=None, gamesir_finder=None,
              backup_dir: Path | None = None, loader_cache: Path | None = None,
              live_open=None, profiles_dir: Path | None = None,
              app_update_url: str = appupdate.PYPI) -> web.Application:
    session = Session()
    store = profiles.Store(profiles_dir or paths.PROFILES_DIR)
    presence = Presence()
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
            return web.Response(status=403, text="Open the link printed by `padsquire gui`.")
        if request.path == "/api/bye" and request.method == "POST":
            presence.bye()                     # a beacon can't set headers; the cookie is enough
            return web.Response(status=204)
        presence.seen()
        if request.path.startswith("/api/") and request.headers.get(API_HEADER) != "1":
            return fail("missing header", 403)
        resp = await handler(request)
        if resp.prepared:      # a stream sets its own headers before it starts
            return resp
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self' 'wasm-unsafe-eval'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'")
        return resp

    app = web.Application(middlewares=[guard])
    app[PRESENCE] = presence
    routes = web.RouteTableDef()

    def open_live():
        """The gamepad device of the connected controller (cable or receiver only)."""
        if live_open is not None:
            return live_open(session)
        if session.mode != "usb":
            raise liveinput.Unavailable("live input is only available by cable or through the receiver")
        if session.simulated:
            return SimulatedSticks(session.client.t)
        if hid.MACOS:                      # no gamepad device there: the controller's own state reports
            if not hasattr(session.client.t, "live_state"):
                raise liveinput.Unavailable("live input is only available by cable or through the receiver")
            return liveinput.ReportInput(session.client.t)
        return liveinput.LiveInput(liveinput.event_node_for(session.address))

    async def state_of(nova: NovaClient) -> dict:
        return await read_state(nova, USB_UNSUPPORTED if session.mode == "usb" else frozenset(),
                                session.through_dongle, remaps=session.mode == "usb")

    def need_client():
        if not session.connected:
            raise web.HTTPConflict(text=json.dumps({"ok": False, "error": "no controller connected"}),
                                   content_type="application/json")
        return session.client

    async def body(request) -> dict:
        try:
            data = await request.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error": "invalid JSON"}),
                                     content_type="application/json") from e
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
            "remap_sources": [k.name for k in gcm.NOVA_REMAP_SOURCES],
            "usb_readonly_features": [f.name for f in USB_READONLY_FEATURES],
            "remap_targets": [k.name for k in gcm.NOVA_REMAP_TARGETS],
            "remap_max_targets": gcm.REMAP_MAX_TARGETS,
            "platform": "macos" if hid.MACOS else "linux",
        })

    @routes.get("/api/status")
    async def status(_):
        return ok(session.status())

    @routes.post("/api/scan")
    @guarded
    async def do_scan(_):
        # By ID, as Steam and the kernel's xpad do: the USB name is "2.4G XBOX 360 For Windows".
        usb = [{"address": d.path, "name": "GameSir Nova 2 Lite", "link": "usb",
                "via": "dongle" if d.pid == 0x1098 else "cable"} for d in find_usb()]
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
            link = data.get("link", "cable")
            if link not in LINKS:
                return fail(f"link must be one of {', '.join(LINKS)}")
            await session.connect_simulated(link)
        else:
            address = data.get("address")
            if not isinstance(address, str) or not address:
                return fail("address required")
            if data.get("link") == "usb":
                # Only open device nodes we found ourselves, never a path from the request.
                match = next((d for d in find_usb() if d.path == address), None)
                if match is None:
                    return fail("that USB controller is no longer connected; search again")
                await session.connect_usb(match.path, "GameSir Nova 2 Lite",
                                          "dongle" if match.pid == 0x1098 else "cable")
            else:
                await session.connect_ble(address, data.get("name"))
        return ok(session.status())

    @routes.get("/api/input")
    async def input_stream(request):
        """Live stick and trigger positions as JSON lines, about 30 a second, until the page
        stops reading. Not `guarded`: it must not hold the session lock while it runs."""
        if session.mode != "usb" and live_open is None:
            return fail("live input is only available by cable", 409)
        if hid.MACOS:
            return await report_stream(request)
        try:
            live = open_live()
        except liveinput.Unavailable as e:
            return fail(str(e), 409)
        resp = web.StreamResponse(headers={
            "Content-Type": "application/x-ndjson", "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff"})
        loop = asyncio.get_running_loop()
        ready = asyncio.Event()
        fd = getattr(live, "fd", None)     # a simulated controller has no device node: just poll it
        if fd is not None:
            loop.add_reader(fd, ready.set)
        try:
            await resp.prepare(request)
            while True:
                await resp.write(json.dumps(live.state()).encode() + b"\n")
                if fd is not None:
                    try:
                        await asyncio.wait_for(ready.wait(), timeout=1.0)
                    except TimeoutError:
                        pass       # nothing moved; send the same state as a keepalive
                    ready.clear()
                live.pump()
                await asyncio.sleep(1 / 30)
        except (ConnectionResetError, liveinput.Unavailable):
            pass
        finally:
            if fd is not None:
                loop.remove_reader(fd)
            live.close()
        return resp

    async def report_stream(request):
        """The same stream from the controller's own state reports (macOS: no xpad there)."""
        transport = session.client.t if session.client else None
        if not hasattr(transport, "live_state"):
            return fail("live input is only available by cable", 409)
        resp = web.StreamResponse(headers={
            "Content-Type": "application/x-ndjson", "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff"})
        try:
            await transport.live_state(on=True)
        except (hid.HidError, OSError) as e:
            return fail(f"the controller sends no live input: {e}", 409)
        try:
            await resp.prepare(request)
            loop = asyncio.get_running_loop()
            last, sent = None, 0.0
            while session.client is not None and session.client.t is transport:
                st = liveinput.state_from_report(await transport.live_state() or b"") or last
                # a change, or once a second as a keepalive: a write is how a closed page shows
                if st is not None and (st != last or loop.time() - sent > 1.0):
                    await resp.write(json.dumps(st).encode() + b"\n")
                    last, sent = st, loop.time()
                await asyncio.sleep(1 / 30)
        except (ConnectionResetError, hid.HidError, OSError, ValueError):
            pass               # ValueError: hidapi's "not open", once update mode took the device
        finally:
            if session.client is not None and session.client.t is transport:
                try:
                    await transport.live_state(on=False)
                except (hid.HidError, OSError):
                    pass
        return resp

    @routes.get("/api/devices")
    async def devices(_):
        """What is plugged in (sysfs only), for the page to notice plugging and unplugging. A USB
        session whose device has gone is closed here."""
        from . import setup
        found = setup.devices(find_usb, gamesir_finder)
        lost = False
        if session.mode == "usb" and not session.simulated and session.address not in {d["address"] for d in found["usb"]}:
            async with session.lock:
                await session.disconnect()
            lost = True
        return ok({**found, "status": session.status(), "lost": lost})

    @routes.get("/api/setup")
    async def setup_checks(_):
        from . import setup
        return ok(setup.checks(fw_dir))

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

    @routes.post("/api/vibration/test")
    @guarded
    async def vibration_test(request):
        d = await body(request)
        motor, strength = d.get("motor"), _byte(d.get("strength", 75), "strength")
        if motor not in ("left", "right") or strength > 100:
            return fail("motor must be left or right, strength 0-100")
        nova = need_client()
        await nova.test_vibration(strength if motor == "left" else 0, strength if motor == "right" else 0)
        return ok(None)

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

    @routes.post("/api/remap")
    @guarded
    async def set_remap(request):
        d = await body(request)
        key = Key[d["key"]]
        if key not in gcm.NOVA_REMAP_SOURCES:
            return fail(f"{key.name} cannot be remapped")
        targets = d.get("targets") or []
        if not isinstance(targets, list) or len(targets) > gcm.REMAP_MAX_TARGETS:
            return fail(f"targets must be a list of up to {gcm.REMAP_MAX_TARGETS} keys")
        targets = [Key[t] for t in targets]
        if len(set(targets)) != len(targets) or key in targets:
            return fail("each target can be used once, and not the button itself")
        if any(t not in gcm.NOVA_REMAP_TARGETS for t in targets):
            return fail("one of those targets cannot be used")
        nova = need_client()
        return ok(list(await nova.set_remap(key, targets)))

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

    # -- stick check --------------------------------------------------------------
    @routes.post("/api/stick-check")
    @guarded
    async def stick_check(request):
        """Measure drift ("drift": sticks left alone) or reach ("reach": turned around the edge)
        from the live input. The dead zones that would hide it are 0 for the test, then put back."""
        d = await body(request)
        seconds = d.get("seconds", 5)
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or not 0 < seconds <= STICK_CHECK_MAX_SECONDS:
            return fail(f"seconds must be more than 0 and at most {STICK_CHECK_MAX_SECONDS}")
        nova = need_client()
        try:
            live = open_live()
            if hasattr(live, "start"):     # the state-report reader (macOS) switches the reports on
                await live.start()
        except liveinput.Unavailable as e:
            return fail(str(e), 409)
        except (hid.HidError, OSError) as e:
            return fail(f"the controller sends no live input: {e}", 409)
        try:
            return ok(await stickcheck.run(nova, live, d.get("test"), float(seconds)))
        except liveinput.Unavailable as e:
            return fail(str(e), 409)
        except RuntimeError as e:          # a dead zone couldn't be put back
            return fail(str(e), 500)
        finally:
            if hasattr(live, "aclose"):
                try:
                    await live.aclose()
                except (hid.HidError, OSError):
                    pass
            else:
                live.close()

    # -- settings file ------------------------------------------------------------
    def file_scope() -> dict:
        # Every key (f1) by cable or receiver; the back buttons' own command (0x1F) over Bluetooth.
        return {"remaps": session.mode != "ble", "mappings": session.mode != "usb",
                "skip": USB_UNSUPPORTED if session.mode == "usb" else frozenset()}

    def cannot_write(change: dict) -> str | None:
        usb = session.mode == "usb"
        if usb and change["section"] == "features" and Feature[change["name"]] in USB_READONLY_FEATURES:
            return "the controller doesn't take this change by cable or through the receiver"
        if usb and change["section"] in ("auto_sleep", "mappings"):
            return "not available by cable or through the receiver"
        if session.mode == "ble" and change["section"] == "remaps":
            return "only by cable or through the receiver; over Bluetooth only the back buttons"
        return None

    async def controller_firmware(nova: NovaClient) -> str | None:
        try:
            return await nova.controller_firmware() if session.through_dongle else (await nova.info()).firmware_version
        except ERRORS:
            return None

    @routes.get("/api/settings/export")
    @guarded
    async def settings_export(_):
        nova = need_client()
        settings = await current_settings(nova)
        return ok(settingsfile.document(settings, app_version=__version__, firmware=await controller_firmware(nova)))

    @routes.post("/api/settings/import")
    @guarded
    async def settings_import(request):
        """Check a settings file and list what loading it changes; with "apply": true, write those
        changes. Nothing is written unless the whole file is valid."""
        d = await body(request)
        try:
            wanted = settingsfile.parse(d.get("file"))
        except settingsfile.InvalidFile as e:
            return fail(f"Can't load this file: {e}")
        return ok(await load(need_client(), wanted, d.get("apply") is True))

    async def current_settings(nova: NovaClient) -> dict:
        return await settingsfile.read(nova, **file_scope())

    def pending(wanted: dict, current: dict) -> list[dict]:
        """The changes loading `wanted` makes, each with why it can't be written here, if so."""
        return [{**c, "why_not": cannot_write(c) or settingsfile.unwritable(c)}
                for c in settingsfile.changes(wanted, settingsfile.typed(current))]

    async def load(nova: NovaClient, wanted: dict, apply: bool) -> dict:
        """Settings (from settingsfile.parse) onto the controller: a preview of the changes, or with
        `apply` the changes written, each read back, and the new state."""
        todo = pending(wanted, await current_settings(nova))
        if not apply:
            return {"changes": [{k: v for k, v in c.items() if k != "write"} for c in todo]}
        result = await settingsfile.apply(nova, [{k: v for k, v in c.items() if k != "why_not"} for c in todo],
                                          cannot_write)
        return {**result, "state": await state_of(nova)}

    # -- profiles -------------------------------------------------------------------
    def profile_summary(p: dict, current: dict | None) -> dict:
        doc = p["doc"]
        out = {"id": p["id"], "name": p["name"], "saved": doc.get("saved"),
               "firmware": (doc.get("controller") or {}).get("firmware")}
        if current is not None:
            diff = [c for c in pending(settingsfile.parse(doc), current) if not c["why_not"]]
            out["differs"] = len(diff)
            out["active"] = not diff
        return out

    @routes.get("/api/profiles")
    @guarded
    async def profile_list(_):
        """The profiles on this computer; connected, also which one the controller has now."""
        current = await current_settings(session.client) if session.connected else None
        return ok({"profiles": [profile_summary(p, current) for p in store.list()], "dir": str(store.root)})

    @routes.post("/api/profiles")
    @guarded
    async def profile_create(request):
        """A new profile: the controller's settings now, or a settings file ("file")."""
        d = await body(request)
        if "file" in d:
            try:
                settingsfile.parse(d["file"])
            except settingsfile.InvalidFile as e:
                return fail(f"Can't use this file: {e}")
            pid = store.create(d.get("name") or d["file"].get("name"), {k: v for k, v in d["file"].items() if k != "name"})
        else:
            nova = need_client()
            pid = store.create(d.get("name"), settingsfile.document(
                await current_settings(nova), app_version=__version__, firmware=await controller_firmware(nova)))
        return ok({"id": pid})

    @routes.post("/api/profiles/update")
    @guarded
    async def profile_update(request):
        """Keep the controller's settings now in an existing profile."""
        d = await body(request)
        nova = need_client()
        store.replace(d.get("id"), settingsfile.document(
            await current_settings(nova), app_version=__version__, firmware=await controller_firmware(nova)))
        return ok({"id": d.get("id")})

    @routes.post("/api/profiles/rename")
    @guarded
    async def profile_rename(request):
        d = await body(request)
        store.rename(d.get("id"), d.get("name"))
        return ok({"id": d.get("id")})

    @routes.post("/api/profiles/delete")
    @guarded
    async def profile_delete(request):
        d = await body(request)
        store.delete(d.get("id"))
        return ok(None)

    @routes.get("/api/profiles/file")
    @guarded
    async def profile_file(request):
        """A profile as a settings file, to save somewhere else."""
        return ok(store.get(request.query.get("id")))

    @routes.post("/api/profiles/switch")
    @guarded
    async def profile_switch(request):
        """Write a profile to the controller: only what differs, each setting read back."""
        d = await body(request)
        doc = store.get(d.get("id"))
        return ok({**await load(need_client(), settingsfile.parse(doc), apply=True), "name": doc["name"]})

    # -- firmware ---------------------------------------------------------------
    @routes.get("/api/firmware")
    @guarded
    async def firmware(_):
        version = dongle = None
        if session.connected:
            try:
                info = (await session.client.info()).firmware_version
                if session.through_dongle:
                    dongle = info
                    version = await session.client.controller_firmware()
                else:
                    version = info
            except ERRORS:
                pass
        return ok({**_firmware_status(fw_dir, version), "dongle_version": dongle})

    # -- PadSquire's own updates (appupdate.py) --------------------------------------------------

    @routes.get("/api/app-update")
    async def app_update_status(_):
        try:
            return ok(await asyncio.to_thread(appupdate.status, app_update_url))
        except appupdate.UpdateError as e:
            return fail(str(e), 502)

    @routes.post("/api/app-update/install")
    async def app_update_install(request):
        d = await body(request)
        if d.get("confirm") is not True:
            return fail("confirmation required")
        version = d.get("version")
        if not isinstance(version, str):
            return fail("version required")
        try:
            latest = await asyncio.to_thread(appupdate.newest, app_update_url)
            if version != latest:            # only ever the newest release, as the page showed it
                return fail(f"{version} isn't the newest release ({latest}); check again", 409)
            await asyncio.to_thread(appupdate.install, version)
        except appupdate.UpdateError as e:
            return fail(str(e), 502)
        return ok({"installed": version})

    @routes.post("/api/app-update/restart")
    async def app_update_restart(_):
        """Start the newly installed version; it stops this one (unless a firmware step runs)."""
        asyncio.get_running_loop().call_later(0.5, appupdate.restart)
        return ok({"restarting": True})

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

    from .. import updatemode
    from . import update
    app[update.KEY] = update.add_routes(routes, session, fw_dir, find_usb, ok, fail, body,
                                        backup_dir or updatemode.BACKUP_DIR,
                                        loader_cache or updatemode.LOADER_CACHE)

    app.add_routes(routes)
    app.router.add_static("/static/", STATIC, show_index=False)

    async def on_startup(_):
        if simulate:
            await session.connect_simulated(simulate)

    async def on_cleanup(_):
        await session.disconnect()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def _instance_file() -> Path | None:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime) / "padsquire" / "gui.json" if runtime else None


def _running_instance() -> dict | None:
    """The settings app this user already has running, if any: its pid, port, url and version
    (no version: 0.1.2 or older)."""
    import socket
    f = _instance_file()
    try:
        info = json.loads(f.read_text()) if f else None
        os.kill(info["pid"], 0)
        with socket.create_connection(("127.0.0.1", info["port"]), timeout=1):
            return info
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _busy(info: dict) -> bool:
    """Whether a running settings app is in the middle of a firmware step, or has a device in update
    mode. Asked through its own API; if it can't be asked, it counts as busy."""
    import http.cookiejar
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    try:
        opener.open(info["url"], timeout=3).read()          # the token in its url sets the session cookie
        req = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/update", headers={API_HEADER: "1"})
        data = json.loads(opener.open(req, timeout=10).read())["data"]
    except (OSError, ValueError, KeyError, TypeError):
        return True
    job = data.get("job") or {}
    return job.get("state") == "running" or data.get("mode") == "download"


def _stop_older(info: dict) -> bool:
    """Stop a settings app of another version (left running across an update), unless it is busy
    with firmware. True once it has stopped."""
    import signal
    import time
    if _busy(info):
        return False
    try:
        os.kill(info["pid"], signal.SIGTERM)                 # run_app's own clean shutdown
    except OSError:
        return True
    for _ in range(50):
        time.sleep(0.2)
        try:
            os.kill(info["pid"], 0)
        except OSError:
            return True
    return False


def serve(fw_dir: Path, *, host: str = "127.0.0.1", port: int = 0, simulate: str | None = None,
          open_browser: bool = True, tab: bool = False, keep_running: bool = False) -> None:
    import signal
    import socket

    from .. import desktop
    from . import update

    if host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("refusing to listen on a non-loopback address")
    single = not simulate and port == 0          # one app per user; a fixed port or a simulation is separate
    if single and (info := _running_instance()):
        older = info.get("version") or "0.1.2 or older"
        if info.get("version") != __version__ and _stop_older(info):
            print(f"Stopped PadSquire {older}, which was still running, to start {__version__}.", flush=True)
        else:
            if info.get("version") != __version__:
                print(f"PadSquire {older} is still running and busy with a firmware step or a device in\n"
                      f"update mode, so it stays open. Close it when that's done, then start PadSquire again.",
                      flush=True)
            print(f"PadSquire is already running at:\n  {info['url']}", flush=True)
            if open_browser:
                desktop.open_app(info["url"], tab)
            return
    token = secrets.token_urlsafe(24)
    sock = socket.socket(socket.AF_INET6 if host == "::1" else socket.AF_INET)
    # Restart on the same port at once, even while an old page's connection is still closing.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    actual = sock.getsockname()[1]
    url = f"http://{'127.0.0.1' if host != '::1' else '[::1]'}:{actual}/?token={token}"
    app = build_app(token, fw_dir, simulate=simulate)
    instance = _instance_file() if single else None

    def ready():
        stop = "Press Ctrl+C to stop." if keep_running else "It stops by itself when you close its window."
        print(f"PadSquire is running at:\n  {url}\n{stop}", flush=True)
        if open_browser:
            desktop.open_app(url, tab)

    async def announce(_app):
        asyncio.get_running_loop().call_soon(ready)
        if instance:
            instance.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(instance, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # holds the token
            with os.fdopen(fd, "w") as f:
                json.dump({"pid": os.getpid(), "port": actual, "url": url, "version": __version__}, f)
        if not keep_running:
            _app["watch"] = asyncio.create_task(watch(_app))

    async def watch(_app):
        presence, jobs = _app[PRESENCE], _app[update.KEY]
        while True:
            await asyncio.sleep(2)
            if presence.gone(jobs.busy()):
                print("The settings app was closed, so PadSquire stops.", flush=True)
                os.kill(os.getpid(), signal.SIGINT)      # run_app's own clean shutdown
                return

    async def forget(_app):
        try:
            if instance and json.loads(instance.read_text()).get("pid") == os.getpid():
                instance.unlink()
        except (OSError, ValueError):
            pass

    app.on_startup.append(announce)
    app.on_cleanup.append(forget)
    web.run_app(app, sock=sock, print=None)
