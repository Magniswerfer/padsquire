# SPDX-License-Identifier: AGPL-3.0-or-later
"""`padsquire c2 ...`: first contact with a GameSir Cyclone 2, read-only.

  find   list the Cyclone 2 configuration interfaces plugged in (sysfs only, sends nothing)
  info   firmware version, battery, the active profile and its main settings
  dump   every profile, raw and decoded, to a JSON file for a hardware report

Only reads: a heartbeat, the firmware version, the current profile number and the profiles.
Nothing is written to the controller (see c2usb.py).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from . import c2, c2usb


def _device(args) -> c2usb.C2Device | None:
    found = c2usb.find()
    if args.device:
        return next((d for d in found if d.path == args.device),
                    c2usb.C2Device(args.device, 0, "given on the command line"))
    if not found:
        print("No Cyclone 2 found. Connect it with the cable or its receiver and check that the udev "
              "rules are installed (`padsquire udev-rules`). `padsquire probe` shows what is plugged in.",
              file=sys.stderr)
        return None
    return found[0]


def _open(args) -> c2usb.C2Transport | None:
    if args.simulate:
        from .simulator import SimulatedC2
        sim = SimulatedC2()
        return c2usb.C2Transport(sim.read, sim.write)
    dev = _device(args)
    return c2usb.open_device(dev) if dev else None      # reads only: no writes=True here


def a_find(args) -> int:
    found = c2usb.find()
    for d in found:
        print(f"{d.path}  3537:{d.pid:04x}  {d.link}  {d.name}")
    if not found:
        print("No Cyclone 2 configuration interface found.")
    return 0


def a_info(args) -> int:
    t = _open(args)
    if t is None:
        return 1
    try:
        t.heartbeat()
        fw = t.firmware_version()
        current = t.current_profile()
        state = t.state()
        number = current if current in (1, 2, 3, 4) else 1
        profile = c2.decode_profile(t.profile(number))
    finally:
        t.close()
    print(f"firmware      {fw.controller}" + (f"  (receiver {fw.receiver})" if fw.receiver else ""))
    if state:
        s = c2.parse_state(state)
        print(f"battery       {s.battery}%{' (on the cable)' if s.charging else ''}")
    else:
        print("battery       no state report arrived")
    print(f"profile       {current}: {profile['name'] or '(no name)'}")
    v = profile["vibration"]
    print(f"vibration     left {v['left']}%, right {v['right']}%, triggers {v['LT']}% / {v['RT']}%")
    for side in ("left", "right"):
        dz = profile["sticks"][side]["dead_zone"]
        print(f"{side + ' stick':13} dead zone {dz['start']}-{dz['end']}% (unconfirmed)")
    for key in ("LT", "RT"):
        dz = profile["triggers"][key]["dead_zone"]
        print(f"{key:13} dead zone {dz['start']}-{dz['end']}% (unconfirmed)")
    return 0


def a_dump(args) -> int:
    t = _open(args)
    if t is None:
        return 1
    try:
        report = c2usb.read_everything(t)
    finally:
        t.close()
    report["padsquire"] = "c2 dump"
    report["taken"] = datetime.now().isoformat(timespec="seconds")
    out = args.output or Path(f"cyclone2-dump-{datetime.now():%Y%m%d-%H%M%S}.json")
    out.write_text(json.dumps(report, indent=1) + "\n")
    print(f"Saved {out}: firmware {report['firmware']['controller']}, "
          f"{len(report['profiles'])} profiles. Attach it to a hardware test report.")
    return 0


def register(sub) -> None:
    p = sub.add_parser("c2", help="read-only: the GameSir Cyclone 2 (firmware, battery, profiles)")
    p.add_argument("--device", help="hidraw node, e.g. /dev/hidraw5 (default: find it)")
    p.add_argument("--simulate", action="store_true", help="talk to a simulated Cyclone 2")
    acts = p.add_subparsers(dest="action", required=True)
    acts.add_parser("find", help="list Cyclone 2 configuration interfaces (sends nothing)").set_defaults(run=a_find)
    acts.add_parser("info", help="firmware, battery and the active profile").set_defaults(run=a_info)
    d = acts.add_parser("dump", help="save every profile, raw and decoded, to a JSON file")
    d.add_argument("-o", "--output", type=Path, help="file to write (default: cyclone2-dump-<time>.json)")
    d.set_defaults(run=a_dump)
    p.set_defaults(func=run)


def run(args) -> int:
    try:
        return args.run(args)
    except (c2usb.C2UsbError, c2.C2Error, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
