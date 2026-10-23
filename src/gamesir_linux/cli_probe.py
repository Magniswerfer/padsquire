# SPDX-License-Identifier: AGPL-3.0-or-later
"""`gamesir-linux probe`: read-only first contact with a controller."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import probe

GUIDE = [
    ("wired-xinput",
     "Connect the controller with the USB cable. Hold Home + X for 3 s (LED blue: XInput)."),
    ("wired-dinput", "Still on the cable: hold Home + Y for 3 s (LED green: DInput)."),
    ("wired-switch", "Still on the cable: hold Home + B for 3 s (LED red: Switch mode)."),
    ("dongle", "Unplug the cable. Plug in the 2.4 GHz dongle and turn the controller on "
               "so it connects to the dongle."),
    ("controller-update-mode", "Unplug everything. Hold M + X + B while plugging in the USB cable "
                               "(update mode). Nothing will be flashed."),
    ("dongle-update-mode", "Unplug the controller. Hold the dongle's pairing button while plugging "
                           "the dongle in (update mode). Nothing will be flashed."),
    ("bluetooth", "Unplug everything. Turn the controller on in Bluetooth mode; the phone app uses "
                  "Android mode, so try that first (Home + A for 3 s, LED yellow)."),
]


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def _usb_id(text: str) -> tuple[str, str]:
    vid, sep, pid = text.lower().partition(":")
    if not sep or not all(len(x) == 4 and all(c in "0123456789abcdef" for c in x) for x in (vid, pid)):
        raise argparse.ArgumentTypeError(f"expected VID:PID in hex, like 054c:09cc, not {text!r}")
    return vid, pid


def cmd_probe(args) -> int:
    out = args.output or Path(f"gamesir-probe-{datetime.now():%Y%m%d-%H%M%S}.json")
    report = probe.new_report()

    if args.guided:
        print("Read-only probe: nothing on the controller is changed.\n"
              "For each step: set the controller up as described, then press Enter.\n"
              "Type s to skip a step, q to stop and save.\n")
        for label, text in GUIDE:
            print(f"--- {label}\n{text}")
            answer = _ask("[Enter] probe / [s]kip / [q]uit: ")
            if answer == "q":
                break
            if answer == "s":
                continue
            snap = probe.snapshot(label, usb=label != "bluetooth", bluetooth=label == "bluetooth",
                                  address=args.address, extra_ids=args.device)
            report["snapshots"].append(snap)
            probe.save(report, out)
            print("\n".join(probe.summarize(snap)) + "\n")
    else:
        bluetooth = args.bluetooth or args.bluetooth_only or args.address is not None
        snap = probe.snapshot(args.label, usb=not args.bluetooth_only, bluetooth=bluetooth,
                              address=args.address, extra_ids=args.device)
        report["snapshots"].append(snap)
        print("\n".join(probe.summarize(snap)))

    probe.save(report, out)
    print(f"\nreport saved to {out}; please share this file")
    return 0


def register(sub) -> None:
    p = sub.add_parser("probe", help="read-only: collect details about a connected controller",
                       description="Collects USB, input and Bluetooth details into a JSON report. "
                                   "Never changes controller settings or flashes anything.")
    p.add_argument("--guided", action="store_true", help="walk through every connection mode")
    p.add_argument("--bluetooth", action="store_true", help="also probe Bluetooth")
    p.add_argument("--bluetooth-only", action="store_true", help="skip USB, probe only Bluetooth")
    p.add_argument("--address", help="Bluetooth address to connect to (default: auto)")
    p.add_argument("--label", default="manual", help="name for this snapshot")
    p.add_argument("-o", "--output", type=Path)
    p.add_argument("--device", type=_usb_id, action="append", default=[], metavar="VID:PID",
                   help="also include this USB device, e.g. 054c:09cc when the dongle "
                        "presents as a DualShock 4 (repeatable)")
    p.set_defaults(func=cmd_probe)
