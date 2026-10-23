"""`gamesir-linux uboot ...`: the controller's USB download mode.

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

from . import __version__, hid, installer, jlboot, jlflash, jlwrite, safety
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


# -- writing -------------------------------------------------------------------

WRITE_WARNING = """\
This writes to the controller's flash. Nothing above the code area (settings, Bluetooth
address, calibration) is touched, and every sector is checked afterwards; if something fails,
the controller stays in download mode and the command can be run again.
Run again with --confirm to go ahead."""
ATTEMPTS = 3


def _read_all(boot, size: int, label: str) -> bytes:
    try:
        return jlboot.read_twice(boot, size, _progress(label))
    finally:
        if sys.stderr.isatty():
            print(file=sys.stderr)


def _preflight(boot, backup: Path):
    """Same controller as the backup? Returns (live flash, its layout, the backup)."""
    dump, key = safety.load_backup(backup)
    manifest = json.loads(backup.with_suffix(".json").read_text())
    ident = jlboot.start(boot, LOADER_CACHE)
    if ident.chip_key != key or f"{ident.jedec_id:#08x}" != manifest["flash"]["jedec_id"]:
        raise jlboot.JLBootError(f"this is not the controller in {backup.name} (chip key "
                                 f"{ident.chip_key:#06x}, flash {ident.jedec_id:#08x})")
    size = ident.flash_size or len(dump)
    if size != len(dump):
        raise jlboot.JLBootError(f"the flash is {size} bytes, the backup {len(dump)}")
    live = _read_all(boot, size, "reading the flash twice")
    layout = jlflash.find(live, key)
    for name in ("BTIF", "USERIF"):
        lo, hi = layout.area(name)
        if live[lo:hi] != dump[lo:hi]:
            raise jlboot.JLBootError(f"{name} ({lo:#x}-{hi - 1:#x}) differs from the backup: "
                                     "not the same controller, or not the same backup")
    print(f"controller    same as {backup.name} (chip key, flash ID, Bluetooth address, calibration)")
    return live, layout, dump


def _burn(boot, live: bytes, layout: jlflash.Layout, image: bytes) -> int:
    code_end = layout.code[1]
    if len(image) != code_end:
        raise jlboot.JLBootError(f"the image is {len(image):#x} bytes, the code area ends at {code_end:#x}")
    writer = jlwrite.Writer(boot, 0, code_end, keep=[(code_end, len(live))])

    def show(what, done, total):
        if sys.stderr.isatty():
            print(f"\rwriting... {done * 100 // total}% ({what})   ", end="", file=sys.stderr, flush=True)

    for attempt in range(1, ATTEMPTS + 1):
        try:
            jlwrite.burn(writer, image, layout.base, show)
            break
        except jlwrite.WriteError as e:
            print(f"\nattempt {attempt} failed: {e}", file=sys.stderr)
    else:
        print(f"error: the write did not verify after {ATTEMPTS} attempts. Do NOT restart the "
              "controller; it is still in download mode. Run the same command again.", file=sys.stderr)
        return 1
    if sys.stderr.isatty():
        print(file=sys.stderr)
    after = _read_all(boot, len(live), "reading the flash back twice")
    if after[:code_end] != image:
        print("error: the code area does not read back as written. Do NOT restart; run the "
              "command again.", file=sys.stderr)
        return 1
    if after[code_end:] != live[code_end:]:
        print("error: something above the code area changed. Do NOT restart; compare with the "
              "backup before doing anything else.", file=sys.stderr)
        return 1
    print(f"written       {0:#x}-{code_end - 1:#x}, every sector verified; everything above "
          f"{code_end:#x} unchanged")
    print("Next: `gamesir-linux uboot restart`.")
    return 0


def a_test_write(args) -> int:
    if not args.confirm:
        print(WRITE_WARNING, file=sys.stderr)
        return 2
    dev, boot = _open(args)
    try:
        live, layout, dump = _preflight(boot, args.backup)
        size = len(live)
        used_end = max([e.end for e in layout.areas.values() if e.end <= size]
                       + [layout.base + f.end for f in layout.files if f.length < size])
        addr = args.address if args.address is not None else size - jlwrite.SECTOR
        if addr % jlwrite.SECTOR or addr < used_end or addr + jlwrite.SECTOR > size:
            raise jlboot.JLBootError(f"{addr:#x} is not a free sector above the layout (which ends at {used_end:#x})")
        if live[addr:addr + jlwrite.SECTOR] != b"\xff" * jlwrite.SECTOR:
            raise jlboot.JLBootError(f"sector {addr:#x} is not blank; not using it")
        pattern = b"".join(hashlib.sha256(b"gamesir-linux %d" % i).digest() for i in range(128))
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
        after = _read_all(boot, size, "reading the flash back twice")
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
        live, layout, dump = _preflight(boot, args.backup)
        saved = jlflash.find(dump, int(json.loads(args.backup.with_suffix(".json").read_text())
                                       ["chip"]["chip_key"], 16))
        if saved.code != layout.code or saved.base != layout.base:
            raise jlboot.JLBootError("the backup's code area is not where the controller's is now")
        image = dump[:layout.code[1]]
        same = live[:len(image)] == image
        print(f"restoring     {0:#x}-{len(image) - 1:#x} from {args.backup.name}"
              + (" (identical to what is there now)" if same else ""))
        return _burn(boot, live, layout, image)
    finally:
        dev.close()


def a_flash(args) -> int:
    if not args.confirm:
        print(WRITE_WARNING, file=sys.stderr)
        return 2
    bundled = installer.load_manifest(args.firmware_dir)
    fw = next((f for f in bundled if (args.firmware_dir / f.file).resolve() == args.ufw.resolve()), None)
    if fw is None:
        raise jlboot.JLBootError(f"{args.ufw} is not in the firmware manifest in {args.firmware_dir}")
    dev, boot = _open(args)
    try:
        live, layout, dump = _preflight(boot, args.backup)
        pid, vid = jlflash.identity(live, layout.base)
        plan = safety.plan_update(safety.DeviceState("controller", pid, vid, args.installed_version),
                                  fw, args.ufw, bundled=bundled, reinstall=args.reinstall)
        choice = safety.choose_flash_image(args.ufw, args.backup)
        errors = plan.errors + choice.errors
        for e in errors:
            print(f"BLOCKED       {e}", file=sys.stderr)
        if errors:
            return 2
        image = safety.update_image(args.ufw, choice)
        print(f"flashing      {fw.file} (v{fw.version}): {choice.partition} -> "
              f"{choice.start:#x}-{choice.end - 1:#x}")
        return _burn(boot, live, layout, image)
    finally:
        dev.close()


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
    f.add_argument("ufw", type=Path, help="the .ufw file, from `gamesir-linux update`")
    f.add_argument("--backup", type=Path, required=True, help="this controller's flash backup (.bin)")
    f.add_argument("--installed-version", type=int, required=True,
                   help="the firmware it runs now, from `gamesir-linux usb info` (e.g. 7701)")
    f.add_argument("--firmware-dir", type=Path, default=DEFAULT_FW_DIR)
    f.add_argument("--reinstall", action="store_true")
    f.add_argument("--confirm", action="store_true", help="really write")
    f.add_argument("--device", help="SCSI generic node, e.g. /dev/sg2 (default: find it)")
    f.set_defaults(run=a_flash)

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
