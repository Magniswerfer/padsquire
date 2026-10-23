"""`gamesir-linux uboot ...`: the controller's USB download mode, read-only.

The steps, each its own command so they can be run (and checked) one at a time:
  enter --confirm   switch the controller on the cable into download mode
  info              identify the chip and its flash
  backup            read the whole flash twice, compare, save it outside the repo
  restart           boot the firmware in flash again

Nothing here erases or writes flash; jlboot refuses those commands outright.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, hid, jlboot
from .client import NovaClient
from .gcm import ProtocolError

_XDG_DATA = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
_XDG_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
BACKUP_DIR = _XDG_DATA / "gamesir-linux" / "backups"
LOADER_CACHE = _XDG_CACHE / "gamesir-linux" / "loaders"
REPO_ROOT = Path(__file__).resolve().parents[2]

ENTER_WARNING = """\
This switches the controller into its USB download mode, the one GameSir Connect uses to update
firmware. It sends one command over the cable (f2 00 04 f6, as GameSir's own DLL does); nothing is
erased or written. The controller should come back as a USB disk named BR23 UBOOT1.00.

To leave the mode: `gamesir-linux uboot restart`. If that fails, hold Home + M for about 10 seconds.

Run again with --confirm to go ahead."""


def _no_sg_hint() -> str:
    if not Path("/sys/class/scsi_generic").exists():
        return " The sg kernel module isn't loaded: run `sudo modprobe sg` and try again."
    return ""


def _sg_path(args) -> str:
    if args.device:
        return args.device
    found = jlboot.find()
    if not found:
        raise jlboot.JLBootError("no controller in download mode found. Run `gamesir-linux uboot enter "
                                 "--confirm` first, or pass --device /dev/sgN." + _no_sg_hint())
    if len(found) > 1:
        raise jlboot.JLBootError("several devices in download mode, pick one with --device: "
                                 + ", ".join(f"{d.path} ({d.vendor} {d.product})" for d in found))
    return found[0].path


def _open(args) -> tuple[jlboot.SgDevice, jlboot.JLBoot]:
    dev = jlboot.SgDevice(_sg_path(args))
    return dev, jlboot.JLBoot(dev.execute)


def _inside_repo(path: Path) -> bool:
    return (REPO_ROOT / ".git").exists() and path.resolve().is_relative_to(REPO_ROOT)


def _progress(label: str):
    def show(done: int, total: int) -> None:
        if sys.stderr.isatty():
            print(f"\r{label}... {done * 100 // total}%", end="", file=sys.stderr, flush=True)
    return show


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
        print("no controller in download mode." + _no_sg_hint(), file=sys.stderr)
    return 0 if found else 1


async def _controller_info(path: str):
    async with hid.connect(path) as transport:
        return await NovaClient(transport).info()


def a_enter(args) -> int:
    if not args.confirm:
        print(ENTER_WARNING, file=sys.stderr)
        return 2
    path = args.device
    if not path:
        cables = [d.path for d in hid.find() if d.link == "cable"]
        if not cables:
            raise hid.HidError("no Nova 2 Lite on the cable. Connect it and switch to PC/Xbox mode "
                               "(hold Menu + View until the LED is green), or pass --device")
        if len(cables) > 1:
            raise hid.HidError("several controllers on the cable, pick one with --device: " + ", ".join(cables))
        path = cables[0]
    # Make sure it answers like a Nova 2 Lite before asking it to change mode.
    info = asyncio.run(_controller_info(path))
    print(f"controller    Nova 2 Lite on {path}, firmware {info.firmware_version}, MAC {info.mac}")
    before = {d.path for d in jlboot.disks()}
    hid.enter_update_mode(path)
    print("sent the update-mode command; waiting for the controller to come back as a USB disk...")
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        found = jlboot.find()
        if found:
            for d in found:
                print(f"download mode {d.path}  {d.vendor} {d.product}")
            print("Next: `gamesir-linux uboot info`.")
            return 0
        time.sleep(0.5)
    new = [d for d in jlboot.disks() if d.path not in before]
    print(f"no UBOOT disk appeared within {args.wait:.0f} s.{_no_sg_hint()}", file=sys.stderr)
    for d in new:
        print(f"  new SCSI device: {d.path}  {d.vendor!r} {d.product!r}", file=sys.stderr)
    print("Nothing more was sent. `gamesir-linux probe` shows how the controller enumerates now; "
          "to get it back, unplug it or hold Home + M for about 10 seconds.", file=sys.stderr)
    return 1


def a_info(args) -> int:
    dev, boot = _open(args)
    try:
        ident = jlboot.start(boot, LOADER_CACHE)
    finally:
        dev.close()
    _print_identity(ident)
    print("The loader runs in RAM; flash is unchanged. Next: `gamesir-linux uboot backup`, "
          "or `gamesir-linux uboot restart` to leave.")
    return 0


def a_backup(args) -> int:
    out_dir: Path = args.output
    if _inside_repo(out_dir):
        print(f"error: {out_dir} is inside the gamesir-linux repo; the backup holds GameSir's firmware "
              "and your controller's pairing keys, so keep it elsewhere", file=sys.stderr)
        return 1
    dev, boot = _open(args)
    try:
        ident = jlboot.start(boot, LOADER_CACHE)
        _print_identity(ident)
        size = args.size or ident.flash_size
        if not size:
            raise jlboot.JLBootError("the flash size is unknown; pass --size (e.g. --size 0x100000)")
        if ident.flash_size and size > ident.flash_size:
            raise jlboot.JLBootError(f"--size {size:#x} is larger than the flash ({ident.flash_size:#x})")
        try:
            data = jlboot.read_twice(boot, size, _progress("reading flash twice"))
        finally:
            if sys.stderr.isatty():
                print(file=sys.stderr)
        print(f"read          {size} bytes twice; both reads are identical")
        bad = jlboot.device_crc_mismatches(boot, data)
    finally:
        dev.close()

    sectors = size // jlboot.SECTOR
    if not bad:
        crc = "matches the dump in every sector"
    elif len(bad) == sectors:
        crc = "differs in every sector (likely another CRC variant; the dump itself is unaffected)"
    else:
        print(f"error: the controller's CRC disagrees with the dump in {len(bad)} of {sectors} sectors "
              f"(first at {bad[0]:#x}), although both reads matched. Not saving it.", file=sys.stderr)
        return 1
    print(f"device CRC    {crc}")

    digest = hashlib.sha256(data).hexdigest()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / f"nova2lite-{ident.vendor.lower()}-flash-{stamp}"
    image, manifest = base.with_suffix(".bin"), base.with_suffix(".json")
    # Owner-only: the flash holds the controller's Bluetooth pairing keys.
    with os.fdopen(os.open(image, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(data)
    if hashlib.sha256(image.read_bytes()).hexdigest() != digest:
        print(f"error: {image} does not read back as written", file=sys.stderr)
        return 1
    with os.fdopen(os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump({
            "tool": f"gamesir-linux {__version__}",
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "file": image.name, "size": size, "sha256": digest,
            "chip": {"vendor": ident.vendor, "product": ident.product, "revision": ident.revision,
                     "chip_key": f"{ident.chip_key:#06x}"},
            "flash": {"type": ident.flash_type, "jedec_id": f"{ident.jedec_id:#08x}",
                      "size": ident.flash_size},
            "reads": 2, "identical": True,
            "device_crc": {"sectors": sectors, "mismatched": len(bad)},
        }, f, indent=2)
        f.write("\n")
    print(f"saved         {image}")
    print(f"sha256        {digest}")
    print("Keep a copy somewhere safe. Next: `gamesir-linux uboot restart`.")
    return 0


def a_restart(args) -> int:
    dev, boot = _open(args)
    try:
        jlboot.ensure_loader(boot, LOADER_CACHE)
        try:
            boot.run_app()
        except jlboot.JLBootError:
            pass        # the controller may drop off USB before it answers
    finally:
        dev.close()
    print("asked the controller to restart; waiting for it to come back in normal mode...")
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if not jlboot.find() and hid.find():
            print("back in normal mode: " + ", ".join(f"{d.path} ({d.link})" for d in hid.find()))
            return 0
        time.sleep(0.5)
    if jlboot.find():
        print("it is still in download mode. Unplug it and hold Home + M for about 10 seconds, "
              "then turn it on again.", file=sys.stderr)
    else:
        print("it left download mode but its config interface hasn't appeared yet; if the LED isn't "
              "green, hold Menu + View for 2 seconds.", file=sys.stderr)
    return 1


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
    p = sub.add_parser("uboot", help="the controller's USB download mode: identify, back up, restart "
                                     "(read-only)")
    acts = p.add_subparsers(dest="action", required=True)

    acts.add_parser("find", help="list controllers in download mode (sends nothing)").set_defaults(run=a_find)

    e = acts.add_parser("enter", help="switch the controller on the cable into download mode")
    e.add_argument("--confirm", action="store_true", help="really send the command")
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

    r = acts.add_parser("restart", help="leave download mode and boot the firmware in flash")
    r.add_argument("--device", help="SCSI generic node, e.g. /dev/sg2 (default: find it)")
    r.add_argument("--wait", type=float, default=20.0, help="seconds to wait (default 20)")
    r.set_defaults(run=a_restart)

    p.set_defaults(func=run)


def run(args) -> int:
    try:
        return args.run(args)
    except (jlboot.JLBootError, hid.HidError, ProtocolError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
