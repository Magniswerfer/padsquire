"""`gamesir-linux ble ...`: configure a Nova 2 Lite over Bluetooth LE."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys

from . import ble
from .client import NotApplied, NovaClient
from .gcm import DeadZone, Feature, ProtocolError, Vibration
from .simulator import SimulatedNova


def _names(enum) -> dict[str, object]:
    return {m.name.lower().replace("_", "-"): m for m in enum}


ZONES = _names(DeadZone)
FEATURES = _names(Feature)
ON_OFF = {"on": True, "off": False, "true": True, "false": False, "1": True, "0": False}


def _byte(text: str) -> int:
    v = int(text)
    if not 0 <= v <= 255:
        raise argparse.ArgumentTypeError("must be 0-255")
    return v


@contextlib.asynccontextmanager
async def _open(args):
    if args.simulate:
        yield NovaClient(SimulatedNova())
        return
    address = args.address
    if not address:
        found = await ble.scan()
        if not found:
            raise ble.BleError(
                "no Nova 2 Lite found. Put it in Bluetooth mode and pair it, or pass --address")
        if len(found) > 1:
            raise ble.BleError("several found, pick one with --address: "
                               + ", ".join(f"{a} ({n})" for a, n in found))
        address = found[0][0]
    async with ble.connect(address) as transport:
        yield NovaClient(transport)


# -- actions -------------------------------------------------------------------

async def a_scan(args) -> int:
    if args.simulate:
        print("00:00:00:00:00:00  GameSir-Nova 2 Lite_SIM  (simulated)")
        return 0
    found = await ble.scan(args.timeout)
    for address, name in found:
        print(f"{address}  {name}")
    if not found:
        print("nothing found (is the controller in Bluetooth mode?)", file=sys.stderr)
    return 0 if found else 1


async def a_info(args) -> int:
    async with _open(args) as nova:
        info = await nova.info()
        bat = await nova.battery()
    print(f"firmware      {info.firmware_version}")
    print(f"hardware      {info.hardware_version}")
    print(f"MAC           {info.mac}")
    print(f"made          {info.production_date}")
    print(f"protocol      GCM {info.gcm_version}")
    print(f"battery       {bat.level}%{' (charging)' if bat.charging else ''}  [raw {bat.raw:#04x}]")
    return 0


async def a_deadzone(args) -> int:
    async with _open(args) as nova:
        if args.zone is None:
            for name, zone in ZONES.items():
                print(f"{name:22} {await nova.dead_zone(zone)}")
        elif args.value is None:
            print(await nova.dead_zone(ZONES[args.zone]))
        else:
            await nova.set_dead_zone(ZONES[args.zone], args.value)
            print(f"{args.zone} = {args.value}")
    return 0


async def a_feature(args) -> int:
    async with _open(args) as nova:
        if args.name is None:
            for name, feat in FEATURES.items():
                print(f"{name:22} {'on' if await nova.feature(feat) else 'off'}")
        elif args.state is None:
            print("on" if await nova.feature(FEATURES[args.name]) else "off")
        else:
            await nova.set_feature(FEATURES[args.name], ON_OFF[args.state])
            print(f"{args.name} = {args.state}")
    return 0


VIB_FIELDS = ["left_grip", "left_trigger", "right_grip", "right_trigger"]


async def a_vibration(args) -> int:
    async with _open(args) as nova:
        v = await nova.vibration()
        changes = {f: getattr(args, f) for f in VIB_FIELDS if getattr(args, f) is not None}
        if changes:
            v = Vibration(**{**v.__dict__, **changes})
            await nova.set_vibration(v)
    for f in VIB_FIELDS:
        print(f"{f.replace('_', '-'):15} {getattr(v, f)}")
    return 0


async def a_sleep(args) -> int:
    async with _open(args) as nova:
        if args.minutes is not None:
            await nova.set_auto_sleep(args.minutes)
        print(f"auto sleep: {await nova.auto_sleep()}")
    return 0


async def a_restore(args) -> int:
    if not args.yes:
        print("this resets every setting on the controller; add --yes to confirm", file=sys.stderr)
        return 2
    async with _open(args) as nova:
        await nova.restore_defaults()
    print("restored factory defaults")
    return 0


# -- wiring --------------------------------------------------------------------

def register(sub) -> None:
    p = sub.add_parser("ble", help="configure the controller over Bluetooth LE")
    p.add_argument("--address", help="Bluetooth address (default: scan for one)")
    p.add_argument("--simulate", action="store_true", help="talk to a simulated controller")
    acts = p.add_subparsers(dest="action", required=True)

    s = acts.add_parser("scan", help="list Nova 2 Lite controllers in range")
    s.add_argument("--timeout", type=float, default=6.0)
    s.set_defaults(run=a_scan)

    acts.add_parser("info", help="firmware version, battery, MAC").set_defaults(run=a_info)

    d = acts.add_parser("deadzone", help="show or set dead zones")
    d.add_argument("zone", nargs="?", choices=list(ZONES))
    d.add_argument("value", nargs="?", type=_byte, help="raw value (units unconfirmed)")
    d.set_defaults(run=a_deadzone)

    f = acts.add_parser("feature", help="show or toggle features (stick swap, D-pad lock, ...)")
    f.add_argument("name", nargs="?", choices=list(FEATURES))
    f.add_argument("state", nargs="?", choices=list(ON_OFF))
    f.set_defaults(run=a_feature)

    v = acts.add_parser("vibration", help="show or set rumble strength")
    for field in VIB_FIELDS:
        v.add_argument("--" + field.replace("_", "-"), dest=field, type=_byte)
    v.set_defaults(run=a_vibration)

    z = acts.add_parser("sleep", help="show or set the auto-sleep time")
    z.add_argument("minutes", nargs="?", type=_byte)
    z.set_defaults(run=a_sleep)

    r = acts.add_parser("restore", help="reset all settings to factory defaults")
    r.add_argument("--yes", action="store_true")
    r.set_defaults(run=a_restore)

    p.set_defaults(func=run)


def run(args) -> int:
    try:
        return asyncio.run(args.run(args))
    except (ble.BleError, ProtocolError, NotApplied) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
