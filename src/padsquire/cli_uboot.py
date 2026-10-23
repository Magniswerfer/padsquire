# SPDX-License-Identifier: AGPL-3.0-or-later
"""`padsquire uboot ...`: the controller's USB download mode.

The steps, each its own command so they can be run (and checked) one at a time:
  enter --confirm   switch the controller on the cable into download mode
  info              identify the chip and its flash
  backup            read the whole flash twice, compare, save it outside the repo
  restart           boot the firmware in flash again

Writing (jlwrite), each only with --confirm and a verified backup of this same controller:
  test-write        erase, write and erase back one unused sector above the layout
  restore           write the backup's code area back (firmware, as it was)
  flash             write a firmware update, as GameSir Connect would

Writes never reach the reserved areas (settings, Bluetooth address, calibration): the writer
refuses any address outside its window, and the whole flash is compared before and after.
The steps themselves live in updatemode, which the settings app uses too.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

from . import hid, jlboot, jlwrite, updatemode
from .client import NovaClient
from .gcm import ProtocolError
from .updatemode import BACKUP_DIR, LOADER_CACHE, REPO_ROOT   # noqa: F401 (tests patch these here)

ENTER_WARNING = """\
This switches the controller into its USB download mode, the one GameSir Connect uses to update
firmware. It sends one command over the cable (f2 00 04 f6, as GameSir's own DLL does); nothing is
erased or written. The controller should come back as a USB disk named BR23 UBOOT1.00.

To leave the mode: `padsquire uboot restart`. If that fails, hold Home for about 5 seconds.

Run again with --confirm to go ahead."""

WRITE_WARNING = """\
This writes to the controller's flash. Nothing above the code area (settings, Bluetooth
address, calibration) is touched, and every sector is checked afterwards; if something fails,
the controller stays in download mode and the command can be run again.
Run again with --confirm to go ahead."""


def _open(args) -> tuple[jlboot.SgDevice, jlboot.JLBoot]:
    return updatemode.open_device(args.device)


def _progress(label: str, done: int, total: int) -> None:
    if sys.stderr.isatty():
        end = "\n" if done >= total else ""
        print(f"\r{label}... {done * 100 // total}%   ", end=end, file=sys.stderr, flush=True)


def _log(text: str) -> None:
    print(text)


def _print_identity(ident: jlboot.Identity) -> None:
    size = f"{ident.flash_size // 1024} KB" if ident.flash_size else "unknown (pass --size to backup)"
    print(f"chip          {ident.vendor} ({ident.product} {ident.revision})")
    print(f"chip key      {ident.chip_key:#06x}")
    print(f"flash         SPI NOR, JEDEC ID {ident.jedec_id:#08x}, {size}")
    print(f"buffer        {ident.buffer_size} bytes")


# -- actions -------------------------------------------------------------------

def a_find(args) -> int:
    found = jlboot.find()
    for d in found:
        print(f"{d.path}  {d.vendor} {d.product}")
    if not found:
        print("no controller in download mode." + updatemode.no_sg_hint(), file=sys.stderr)
    return 0 if found else 1


async def _controller_info(path: str):
    async with hid.connect(path) as transport:
        return await NovaClient(transport).info()


def a_enter(args) -> int:
    if not args.confirm:
        print(ENTER_WARNING, file=sys.stderr)
        return 2
    path = args.device
    what = "2.4 GHz receiver" if args.receiver else "cable"
    if not path:
        found = [d.path for d in hid.find() if (d.pid == 0x1098) == args.receiver]
        if not found:
            raise hid.HidError(f"no Nova 2 Lite on the {what}. Switch it to PC/Xbox mode (hold Menu + View "
                               "until the LED is green), or pass --device")
        if len(found) > 1:
            raise hid.HidError(f"several on the {what}, pick one with --device: " + ", ".join(found))
        path = found[0]
    # Make sure it answers like a Nova 2 Lite before asking it to change mode.
    info = asyncio.run(_controller_info(path))
    if args.receiver:
        # Through the receiver, basic info is the receiver's, and the update command reaches it.
        print(f"receiver      on {path}, firmware {info.firmware_version} (the receiver goes into update mode)")
    else:
        print(f"controller    Nova 2 Lite on {path}, firmware {info.firmware_version}, MAC {info.mac}")
    print("sending the update-mode command; waiting for the controller to come back as a USB disk...")
    d = updatemode.enter(path, args.wait)
    print(f"download mode {d.path}  {d.vendor} {d.product}")
    print("Next: `padsquire uboot info`.")
    return 0


def a_info(args) -> int:
    dev, boot = _open(args)
    try:
        ident = jlboot.start(boot, LOADER_CACHE)
    finally:
        dev.close()
    _print_identity(ident)
    print("The loader runs in RAM; flash is unchanged. Next: `padsquire uboot backup`, "
          "or `padsquire uboot restart` to leave.")
    return 0


def a_backup(args) -> int:
    if updatemode.inside_repo(args.output):
        raise updatemode.Failed(f"{args.output} is inside the PadSquire repo; the backup holds GameSir's "
                                "firmware and your controller's pairing keys, so keep it elsewhere")
    dev, boot = _open(args)
    try:
        ident = jlboot.start(boot, LOADER_CACHE)
        _print_identity(ident)
        image = updatemode.backup(boot, ident, args.output, args.size, _log, _progress)
    finally:
        dev.close()
    print("Keep a copy somewhere safe. Next: `padsquire uboot restart`.")
    return 0 if image else 1


def a_restart(args) -> int:
    dev, boot = _open(args)
    try:
        updatemode.restart(boot, LOADER_CACHE)
    finally:
        dev.close()
    print("asked it to restart; waiting for it to come back in normal mode...")
    found = updatemode.wait_for_normal_mode(args.wait, receiver=args.receiver)
    print("back in normal mode" + (": " + ", ".join(f"{d.path} ({d.link})" for d in found) if found else ""))
    return 0


def a_test_write(args) -> int:
    if not args.confirm:
        print(WRITE_WARNING, file=sys.stderr)
        return 2
    dev, boot = _open(args)
    try:
        live, layout, _dump = updatemode.same_controller(boot, args.backup, LOADER_CACHE, _log, _progress)
        size = len(live)
        used_end = max([e.end for e in layout.areas.values() if e.end <= size]
                       + [layout.base + f.end for f in layout.files if f.length < size])
        addr = args.address if args.address is not None else size - jlwrite.SECTOR
        if addr % jlwrite.SECTOR or addr < used_end or addr + jlwrite.SECTOR > size:
            raise updatemode.Failed(f"{addr:#x} is not a free sector above the layout (which ends at {used_end:#x})")
        if live[addr:addr + jlwrite.SECTOR] != b"\xff" * jlwrite.SECTOR:
            raise updatemode.Failed(f"sector {addr:#x} is not blank; not using it")
        pattern = b"".join(hashlib.sha256(b"padsquire %d" % i).digest() for i in range(128))
        writer = jlwrite.Writer(boot, addr, addr + jlwrite.SECTOR,
                                keep=[(0, addr), (addr + jlwrite.SECTOR, size)])
        writer.program(addr, pattern)
        crc_ok = not writer.mismatches(addr, pattern)
        readback = boot.flash_read(addr, jlwrite.SECTOR)
        print(f"wrote         {addr:#x}: device CRC {'matches' if crc_ok else 'DIFFERS'}, "
              f"read back {'identical' if readback == pattern else 'DIFFERENT'}")
        writer.erase(addr)
        blank = boot.flash_read(addr, jlwrite.SECTOR) == b"\xff" * jlwrite.SECTOR
        print(f"erased        {addr:#x}: {'blank again' if blank else 'NOT BLANK'}")
        after = updatemode.read_all(boot, size, "reading the flash back twice", _progress)
    finally:
        dev.close()
    if after != live:
        print("error: the flash differs from before the test", file=sys.stderr)
        return 1
    if not (crc_ok and readback == pattern and blank):
        return 1
    print("flash         identical to before the test. Erase and write work.")
    return 0


def a_restore(args) -> int:
    if not args.confirm:
        print(WRITE_WARNING, file=sys.stderr)
        return 2
    dev, boot = _open(args)
    try:
        updatemode.restore(boot, args.backup, LOADER_CACHE, _log, _progress)
    finally:
        dev.close()
    print("Next: `padsquire uboot restart`.")
    return 0


def a_flash(args) -> int:
    if not args.confirm:
        print(WRITE_WARNING, file=sys.stderr)
        return 2
    dev, boot = _open(args)
    try:
        updatemode.flash(boot, args.ufw, args.backup, args.installed_version, args.firmware_dir,
                         args.reinstall, LOADER_CACHE, _log, _progress)
    except updatemode.Blocked as e:
        for reason in e.reasons:
            print(f"BLOCKED       {reason}", file=sys.stderr)
        return 2
    finally:
        dev.close()
    print("Next: `padsquire uboot restart`.")
    return 0


# -- wiring --------------------------------------------------------------------

def _size(text: str) -> int:
    try:
        v = int(text, 0)
    except ValueError:
        v = 0
    if v <= 0 or v % jlboot.SECTOR:
        raise argparse.ArgumentTypeError(f"expected a multiple of 4096, like 0x100000, not {text!r}")
    return v


def register(sub) -> None:
    p = sub.add_parser("uboot", help="the controller's USB download mode: identify, back up, restore, "
                                     "update")
    acts = p.add_subparsers(dest="action", required=True)

    acts.add_parser("find", help="list controllers in download mode (sends nothing)").set_defaults(run=a_find)

    e = acts.add_parser("enter", help="switch the controller on the cable into download mode")
    e.add_argument("--confirm", action="store_true", help="really send the command")
    e.add_argument("--receiver", action="store_true",
                   help="the 2.4 GHz receiver instead of the controller (controller in green mode, linked)")
    e.add_argument("--device", help="hidraw node of the cable's config interface (default: find it)")
    e.add_argument("--wait", type=float, default=20.0, help="seconds to wait for the disk (default 20)")
    e.set_defaults(run=a_enter)

    i = acts.add_parser("info", help="identify the chip and its flash (starts the loader in RAM)")
    i.add_argument("--device", help="SCSI generic node, e.g. /dev/sg2 (default: find it)")
    i.set_defaults(run=a_info)

    b = acts.add_parser("backup", help="read the whole flash twice, compare, save it")
    b.add_argument("--device", help="SCSI generic node, e.g. /dev/sg2 (default: find it)")
    b.add_argument("-o", "--output", type=Path, default=BACKUP_DIR,
                   help=f"directory outside the repo (default: {BACKUP_DIR})")
    b.add_argument("--size", type=_size, help="bytes to read, a multiple of 4096 (default: from the flash ID)")
    b.set_defaults(run=a_backup)

    t = acts.add_parser("test-write", help="write and erase one unused sector, to test writing")
    t.add_argument("--backup", type=Path, required=True, help="this controller's flash backup (.bin)")
    t.add_argument("--address", type=lambda x: int(x, 0), help="sector to use (default: the last one)")
    t.add_argument("--confirm", action="store_true", help="really write")
    t.add_argument("--device", help="SCSI generic node, e.g. /dev/sg2 (default: find it)")
    t.set_defaults(run=a_test_write)

    s = acts.add_parser("restore", help="write the backup's firmware back (keeps settings and calibration)")
    s.add_argument("backup", type=Path, help="this controller's flash backup (.bin)")
    s.add_argument("--confirm", action="store_true", help="really write")
    s.add_argument("--device", help="SCSI generic node, e.g. /dev/sg2 (default: find it)")
    s.set_defaults(run=a_restore)

    from .cli import DEFAULT_FW_DIR
    f = acts.add_parser("flash", help="install a firmware update, as GameSir Connect would")
    f.add_argument("ufw", type=Path, help="the .ufw file, from `padsquire update`")
    f.add_argument("--backup", type=Path, required=True, help="this controller's flash backup (.bin)")
    f.add_argument("--installed-version", type=int, required=True,
                   help="the firmware it runs now, from `padsquire usb info` (e.g. 7701)")
    f.add_argument("--firmware-dir", type=Path, default=DEFAULT_FW_DIR)
    f.add_argument("--reinstall", action="store_true")
    f.add_argument("--confirm", action="store_true", help="really write")
    f.add_argument("--device", help="SCSI generic node, e.g. /dev/sg2 (default: find it)")
    f.set_defaults(run=a_flash)

    r = acts.add_parser("restart", help="leave download mode and boot the firmware in flash")
    r.add_argument("--device", help="SCSI generic node, e.g. /dev/sg2 (default: find it)")
    r.add_argument("--wait", type=float, default=20.0, help="seconds to wait (default 20)")
    r.add_argument("--receiver", action="store_true", help="it is the 2.4 GHz receiver that restarts")
    r.set_defaults(run=a_restart)

    p.set_defaults(func=run)


def run(args) -> int:
    try:
        return args.run(args)
    except updatemode.Failed as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (jlboot.JLBootError, hid.HidError, ProtocolError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
