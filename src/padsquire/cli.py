# SPDX-License-Identifier: AGPL-3.0-or-later
"""PadSquire command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, installer, jlflash, paths, safety, ufw

DEFAULT_FW_DIR = paths.FIRMWARE_DIR


def cmd_extract(args) -> int:
    try:
        manifest = installer.extract(args.installer, args.output, model=args.model)
    except (installer.ExtractError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"GameSir Connect {manifest['gamesir_connect_version']}: "
          f"extracted {len(manifest['firmware'])} firmware files to {args.output}")
    for fw in manifest["firmware"]:
        region = f", {fw['region']}" if fw["region"] else ""
        print(f"  {fw['target']:10} v{fw['version']}{region:15} {fw['file']}")
    return 0


def cmd_update(args) -> int:
    from . import updater

    shown = []

    def progress(done, total):
        if sys.stderr.isatty():
            pct = f"{done * 100 // total}%" if total else f"{done >> 20} MB"
            print(f"\rdownloading GameSir Connect... {pct}", end="", file=sys.stderr, flush=True)
            shown.append(True)

    try:
        r = updater.update(args.firmware_dir, feed_url=args.feed, check_only=args.check,
                           force=args.force, keep_installer=args.keep_installer, progress=progress)
    except (updater.UpdateError, installer.ExtractError, OSError) as e:
        print(("\n" if shown else "") + f"error: {e}", file=sys.stderr)
        return 1
    if shown:
        print(file=sys.stderr)
    have = r.current or "none"
    if r.updated:
        print(f"updated firmware from GameSir Connect {have} to {r.latest}")
        for fw in r.manifest["firmware"]:
            region = f", {fw['region']}" if fw["region"] else ""
            print(f"  {fw['target']:10} v{fw['version']}{region:15} {fw['file']}")
    elif updater.version_key(r.latest) > updater.version_key(r.current):
        print(f"GameSir Connect {r.latest} is available (you have {have}); "
              "run `padsquire update` to fetch it")
    else:
        print(f"up to date (GameSir Connect {have})")
    return 0


def cmd_gui(args) -> int:
    try:
        from .web.server import serve
    except ImportError:
        print("the GUI needs aiohttp: pip install --user 'padsquire[gui]'", file=sys.stderr)
        return 1
    serve(args.firmware_dir, port=args.port, simulate=args.simulate, open_browser=not args.no_browser,
          tab=args.tab, keep_running=args.keep_running or args.no_browser)
    return 0


def cmd_menu(args) -> int:
    from . import desktop
    if args.remove:
        gone = desktop.remove_entry()
        print("Removed " + ", ".join(map(str, gone)) if gone else
              "PadSquire wasn't in ~/Applications." if desktop.MACOS else "PadSquire wasn't in the app menu.")
        return 0
    try:
        written = desktop.install_entry()
    except FileExistsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for f in written:
        print(f"Wrote {f}")
    where = "in ~/Applications (Launchpad and Spotlight find it)" if desktop.MACOS else "in your app menu"
    print(f"PadSquire is {where}. To take it out again: padsquire menu --remove")
    return 0


def cmd_udev_rules(args) -> int:
    from . import desktop
    if args.path:
        print(desktop.rules_path())
    else:
        sys.stdout.write(desktop.rules_text())
    return 0


def cmd_ufw(args) -> int:
    try:
        f = ufw.load(args.file)
    except (ufw.UfwError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    problems = f.problems()
    if args.json:
        print(json.dumps({
            "chip": f.chip, "jl_pid": f.jl_pid, "jl_vid": f.jl_vid,
            "tool_version": f.tool_version, "size": len(f.data), "ok": not problems,
            "problems": problems,
            "entries": [{"name": e.name, "type": e.type, "offset": e.offset,
                         "length": e.length, "crc_ok": e.crc_ok} for e in f.entries],
        }, indent=2))
    else:
        print(f"chip      {f.chip}")
        print(f"identity  PID={f.jl_pid}  VID={f.jl_vid}")
        print(f"built by  {f.tool_version}")
        print(f"size      {len(f.data)} bytes, {len(f.entries)} partitions")
        for e in f.entries:
            mark = "ok " if e.crc_ok else "BAD"
            print(f"  [{mark}] {e.name:16} offset {e.offset:#09x}  {e.length:>8} bytes")
        print("verdict   " + ("intact" if not problems else "DAMAGED: " + "; ".join(problems)))
    return 0 if not problems else 1


def cmd_plan(args) -> int:
    try:
        bundled = installer.load_manifest(args.firmware_dir)
    except (OSError, KeyError, ValueError) as e:
        print(f"error: cannot read manifest in {args.firmware_dir}: {e}", file=sys.stderr)
        return 1
    device = safety.DeviceState(args.target, args.pid, args.vid, args.version)
    if not (args.pid and args.vid):
        print("BLOCKED    device identity unknown (--pid/--vid); refusing to guess")
        return 2
    fw = safety.choose_firmware(device, bundled)
    if fw is None:
        print("no matching firmware in this release for that device")
        return 1
    plan = safety.plan_update(
        device, fw, Path(args.firmware_dir) / fw.file,
        dongle_version=args.dongle_version, bundled=bundled, reinstall=args.reinstall,
    )
    print(f"candidate  {fw.file} (v{fw.version})")
    errors = list(plan.errors)
    if args.backup is None:
        errors.append("no flash backup of this controller (--backup, from `padsquire uboot backup`)")
    else:
        image = safety.choose_flash_image(Path(args.firmware_dir) / fw.file, args.backup)
        errors += image.errors
        if image.partition:
            print(f"image      {image.partition} -> {image.start:#x}-{image.end - 1:#x}; keeps "
                  + ", ".join(jlflash.RESERVED))
    for w in plan.warnings:
        print(f"warning    {w}")
    for e in errors:
        print(f"BLOCKED    {e}")
    print("verdict    " + ("would flash" if not errors else "will not flash"))
    return 0 if not errors else 2


def main(argv=None) -> int:
    if os.environ.get("PADSQUIRE_DEBUG"):
        import logging
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s.%(msecs)03d %(name)s %(message)s",
                            datefmt="%H:%M:%S")
    for line in paths.migrate():          # the folders of the old name, gamesir-linux, once
        print(f"PadSquire: {line}", file=sys.stderr)
    p = argparse.ArgumentParser(prog="padsquire", description="PadSquire: Linux and macOS tools for GameSir controllers")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="pull firmware out of a GameSir Connect installer")
    e.add_argument("installer", help="GameSir Connect Setup .exe")
    e.add_argument("-o", "--output", type=Path, default=DEFAULT_FW_DIR)
    e.add_argument("--model", default="T4N2", help="GameSir model code (T4N2 = Nova 2 Lite)")
    e.set_defaults(func=cmd_extract)

    from .updater import FEED_URL
    up = sub.add_parser("update", help="check for new official firmware and download it")
    up.add_argument("--check", action="store_true", help="only report whether a newer release exists")
    up.add_argument("--force", action="store_true", help="re-download even if already current")
    up.add_argument("--firmware-dir", type=Path, default=DEFAULT_FW_DIR)
    up.add_argument("--keep-installer", type=Path, metavar="PATH", help="also save the installer here")
    up.add_argument("--feed", default=FEED_URL, help=argparse.SUPPRESS)
    up.set_defaults(func=cmd_update)

    u = sub.add_parser("ufw", help="inspect and verify a .ufw firmware file")
    u.add_argument("file")
    u.add_argument("--json", action="store_true")
    u.set_defaults(func=cmd_ufw)

    pl = sub.add_parser(
        "plan", help="dry run: would this device be updated, and with what?",
        description="Until device detection exists, describe the device by hand.",
    )
    pl.add_argument("--firmware-dir", type=Path, default=DEFAULT_FW_DIR)
    pl.add_argument("--target", choices=["controller", "dongle"], required=True)
    pl.add_argument("--pid", help="JieLi PID string, e.g. AC695X_TRANS")
    pl.add_argument("--vid", help="JieLi VID string, e.g. N2LT")
    pl.add_argument("--version", type=int, help="installed firmware version, e.g. 8101")
    pl.add_argument("--dongle-version", type=int)
    pl.add_argument("--reinstall", action="store_true")
    pl.add_argument("--backup", type=Path, help="this controller's flash backup (.bin with its .json)")
    pl.set_defaults(func=cmd_plan)

    from . import cli_ble, cli_c2, cli_probe, cli_uboot
    cli_ble.register(sub)
    cli_c2.register(sub)
    cli_probe.register(sub)
    cli_uboot.register(sub)

    g = sub.add_parser("gui", help="open the graphical settings app in your browser")
    g.add_argument("--simulate", nargs="?", const="cable", choices=["cable", "receiver", "bluetooth"],
                   help="start connected to a simulated controller, behaving as on that connection (default: cable)")
    g.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: any free port)")
    g.add_argument("--no-browser", action="store_true",
                   help="print the address instead of opening it (keeps running until Ctrl+C)")
    g.add_argument("--tab", action="store_true", help="open in a browser tab instead of its own window")
    g.add_argument("--keep-running", action="store_true",
                   help="don't stop when the app's window is closed")
    g.add_argument("--firmware-dir", type=Path, default=DEFAULT_FW_DIR)
    g.set_defaults(func=cmd_gui)

    m = sub.add_parser("menu", help="add PadSquire to your app menu, or ~/Applications on macOS (no root needed)")
    m.add_argument("--remove", action="store_true", help="take it out of the app menu again")
    m.set_defaults(func=cmd_menu)

    r = sub.add_parser("udev-rules", help="print the udev rules that let you use the controller without sudo")
    r.add_argument("--path", action="store_true", help="print where the rules file is instead")
    r.set_defaults(func=cmd_udev_rules)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
