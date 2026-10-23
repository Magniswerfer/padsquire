"""Decide whether a firmware file may be flashed onto a device.

Every rule here exists because breaking it can brick the device or leave the
controller and dongle unable to talk to each other. The planner only reads
data; it never touches hardware.

Assumptions still to confirm on real hardware are marked ASSUMPTION.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import jlflash, ufw
from .installer import FirmwareInfo

# ASSUMPTION: the last digit of a controller version is its region, inferred from
# GameSir's table (China 8200 needs >=5600, International 8201 needs >=5601).
REGIONS = {0: "china", 1: "international"}


@dataclass
class DeviceState:
    """What the device reports about itself (in update mode)."""
    target: str                      # "controller" or "dongle"
    jl_pid: str | None = None        # e.g. "AC695X_TRANS"
    jl_vid: str | None = None        # e.g. "N2LT"
    version: int | None = None       # e.g. 8101


@dataclass
class Plan:
    firmware: FirmwareInfo
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return not self.errors


def region_of(version: int) -> str | None:
    return REGIONS.get(version % 10)


def plan_update(
    device: DeviceState,
    firmware: FirmwareInfo,
    fw_path: str | Path,
    *,
    dongle_version: int | None = None,
    bundled: list[FirmwareInfo] = (),
    reinstall: bool = False,
) -> Plan:
    plan = Plan(firmware)
    err, warn = plan.errors.append, plan.warnings.append

    # 1. The file is exactly the one the manifest describes, and intact.
    data = Path(fw_path).read_bytes()
    if hashlib.sha256(data).hexdigest() != firmware.sha256:
        err("firmware file does not match the manifest (sha256 differs)")
    try:
        problems = ufw.parse(data).problems()
    except ufw.UfwError as e:
        problems = [str(e)]
    for p in problems:
        err(f"firmware file is damaged: {p}")

    # 2. Right kind of device.
    if device.target != firmware.target:
        err(f"this is {firmware.target} firmware, but the device is a {device.target}")

    # 3. Right model: JieLi identity strings must match exactly.
    if device.jl_pid is None or device.jl_vid is None:
        err("device did not report its identity; refusing to guess")
    else:
        if device.jl_pid != firmware.jl_pid:
            err(f"chip mismatch: device {device.jl_pid}, firmware {firmware.jl_pid}")
        if device.jl_vid != firmware.jl_vid:
            err(f"model mismatch: device {device.jl_vid}, firmware {firmware.jl_vid}")

    # 4. Version rules.
    if device.version is None:
        err("device did not report its firmware version")
    else:
        if firmware.target == "controller":
            dev_region, fw_region = region_of(device.version), firmware.region
            if dev_region is None:
                err(f"cannot tell the region of installed version {device.version}")
            elif dev_region != fw_region:
                err(f"region mismatch: device runs the {dev_region} build, "
                    f"firmware is the {fw_region} build")
        if firmware.minimum_version is not None and device.version < firmware.minimum_version:
            err(f"installed version {device.version} is older than {firmware.minimum_version}; "
                "GameSir requires an intermediate update first")
        if device.version > firmware.version:
            err(f"refusing to downgrade from {device.version} to {firmware.version}")
        elif device.version == firmware.version and not reinstall:
            err(f"version {firmware.version} is already installed (use --reinstall to flash anyway)")

    # 5. Controller and dongle firmware ship as a matched pair.
    if firmware.target == "controller":
        dongles = [fw for fw in bundled if fw.target == "dongle"]
        if dongles:
            want = max(d.version for d in dongles)
            if dongle_version is None:
                warn(f"dongle version unknown; this release pairs with dongle firmware {want}")
            elif dongle_version < want:
                warn(f"update the dongle too: it runs {dongle_version}, this release ships {want}")

    return plan


def choose_firmware(device: DeviceState, bundled: list[FirmwareInfo]) -> FirmwareInfo | None:
    """The firmware in `bundled` meant for this device, or None if nothing fits.
    The caller must still run plan_update() on the result."""
    candidates = [fw for fw in bundled if fw.target == device.target and fw.jl_vid == device.jl_vid]
    if device.target == "controller" and device.version is not None:
        candidates = [fw for fw in candidates if fw.region == region_of(device.version)]
    return max(candidates, key=lambda fw: fw.version, default=None)


# -- the flash layout, checked against this controller's own backup ------------------------------

@dataclass
class FlashChoice:
    partition: str | None             # e.g. "flash2.bin"
    start: int = 0
    end: int = 0
    errors: list[str] = field(default_factory=list)


def load_backup(path: str | Path) -> tuple[bytes, int]:
    """A flash backup made by `gamesir-linux uboot backup`, checked against its manifest.
    Returns the dump and the controller's chip key."""
    path = Path(path)
    manifest = json.loads(path.with_suffix(".json").read_text())
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != manifest["sha256"] or not manifest.get("identical"):
        raise ValueError(f"{path} does not match its manifest; not using it")
    return data, int(manifest["chip"]["chip_key"], 16)


def choose_flash_image(fw_path: str | Path, backup: str | Path) -> FlashChoice:
    """Which firmware image in the .ufw fits this controller's flash, and where it goes.
    Exactly one image must keep every reserved area of the backup where it is."""
    try:
        dump, key = load_backup(backup)
    except (OSError, KeyError, ValueError) as e:
        return FlashChoice(None, errors=[f"cannot use the backup: {e}"])
    device = None
    for base in (0x1000, 0, 0x10000):
        try:
            device = jlflash.parse(dump, base, key)
            break
        except jlflash.FlashError:
            continue
    if device is None:
        return FlashChoice(None, errors=["cannot read the flash layout in the backup"])
    f = ufw.load(fw_path)
    fits, refused = [], []
    for e in f.entries:
        if not (e.name.startswith("flash") and e.name.endswith(".bin")):
            continue
        plan = jlflash.check_update(device, f.payload(e.name), key)
        (fits if plan.ok else refused).append((e.name, plan))
    if len(fits) == 1:
        name, plan = fits[0]
        return FlashChoice(name, plan.start, plan.end)
    if not fits:
        return FlashChoice(None, errors=["no image in the firmware file fits this controller's flash: "
                                         + "; ".join(f"{n}: {', '.join(p.problems)}" for n, p in refused)])
    return FlashChoice(None, errors=["several images fit this controller's flash ("
                                     + ", ".join(n for n, _ in fits) + "); refusing to guess"])
