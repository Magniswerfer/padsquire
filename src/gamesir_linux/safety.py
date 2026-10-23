"""Decide whether a firmware file may be flashed onto a device.

Every rule here exists because breaking it can brick the device or leave the
controller and dongle unable to talk to each other. The planner only reads
data; it never touches hardware.

Assumptions still to confirm on real hardware are marked ASSUMPTION.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from . import ufw
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
