# SPDX-License-Identifier: AGPL-3.0-or-later
"""The controller's USB download mode as steps, shared by the command line and the settings app.

Each step reports through two callbacks, log(text) and progress(label, done, total), and raises
Failed with a message meant for the user. `stay` on Failed means: the controller is still in
download mode and must not be restarted (a write did not verify); run the same step again.

Order of use: enter -> identify -> backup -> restore | flash -> restart. Every write needs a
verified backup of the same controller (jlwrite, PROTOCOL.md section 3).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, hid, installer, jlboot, jlflash, jlwrite, paths, safety

BACKUP_DIR = paths.BACKUP_DIR
LOADER_CACHE = paths.LOADER_CACHE
REPO_ROOT = Path(__file__).resolve().parents[2]
ATTEMPTS = 3
# What is in download mode, by chip: the safety rules' target names (safety.DeviceState).
TARGET_BY_CHIP = {"br23": "controller", "bd19": "dongle"}

Log = Callable[[str], None]
Progress = Callable[[str, int, int], None]


def _quiet(*_args) -> None:
    pass


class Failed(jlboot.JLBootError):
    def __init__(self, message: str, stay: bool = False):
        super().__init__(message)
        self.stay = stay


class Blocked(Failed):
    """The safety checks refuse a firmware update; nothing was written."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def no_sg_hint() -> str:
    if jlboot.MACOS:
        return ""
    if not Path("/sys/class/scsi_generic").exists():
        return " The sg kernel module isn't loaded: run `sudo modprobe sg` and try again."
    return ""


# -- finding and opening ------------------------------------------------------------------------

def find_path(path: str | None = None) -> str:
    if path:
        return path
    found = jlboot.find()
    if not found:
        raise Failed("no controller in download mode found." + no_sg_hint())
    if len(found) > 1:
        raise Failed("several devices in download mode: "
                     + ", ".join(f"{d.path} ({d.vendor} {d.product})" for d in found))
    return found[0].path


def open_device(path: str | None = None):
    """(device, JLBoot) for the download-mode chip. On macOS, through the settings app's
    administrator helper when it runs (macos_helper), else directly (the CLI, under sudo)."""
    if jlboot.MACOS and path is None:
        from . import macos_helper
        if macos_helper.running():
            dev = macos_helper.HelperDevice()
            return dev, jlboot.JLBoot(dev.execute)
    dev = jlboot.open_device(find_path(path))
    return dev, jlboot.JLBoot(dev.execute)


def wait_for_download_mode(before: set[str], timeout: float) -> jlboot.UbootDevice:
    """After the update-mode command: wait for the UBOOT disk. On a timeout, Failed names any
    other new SCSI device that appeared, and nothing more is sent."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = jlboot.find()
        if found:
            return found[0]
        time.sleep(0.5)
    new = [d for d in jlboot.disks() if d.path not in before]
    extra = "".join(f" A new device appeared instead: {d.path} {d.vendor!r} {d.product!r}." for d in new)
    raise Failed(f"no download-mode disk appeared within {timeout:.0f} s.{no_sg_hint()}{extra} Nothing more "
                 "was sent; unplug the controller or hold Home for about 5 seconds to get it back.")


def enter(hid_path: str, timeout: float = 20.0) -> jlboot.UbootDevice:
    """Send the update-mode command to the controller on `hid_path` and wait for it."""
    before = {d.path for d in jlboot.disks()}
    hid.enter_update_mode(hid_path)
    return wait_for_download_mode(before, timeout)


RECEIVER_PIDS = {0x2106, 0x1098}       # idle, and carrying a green-mode controller


def receiver_present(sys_root: Path = Path("/sys")) -> bool:
    for dev in (sys_root / "bus" / "usb" / "devices").glob("*"):
        try:
            if (int((dev / "idVendor").read_text(), 16) == hid.VENDOR_ID
                    and int((dev / "idProduct").read_text(), 16) in RECEIVER_PIDS):
                return True
        except (OSError, ValueError):
            continue
    return False


def wait_for_normal_mode(timeout: float, receiver: bool = False) -> list[hid.HidDevice]:
    """Wait until download mode is gone and the controller's cable interface (or, for the
    receiver, the receiver in any normal identity) is back."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not jlboot.find():
            if receiver:
                if receiver_present():
                    return [d for d in hid.find() if d.pid == 0x1098]
            else:
                found = [d for d in hid.find() if d.link == "cable"]
                if found:
                    return found
        time.sleep(0.5)
    if jlboot.find():
        raise Failed("it is still in download mode. Unplug it and hold Home for about 5 seconds, "
                     "then turn it on again.")
    raise Failed("it left download mode but its config interface hasn't appeared yet; if the LED isn't "
                 "green, hold Menu + View for 2 seconds.")


def restart(boot: jlboot.JLBoot, cache_dir: Path = LOADER_CACHE) -> None:
    jlboot.ensure_loader(boot, cache_dir)
    try:
        boot.run_app()
    except jlboot.JLBootError:
        pass        # the controller may drop off USB before it answers


# -- backups ----------------------------------------------------------------------------------------

def inside_repo(path: Path) -> bool:
    return (REPO_ROOT / ".git").exists() and path.resolve().is_relative_to(REPO_ROOT)


@dataclass
class Backup:
    path: Path
    created: str
    sha256: str
    size: int
    chip_key: int
    jedec_id: str

    @property
    def name(self) -> str:
        return self.path.name


def list_backups(directory: Path = BACKUP_DIR) -> list[Backup]:
    """Backups whose manifest says both reads matched, newest first (files are not hashed here)."""
    out = []
    for manifest in sorted(directory.glob("*.json"), reverse=True):
        try:
            m = json.loads(manifest.read_text())
            image = manifest.with_suffix(".bin")
            if not image.exists() or not m.get("identical"):
                continue
            out.append(Backup(image, m["created"], m["sha256"], m["size"],
                              int(m["chip"]["chip_key"], 16), m["flash"]["jedec_id"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def read_all(boot: jlboot.JLBoot, size: int, label: str, progress: Progress) -> bytes:
    return jlboot.read_twice(boot, size, lambda done, total: progress(label, done, total))


def backup(boot: jlboot.JLBoot, ident: jlboot.Identity, out_dir: Path = BACKUP_DIR,
           size: int | None = None, log: Log = _quiet, progress: Progress = _quiet) -> Path:
    """Read the whole flash twice, compare, check against the device CRC, save. Returns the .bin."""
    if inside_repo(out_dir):
        raise Failed(f"{out_dir} is inside the PadSquire repo; the backup holds GameSir's firmware "
                     "and your controller's pairing keys, so keep it elsewhere")
    size = size or ident.flash_size
    if not size:
        raise Failed("the flash size is unknown; give the size to read")
    if ident.flash_size and size > ident.flash_size:
        raise Failed(f"size {size:#x} is larger than the flash ({ident.flash_size:#x})")
    data = read_all(boot, size, "Reading the flash twice", progress)
    log(f"Read {size} bytes twice; both reads are identical.")
    bad = jlboot.device_crc_mismatches(boot, data)
    sectors = size // jlboot.SECTOR
    if bad and len(bad) != sectors:
        raise Failed(f"the controller's CRC disagrees with the dump in {len(bad)} of {sectors} sectors "
                     f"(first at {bad[0]:#x}), although both reads matched. Not saving it.")
    log("The controller's own CRC matches the dump in every sector." if not bad
        else "The controller's CRC differs in every sector (likely another CRC variant).")

    digest = hashlib.sha256(data).hexdigest()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    paths.make_dirs(out_dir)
    base = out_dir / f"nova2lite-{ident.vendor.lower()}-flash-{stamp}"
    image, manifest = base.with_suffix(".bin"), base.with_suffix(".json")
    # Owner-only: the flash holds the controller's Bluetooth pairing keys.
    with os.fdopen(os.open(image, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
        f.write(data)
    paths.give_back(image)
    if hashlib.sha256(image.read_bytes()).hexdigest() != digest:
        raise Failed(f"{image} does not read back as written")
    with os.fdopen(os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump({
            "tool": f"padsquire {__version__}",
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "file": image.name, "size": size, "sha256": digest,
            "chip": {"vendor": ident.vendor, "product": ident.product, "revision": ident.revision,
                     "chip_key": f"{ident.chip_key:#06x}"},
            "flash": {"type": ident.flash_type, "jedec_id": f"{ident.jedec_id:#08x}",
                      "size": ident.flash_size},
            "reads": 2, "identical": True,
            "device_crc": {"sectors": sectors, "mismatched": len(bad)},
        }, f, indent=2)
        f.write("\n")
    paths.give_back(manifest)
    log(f"Saved {image} (SHA-256 {digest}).")
    return image


# -- writing --------------------------------------------------------------------------------------

def same_controller(boot: jlboot.JLBoot, backup_path: Path, cache_dir: Path = LOADER_CACHE,
                    log: Log = _quiet, progress: Progress = _quiet):
    """Is this the controller in the backup? Returns (live flash, its layout, the backup's bytes)."""
    try:
        dump, key = safety.load_backup(backup_path)
        manifest = json.loads(backup_path.with_suffix(".json").read_text())
    except (OSError, KeyError, ValueError) as e:
        raise Failed(f"cannot use the backup: {e}") from e
    ident = jlboot.start(boot, cache_dir)
    if ident.chip_key != key or f"{ident.jedec_id:#08x}" != manifest["flash"]["jedec_id"]:
        raise Failed(f"this is not the device in {backup_path.name} (chip key "
                     f"{ident.chip_key:#06x}, flash {ident.jedec_id:#08x})")
    size = ident.flash_size or len(dump)
    if size != len(dump):
        raise Failed(f"the flash is {size} bytes, the backup {len(dump)}")
    live = read_all(boot, size, "Reading the flash twice", progress)
    layout = jlflash.find(live, key)
    for name in ("BTIF", "USERIF"):
        lo, hi = layout.area(name)
        if live[lo:hi] != dump[lo:hi]:
            raise Failed(f"{name} ({lo:#x}-{hi - 1:#x}) differs from the backup: not the same device, "
                         "or not the same backup")
    log(f"Same device as {backup_path.name}: chip key, flash ID, Bluetooth data and user data match.")
    return live, layout, dump


def burn(boot: jlboot.JLBoot, live: bytes, layout: jlflash.Layout, image: bytes,
         log: Log = _quiet, progress: Progress = _quiet) -> None:
    """Write `image` over [0, end of the code area), verify, and read everything back twice."""
    code_end = layout.code[1]
    if len(image) != code_end:
        raise Failed(f"the image is {len(image):#x} bytes, the code area ends at {code_end:#x}")
    writer = jlwrite.Writer(boot, 0, code_end, keep=[(code_end, len(live))])
    for attempt in range(1, ATTEMPTS + 1):
        try:
            jlwrite.burn(writer, image, layout.base, lambda what, d, t: progress("Writing", d, t))
            break
        except jlwrite.WriteError as e:
            log(f"Attempt {attempt} did not verify: {e}")
    else:
        raise Failed(f"the write did not verify after {ATTEMPTS} attempts. Do not restart the controller; "
                     "it is still in download mode. Run the same step again.", stay=True)
    after = read_all(boot, len(live), "Reading the flash back twice", progress)
    if after[:code_end] != image:
        raise Failed("the code area does not read back as written. Do not restart; run the step again.",
                     stay=True)
    if after[code_end:] != live[code_end:]:
        raise Failed("something above the code area changed. Do not restart; compare with the backup "
                     "before doing anything else.", stay=True)
    log(f"Wrote {0:#x}-{code_end - 1:#x}, every sector verified; everything above {code_end:#x} unchanged.")


def restore(boot: jlboot.JLBoot, backup_path: Path, cache_dir: Path = LOADER_CACHE,
            log: Log = _quiet, progress: Progress = _quiet) -> None:
    """Write the backup's code area back: the firmware as it was, settings and calibration kept."""
    live, layout, dump = same_controller(boot, backup_path, cache_dir, log, progress)
    saved = jlflash.find(dump, int(json.loads(backup_path.with_suffix(".json").read_text())
                                   ["chip"]["chip_key"], 16))
    if saved.code != layout.code or saved.base != layout.base:
        raise Failed("the backup's code area is not where the controller's is now")
    image = dump[:layout.code[1]]
    log(f"Restoring {0:#x}-{len(image) - 1:#x} from {backup_path.name}"
        + (" (identical to what is there now)." if live[:len(image)] == image else "."))
    burn(boot, live, layout, image, log, progress)


def plan_flash(ufw_path: Path, backup_path: Path, installed_version: int, fw_dir: Path,
               live: bytes, layout: jlflash.Layout, reinstall: bool = False, target: str = "controller"):
    """All the checks for a firmware update. Returns (FirmwareInfo, FlashChoice, errors)."""
    bundled = installer.load_manifest(fw_dir)
    fw = next((f for f in bundled if (fw_dir / f.file).resolve() == ufw_path.resolve()), None)
    if fw is None:
        return None, None, [f"{ufw_path.name} is not in the firmware manifest in {fw_dir}"]
    pid, vid = jlflash.identity(live, layout.base)
    plan = safety.plan_update(safety.DeviceState(target, pid, vid, installed_version),
                              fw, ufw_path, bundled=bundled, reinstall=reinstall)
    choice = safety.choose_flash_image(ufw_path, backup_path)
    return fw, choice, plan.errors + choice.errors


def flash(boot: jlboot.JLBoot, ufw_path: Path, backup_path: Path, installed_version: int,
          fw_dir: Path, reinstall: bool = False, cache_dir: Path = LOADER_CACHE,
          log: Log = _quiet, progress: Progress = _quiet) -> int:
    """Install a firmware update exactly as GameSir Connect would. Returns the new version."""
    live, layout, _dump = same_controller(boot, backup_path, cache_dir, log, progress)
    target = TARGET_BY_CHIP.get(boot.inquiry()[0].lower(), "unknown")
    fw, choice, errors = plan_flash(ufw_path, backup_path, installed_version, fw_dir, live, layout,
                                    reinstall, target)
    if errors:
        raise Blocked(errors)
    image = safety.update_image(ufw_path, choice)
    log(f"Installing {fw.file} (version {fw.version}): {choice.partition} at "
        f"{choice.start:#x}-{choice.end - 1:#x}.")
    burn(boot, live, layout, image, log, progress)
    return fw.version
