# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which GameSir controller is plugged in, and what PadSquire can do with it.

The table comes from GameSir Connect's own device list (data/connect-devices.json, made with
tools/connect-devices.py), plus identities other projects found on hardware that Connect doesn't
list (EXTRA_IDS). What decides whether PadSquire can talk to a controller is the protocol family
it belongs to (FAMILY), not the model: a new edition of a known controller is usually just a new
USB ID in a family we already speak. Controllers someone added on their own computer
(`added.json` in the data folder) come first.

Recognising a controller never sends it anything: this reads sysfs (hidapi on macOS) only.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path

from . import paths

VENDOR_ID = 0x3537
MACOS = sys.platform == "darwin"

# Protocol families, by GameSir's model code (stable across Connect versions, unlike its
# minified handler names). PROTOCOL.md, "Other GameSir controllers in Connect", has the detail.
FAMILY = {
    "T4N2": "gcm", "N2S": "gcm", "T3XS": "gcm",          # GameSir's DLL: GCM on a vendor channel
    "C2": "c2", "T3Lite": "c2",                           # profile images on usage page 0xFFF0
    "T3Pro": "sapjoy", "T4NPro": "sapjoy",                # GameSir's older DLL
    "G7ProCE": "g7-8k", "G7ProS": "g7-8k", "K2_8K": "g7-8k", "T3CE": "g7-8k",
    "G7Pro": "g7", "G7SE": "g7se", "Kaleid": "kaleid",    # not in Connect (GameSir Nexus)
    "NovaFamily": "gcm",                                  # IDs GameSir's DLL opens, no model known
}
FAMILY_NAMES = {
    "gcm": "GameSir's configuration protocol, as on the Nova 2 Lite",
    "c2": "the Cyclone 2's profile protocol",
    "sapjoy": "an older GameSir configuration protocol",
    "g7-8k": "the protocol of GameSir's 8K controllers",
    "g7": "the G7 Pro's protocol, inside the Xbox connection",
    "g7se": "the G7 SE's protocol",
    "kaleid": "the Kaleid's lighting protocol",
}
# The families PadSquire can talk to, and the usage page of their settings interface.
SPOKEN = {"gcm": 0xFF7A, "c2": 0xFFF0}

NAMES = {"T4N2": "Nova 2 Lite", "N2S": "Nova Vision", "T3XS": "T3XS", "C2": "Cyclone 2",
         "T3Lite": "Tegenaria Lite", "T3Pro": "Tarantula Pro", "T4NPro": "Nova Pro",
         "G7ProCE": "G7 Pro 8K", "G7ProS": "G7 Pro S", "K2_8K": "K2 8K", "T3CE": "Tarantula Pro 8K",
         "G7Pro": "G7 Pro", "G7SE": "G7 SE", "Kaleid": "Kaleid",
         "NovaFamily": "GameSir controller (Nova 2 Lite family)"}

# What PadSquire does with each model today:
#   settings   settings (and, for the Nova 2 Lite, firmware) in the app
#   read       read in the app, settings not yet
#   try        speaks a family PadSquire knows, not tested on one: can be added (Add this controller)
#   no         not supported; ELSEWHERE may list a project that is
SUPPORT = {"T4N2": "settings", "C2": "read", "N2S": "try", "T3XS": "try", "NovaFamily": "try"}

DEADBAND = ("Deadband", "https://github.com/broroeror/gamesir-linux-tools")
G7CTL = ("g7ctl", "https://github.com/questionablesyntax/g7ctl")
NATIVE = ("gamesir-native-linux", "https://github.com/RetroGamerGER/gamesir-native-linux")
CYCLONE2_LINUX = ("cyclone2-linux", "https://github.com/vdemonchy/cyclone2-linux")
ELSEWHERE = {
    "C2": (DEADBAND, CYCLONE2_LINUX), "G7ProCE": (DEADBAND,), "T3CE": (DEADBAND,),
    "G7Pro": (DEADBAND, G7CTL, NATIVE), "G7SE": (DEADBAND,), "Kaleid": (DEADBAND,),
}

# Identities seen on hardware that Connect's table doesn't have: (model, PID, link, product
# test). A product test is a lowercase word the USB product name must contain, "!word" one it
# must not, or None. Sources: Deadband (controller_profile.py, models/*/protocol.py),
# cyclone2-linux, and PROTOCOL.md for the Nova 2 Lite's receiver.
EXTRA_IDS = [
    ("T4N2", 0x2106, "receiver, no controller linked", None),
    # the T4N2 DLL also tries these on usage page 0xFF7A (PROTOCOL.md, "Opening the device")
    ("NovaFamily", 0x10ED, "cable", None), ("NovaFamily", 0x10EF, "cable", None),
    # 0x0575: a Cyclone 2 ("GameSir-Cyclone 2"), or an idle G7 Pro 8K receiver ("Gamepad")
    ("C2", 0x0575, "cable", "cyclone"),
    ("G7ProCE", 0x0575, "receiver, no controller linked", "!cyclone"),
    ("G7Pro", 0x109B, "cable", None), ("G7Pro", 0x109C, "receiver", None),
    ("G7Pro", 0x10BA, "cable", None), ("G7Pro", 0x10BB, "receiver", None),
    ("G7Pro", 0x1003, "cable", None), ("G7Pro", 0x1004, "dock", "g7"),
    ("G7Pro", 0x10A7, "cable", None), ("G7Pro", 0x100A, "receiver, starting up", None),
    ("G7Pro", 0x1022, "receiver", None),
    ("G7SE", 0x1010, "cable", None), ("G7SE", 0x1069, "cable", None), ("G7SE", 0x10A0, "cable", None),
    ("G7SE", 0x1082, "cable", "g7"),
    ("Kaleid", 0x1012, "cable", None), ("Kaleid", 0x1086, "cable", None), ("Kaleid", 0x1082, "cable", "!g7"),
]

_LINKS = {"Wired": "cable", "Wireless": "receiver"}


@dataclass
class Interface:
    path: str            # /dev/hidrawN, or hidapi's path on macOS
    usage_page: int | None


@dataclass
class UsbDevice:
    """One GameSir USB device, with its HID interfaces (sysfs or hidapi; nothing is sent)."""
    pid: int
    product: str
    interfaces: list[Interface] = field(default_factory=list)


@dataclass
class Match:
    model: str
    name: str
    link: str
    family: str | None
    support: str
    added: bool = False                   # added on this computer, not in the built-in table


@dataclass
class Recognised:
    pid: int
    product: str
    matches: list[Match]                  # empty: a GameSir device we don't know
    interfaces: list[Interface]

    @property
    def known(self) -> bool:
        return bool(self.matches)

    def as_dict(self) -> dict:
        names = sorted({m.name for m in self.matches})
        best = self.matches[0] if len(names) == 1 else None
        family = best.family if best else None
        return {
            "usb_id": f"3537:{self.pid:04x}", "product": self.product,
            "name": " or ".join(names) if names else None,
            "model": best.model if best else None,
            "link": best.link if best else None,
            "family": family, "family_name": FAMILY_NAMES.get(family),
            "support": best.support if best else ("no" if names else "unknown"),
            "added": bool(best and best.added),
            "elsewhere": [{"name": n, "url": u} for n, u in ELSEWHERE.get(best.model, ())] if best else [],
            "interfaces": [asdict(i) for i in self.interfaces],
            # A GameSir device with a settings interface of a family we speak can be tried.
            "can_add": not (best and best.support in ("settings", "read")) and any(
                i.usage_page in SPOKEN.values() for i in self.interfaces),
        }


# -- the table --------------------------------------------------------------------------

def _builtin() -> list[tuple[str, int, str, str | None]]:
    data = json.loads(resources.files("padsquire").joinpath("data/connect-devices.json").read_text())
    rows = [(m["model"], i["pid"], _LINKS.get(i["mode"], i["mode"].lower()), None)
            for m in data["models"] for i in m["ids"]]
    return rows + EXTRA_IDS


def added_file() -> Path:
    return paths.data_home() / paths.NAME / "added-controllers.json"


def load_added(path: Path | None = None) -> list[dict]:
    """Controllers added on this computer: [{"pid", "model", "name", "family", "link"}]."""
    try:
        data = json.loads((path or added_file()).read_text())
        return [d for d in data.get("controllers", []) if isinstance(d, dict) and "pid" in d]
    except (OSError, ValueError):
        return []


def _product_ok(test: str | None, product: str) -> bool:
    if not test:
        return True
    p = product.lower()
    return test[1:] not in p if test.startswith("!") else test in p


def identify(pid: int, product: str = "", added: list[dict] | None = None) -> list[Match]:
    """Every model this USB ID can be: one, more than one where GameSir reuses an ID, or none."""
    for d in added if added is not None else load_added():
        if d["pid"] == pid:
            return [Match(d.get("model", "added"), d.get("name", f"GameSir 3537:{pid:04x}"),
                          d.get("link", "cable"), d.get("family"), "added", added=True)]
    seen, out = set(), []
    for model, p, link, test in _builtin():
        if p == pid and _product_ok(test, product) and (model, link) not in seen:
            seen.add((model, link))
            out.append(Match(model, NAMES.get(model, model), link, FAMILY.get(model),
                             SUPPORT.get(model, "no")))
    return out


def recognise(dev: UsbDevice, added: list[dict] | None = None) -> Recognised:
    return Recognised(dev.pid, dev.product, identify(dev.pid, dev.product, added), dev.interfaces)


# -- what is plugged in -------------------------------------------------------------------

def usage_page(descriptor: bytes) -> int | None:
    """The first Usage Page item of a HID report descriptor."""
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        size = (0, 1, 2, 4)[prefix & 3]
        if prefix & 0xFC == 0x04:                       # Usage Page (global)
            return int.from_bytes(descriptor[i + 1:i + 1 + size], "little")
        i += 1 + size
    return None


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def scan(sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev")) -> list[UsbDevice]:
    """Every GameSir USB device, from sysfs (hidapi on macOS)."""
    if MACOS:
        return _scan_hidapi()
    base = sys_root / "bus" / "usb" / "devices"
    found = []
    if not base.exists():
        return found
    for dev in sorted(base.iterdir()):
        if ":" in dev.name or _read(dev / "idVendor").lower() != f"{VENDOR_ID:04x}":
            continue
        try:
            pid = int(_read(dev / "idProduct"), 16)
        except ValueError:
            continue
        interfaces = []
        for node in sorted(dev.glob("*:*/*:*:*.*/hidraw/hidraw*")):
            try:
                desc = (node.parent.parent / "report_descriptor").read_bytes()
            except OSError:
                desc = b""
            interfaces.append(Interface(str(dev_root / node.name), usage_page(desc)))
        found.append(UsbDevice(pid, _read(dev / "product"), interfaces))
    return found


def _scan_hidapi(lister=None) -> list[UsbDevice]:
    if lister is None:
        try:
            import hid as hidapi
        except ImportError:
            return []
        lister = hidapi.enumerate
    devices: dict[tuple, UsbDevice] = {}
    for d in lister(VENDOR_ID, 0):
        key = (d["product_id"], d.get("serial_number") or "", d.get("product_string") or "")
        dev = devices.setdefault(key, UsbDevice(d["product_id"], d.get("product_string") or ""))
        path = d["path"].decode() if isinstance(d["path"], bytes) else d["path"]
        if all(i.path != path for i in dev.interfaces):
            dev.interfaces.append(Interface(path, d.get("usage_page")))
    return list(devices.values())


def plugged_in(sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev"),
               added: list[dict] | None = None) -> list[dict]:
    """Every GameSir device plugged in, recognised: what the app's Controller page lists."""
    added = load_added() if added is None else added
    return [recognise(d, added).as_dict() for d in scan(sys_root, dev_root)]
