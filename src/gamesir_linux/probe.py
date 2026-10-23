# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only first-contact probe: collect everything about a connected controller.

USB: reads sysfs only (IDs, interfaces, bound drivers, HID report descriptors,
the disk identity shown in update mode). No USB or SCSI commands are sent.

Bluetooth: scans, dumps the GATT table, reads readable characteristics, and
sends three GCM *queries* (basic info, battery, one dead zone) to learn how
replies arrive. Queries never change controller settings. Nothing is ever sent
to a device in update ("_DFU") mode.

The report deliberately leaves out USB serial numbers and the host's own
Bluetooth address.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, gcm

GAMESIR_VID = "3537"
JIELI_VID = "4c4a"            # JieLi's own VID, used by some update/loader modes
NAME_HINTS = re.compile(r"gamesir|jieli|zhuhai|\bjl\b|ac69|ac63|udisk|uboot|br2[0-9]", re.I)


# -- USB (sysfs) --------------------------------------------------------------------

def _read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return None


def _driver(path: Path) -> str | None:
    link = path / "driver"
    return os.path.basename(os.readlink(link)) if link.is_symlink() else None


def _walk(top: Path, max_depth: int = 8):
    top_depth = len(top.parts)
    for dirpath, dirnames, _ in os.walk(top, followlinks=False):
        if len(Path(dirpath).parts) - top_depth >= max_depth:
            dirnames.clear()
        yield Path(dirpath), dirnames


def _interface(path: Path) -> dict:
    info = {
        "number": _read(path / "bInterfaceNumber"),
        "class": _read(path / "bInterfaceClass"),
        "subclass": _read(path / "bInterfaceSubClass"),
        "protocol": _read(path / "bInterfaceProtocol"),
        "name": _read(path / "interface"),
        "driver": _driver(path),
        "endpoints": [],
        "hid": [],
        "storage": [],
    }
    for ep in sorted(path.glob("ep_*")):
        info["endpoints"].append({
            "address": _read(ep / "bEndpointAddress"),
            "type": _read(ep / "type"),
            "max_packet": _read(ep / "wMaxPacketSize"),
            "interval": _read(ep / "bInterval"),
        })
    for d, dirnames in _walk(path):
        if (d / "report_descriptor").exists() and d.name.count(":") == 2:
            desc = None
            try:
                desc = (d / "report_descriptor").read_bytes().hex()
            except OSError:
                pass
            hidraw = sorted(p.name for p in (d / "hidraw").glob("hidraw*")) if (d / "hidraw").exists() else []
            info["hid"].append({"node": d.name, "hidraw": hidraw, "driver": _driver(d),
                                "report_descriptor": desc})
        if d.name == "block":
            for blk in sorted(d.iterdir()):
                sectors = _read(blk / "size")
                info["storage"].append({
                    "block": blk.name,
                    "vendor": _read(blk / "device" / "vendor"),
                    "model": _read(blk / "device" / "model"),
                    "rev": _read(blk / "device" / "rev"),
                    "bytes": int(sectors) * 512 if sectors and sectors.isdigit() else None,
                    "removable": _read(blk / "removable"),
                })
        if d.name == "scsi_generic":
            for sg in sorted(d.iterdir()):
                info.setdefault("scsi_generic", []).append(sg.name)
    return info


def _is_candidate(vid: str | None, pid: str | None, manufacturer: str | None,
                  product: str | None, extra_ids=()) -> bool:
    if vid in (GAMESIR_VID, JIELI_VID) or (vid, pid) in extra_ids:
        return True
    return bool(NAME_HINTS.search(f"{manufacturer or ''} {product or ''}"))


def probe_usb(sys_root: Path = Path("/sys"), extra_ids=()) -> list[dict]:
    """extra_ids: (vid, pid) pairs to include, e.g. the DualShock 4 identity the dongle takes on."""
    base = sys_root / "bus" / "usb" / "devices"
    out = []
    if not base.exists():
        return out
    for dev in sorted(base.iterdir()):
        if ":" in dev.name or not (dev / "idVendor").exists():
            continue
        vid, pid = _read(dev / "idVendor"), _read(dev / "idProduct")
        manufacturer, product = _read(dev / "manufacturer"), _read(dev / "product")
        if not _is_candidate(vid, pid, manufacturer, product, extra_ids):
            continue
        out.append({
            "path": dev.name,
            "vid": vid,
            "pid": pid,
            "bcd_device": _read(dev / "bcdDevice"),
            "manufacturer": manufacturer,
            "product": product,
            "has_serial": (dev / "serial").exists(),
            "speed_mbps": _read(dev / "speed"),
            "usb_version": _read(dev / "version"),
            "max_power": _read(dev / "bMaxPower"),
            "configuration": _read(dev / "bConfigurationValue"),
            "interfaces": [_interface(i) for i in sorted(dev.glob(f"{dev.name}:*"))],
        })
    return out


def probe_input(proc_root: Path = Path("/proc"), vids=(GAMESIR_VID, JIELI_VID),
                extra_ids=()) -> list[dict]:
    text = _read(proc_root / "bus" / "input" / "devices") or ""
    found = []
    for block in text.split("\n\n"):
        m = re.search(r"Vendor=([0-9a-f]{4}) Product=([0-9a-f]{4})", block)
        if not m or (m.group(1) not in vids and m.groups() not in extra_ids):
            continue
        name = re.search(r'N: Name="([^"]*)"', block)
        handlers = re.search(r"H: Handlers=(.*)", block)
        found.append({"vid": m.group(1), "pid": m.group(2),
                      "name": name.group(1) if name else None,
                      "handlers": handlers.group(1).split() if handlers else []})
    return found


# -- Bluetooth ------------------------------------------------------------------------

READ_ONLY_QUERIES = [
    ("basic_info", gcm.basic_info_query()),
    ("battery", gcm.host_info_query()),
    ("dead_zone_left_inner", gcm.dead_zone_query(gcm.DeadZone.LEFT_STICK_INNER)),
]


async def _scan_gamesir(timeout: float) -> list[dict]:
    import bleak
    found = await bleak.BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for device, adv in found.values():
        name = adv.local_name or device.name
        if not name or not name.lower().startswith("gamesir"):
            continue
        out.append({
            "address": device.address, "name": name, "rssi": adv.rssi,
            "service_uuids": sorted(adv.service_uuids or []),
            "manufacturer_data": {str(k): v.hex() for k, v in (adv.manufacturer_data or {}).items()},
            "service_data": {k: v.hex() for k, v in (adv.service_data or {}).items()},
        })
    return sorted(out, key=lambda d: d["name"])


async def probe_gatt(client, settle: float = 0.3) -> dict:
    """Dump GATT and test how GCM replies arrive. `client` is a connected BleakClient."""
    from . import ble

    report = {"services": [], "reads": {}, "experiments": []}
    for svc in client.services:
        s = {"uuid": str(svc.uuid), "handle": getattr(svc, "handle", None), "characteristics": []}
        for ch in svc.characteristics:
            s["characteristics"].append({
                "uuid": str(ch.uuid), "handle": getattr(ch, "handle", None),
                "properties": sorted(ch.properties),
                "descriptors": [str(d.uuid) for d in getattr(ch, "descriptors", [])],
            })
            # Reading a characteristic changes nothing on the device. Skip the GCM pair:
            # reading those is part of the experiment below.
            if "read" in ch.properties and str(ch.uuid) not in (ble.COMMAND_UUID, ble.DATA_UUID):
                try:
                    data = bytes(await client.read_gatt_char(ch.uuid))
                    report["reads"][str(ch.uuid)] = {"hex": data.hex(), "text": _printable(data)}
                except Exception as e:
                    report["reads"][str(ch.uuid)] = {"error": f"{type(e).__name__}: {e}"}
        report["services"].append(s)

    chars = {c["uuid"]: c for s in report["services"] for c in s["characteristics"]}
    notified: list[tuple[str, bytes]] = []

    def on_notify(uuid):
        return lambda _sender, data: notified.append((uuid, bytes(data)))

    for uuid in (ble.COMMAND_UUID, ble.DATA_UUID):
        c = chars.get(uuid)
        if c and ({"notify", "indicate"} & set(c["properties"])):
            try:
                await client.start_notify(uuid, on_notify(uuid))
            except Exception as e:
                report.setdefault("notify_errors", {})[uuid] = f"{type(e).__name__}: {e}"

    # Only ever write where the app writes (0x865F); 0x8655's purpose is unknown.
    target = ble.COMMAND_UUID
    if target not in chars:
        report["note"] = "GCM command characteristic 0x865F not found"
        return report
    if not {"write", "write-without-response"} & set(chars[target]["properties"]):
        report["note"] = "0x865F is not writable"
        return report
    with_response = "write" in chars[target]["properties"]
    for label, frame in READ_ONLY_QUERIES:
        exp = {"query": label, "wrote_to": target, "sent": frame.hex()}
        notified.clear()
        try:
            await client.write_gatt_char(target, frame, response=with_response)
            await asyncio.sleep(settle)
            exp["notifications"] = [{"from": u, "hex": d.hex()} for u, d in notified]
            for read_uuid in (ble.COMMAND_UUID, ble.DATA_UUID):
                if read_uuid in chars and "read" in chars[read_uuid]["properties"]:
                    data = bytes(await client.read_gatt_char(read_uuid))
                    exp.setdefault("reads", {})[read_uuid] = data.hex()
            exp["verdict"] = _judge(frame, exp)
        except Exception as e:
            exp["error"] = f"{type(e).__name__}: {e}"
        report["experiments"].append(exp)
    return report


def _judge(frame: bytes, exp: dict) -> str:
    """Which delivery path produced a reply that matches the query?"""
    def ok(hexdata):
        data = bytes.fromhex(hexdata)
        if frame == gcm.basic_info_query():
            return len(data) in (19, 20)
        try:
            gcm.verify(frame, data)
            return True
        except gcm.ProtocolError:
            return False
    paths = [f"notify:{n['from'][4:8]}" for n in exp.get("notifications", []) if ok(n["hex"])]
    paths += [f"read:{u[4:8]}" for u, h in exp.get("reads", {}).items() if ok(h)]
    return ", ".join(paths) or "no valid reply"


def _printable(data: bytes) -> str | None:
    text = data.decode("utf-8", "replace").rstrip("\0")
    return text if text and all(c.isprintable() for c in text) else None


async def probe_ble(address: str | None = None, timeout: float = 8.0) -> dict:
    from . import ble
    try:
        import bleak
    except ImportError:
        return {"error": "bleak not installed (pip install 'gamesir-linux[ble]')"}
    try:
        seen = await _scan_gamesir(timeout)
    except FileNotFoundError:
        return {"error": "Bluetooth unavailable: bluetoothd is not running"}
    except Exception as e:
        return {"error": f"scan failed: {type(e).__name__}: {e}"}
    result = {"seen": seen}
    target = address or next((d["address"] for d in seen if ble.is_config_name(d["name"])), None)
    if target is None:
        result["note"] = "no GameSir config identity in range; nothing connected"
        return result
    if any(d["address"] == target and d["name"] == ble.DFU_NAME for d in seen):
        result["note"] = "target is in update (DFU) mode; not connecting"
        return result
    result["connected_to"] = target
    try:
        async with bleak.BleakClient(target, timeout=20.0) as client:
            result.update(await probe_gatt(client))
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


# -- whole report -----------------------------------------------------------------------

def _cmd(*args) -> str | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def system_info() -> dict:
    os_release = _read(Path("/etc/os-release")) or ""
    pretty = re.search(r'^PRETTY_NAME="?([^"\n]*)', os_release, re.M)
    try:
        from importlib.metadata import version
        bleak_version = version("bleak")
    except Exception:
        bleak_version = None
    return {
        "gamesir_linux": __version__,
        "os": pretty.group(1) if pretty else platform.system(),
        "kernel": platform.release(),
        "python": platform.python_version(),
        "bluez": _cmd("bluetoothctl", "--version"),
        "bleak": bleak_version,
    }


def snapshot(label: str, *, usb: bool = True, bluetooth: bool = False,
             address: str | None = None, sys_root: Path = Path("/sys"),
             proc_root: Path = Path("/proc"), extra_ids=()) -> dict:
    snap = {"label": label, "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if usb:
        snap["usb"] = probe_usb(sys_root, extra_ids)
        snap["input"] = probe_input(proc_root, extra_ids=extra_ids)
    if bluetooth:
        snap["bluetooth"] = asyncio.run(probe_ble(address))
    return snap


def new_report() -> dict:
    return {"probe_format": 1, "system": system_info(), "snapshots": []}


def save(report: dict, path: Path) -> None:
    path.write_text(json.dumps(report, indent=2) + "\n")


# -- human summary ---------------------------------------------------------------------

def summarize(snap: dict) -> list[str]:
    lines = [f"[{snap['label']}]"]
    for d in snap.get("usb", []):
        lines.append(f"  USB {d['vid']}:{d['pid']}  {d['manufacturer'] or '?'} / {d['product'] or '?'}"
                     f"  (bcd {d['bcd_device']}, {d['speed_mbps']} Mb/s)")
        for i in d["interfaces"]:
            extra = []
            if i["hid"]:
                extra.append("hid " + ",".join(h for x in i["hid"] for h in x["hidraw"]))
            for s in i["storage"]:
                extra.append(f"disk {s['block']} '{s['vendor']}' '{s['model']}' rev {s['rev']}")
            lines.append(f"    if{i['number']} class {i['class']}/{i['subclass']}/{i['protocol']}"
                         f" driver={i['driver']} {' '.join(extra)}".rstrip())
    for inp in snap.get("input", []):
        lines.append(f"  input: {inp['name']} ({' '.join(inp['handlers'])})")
    if "usb" in snap and not snap["usb"]:
        lines.append("  no GameSir/JieLi USB device found")
    bt = snap.get("bluetooth")
    if bt is not None:
        if bt.get("error"):
            lines.append(f"  Bluetooth error: {bt['error']}")
        for d in bt.get("seen", []):
            lines.append(f"  BLE {d['address']}  {d['name']}  rssi {d['rssi']}")
        if bt.get("note"):
            lines.append(f"  {bt['note']}")
        for e in bt.get("experiments", []):
            lines.append(f"  {e['query']:22} -> {e['wrote_to'][4:8]}: "
                         f"{e.get('verdict') or e.get('error')}")
    return lines
