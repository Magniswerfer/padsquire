# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-run checks for the settings app: what this computer still needs, and the command for it.

Everything here only looks (files, sysfs, PATH); nothing is changed. The page shows each check
with its command and a copy button.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .. import hid, installer, jlboot

REPO_ROOT = Path(__file__).resolve().parents[3]
RULES_NAME = "70-gamesir.rules"
RULES_DIRS = [Path("/etc/udev/rules.d"), Path("/usr/lib/udev/rules.d"), Path("/lib/udev/rules.d")]


def _distro() -> set[str]:
    try:
        fields = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    except OSError:
        return set()
    return {w.strip('"').lower() for k in ("ID", "ID_LIKE") for w in fields.get(k, "").split()}


MAN = "https://man7.org/linux/man-pages"


def _doc(title: str, url: str) -> dict:
    return {"title": title, "url": url}


def install_command(package: str) -> tuple[str, list[str], list[dict]]:
    """(command, what it does, documentation) for installing a package on this distribution."""
    ids = _distro()
    sudo = "sudo runs it as the administrator, because installing software changes the system; it asks for your password."
    if ids & {"fedora", "rhel", "centos", "nobara"}:
        return (f"sudo dnf install {package}",
                [sudo, f"dnf install {package} downloads the {package} package from your distribution and installs it."],
                [_doc("dnf install", "https://dnf.readthedocs.io/en/latest/command_ref.html#install-command")])
    if ids & {"debian", "ubuntu"}:
        return (f"sudo apt install {package}",
                [sudo, f"apt install {package} downloads the {package} package from your distribution and installs it."],
                [_doc("apt", "https://manpages.debian.org/stable/apt/apt.8.en.html")])
    if ids & {"arch", "manjaro", "endeavouros", "cachyos"}:
        return (f"sudo pacman -S {package}",
                [sudo, f"pacman -S {package} downloads the {package} package and installs it."],
                [_doc("pacman", "https://wiki.archlinux.org/title/Pacman")])
    if ids & {"opensuse", "suse", "opensuse-tumbleweed", "opensuse-leap"}:
        return (f"sudo zypper install {package}",
                [sudo, f"zypper install {package} downloads the {package} package and installs it."],
                [_doc("zypper", "https://manpages.opensuse.org/Tumbleweed/zypper/zypper.8.en.html")])
    return (f"install the {package} package with your system's package manager", [], [])


def _rules_command() -> tuple[str, list[str], list[dict]]:
    source = REPO_ROOT / "contrib" / "udev" / RULES_NAME
    return (f"sudo install -m 644 {source} /etc/udev/rules.d/ && "
            "sudo udevadm control --reload && sudo udevadm trigger",
            [f"The rules file ({source}) says: devices from GameSir (USB vendor 3537) and JieLi (4c4a, "
             "the controller's chip maker, in update mode) may be used by whoever is logged in at this "
             "computer (the uaccess tag). Nothing else. It's shown below.",
             "sudo runs the three commands as the administrator, because /etc belongs to the system; it asks for your password.",
             "install -m 644 copies the file into /etc/udev/rules.d, readable by everyone, writable only by root.",
             "udevadm control --reload makes udev read its rules again; udevadm trigger applies them to devices "
             "already plugged in.",
             "To undo it: sudo rm /etc/udev/rules.d/70-gamesir.rules"],
            [_doc("udev and rules files", f"{MAN}/man7/udev.7.html"),
             _doc("udevadm", f"{MAN}/man8/udevadm.8.html"), _doc("install", f"{MAN}/man1/install.1.html")])


def _rules_file() -> dict | None:
    source = REPO_ROOT / "contrib" / "udev" / RULES_NAME
    try:
        return {"path": str(source), "content": source.read_text()}
    except OSError:
        return None


def _sg_at_boot() -> bool:
    for conf in Path("/etc/modules-load.d").glob("*.conf"):
        try:
            if any(line.strip() == "sg" for line in conf.read_text().splitlines()):
                return True
        except OSError:
            continue
    return False


def _cmd(c: tuple[str, list[str], list[dict]] | None) -> dict:
    command, explain, docs = c or (None, [], [])
    return {"command": command, "explain": explain, "docs": docs}


def _with(c: tuple[str, list[str], list[dict]], why: str, doc: dict) -> tuple[str, list[str], list[dict]]:
    command, explain, docs = c
    return command, [why, *explain], [doc, *docs]


def checks(fw_dir: Path) -> list[dict]:
    """Each check: id, title, ok, need ("settings", "updates" or "optional"), detail, and for
    anything missing a command with a plain explanation of each part and links to its documentation."""
    out = []
    rules = next((d / RULES_NAME for d in RULES_DIRS if (d / RULES_NAME).exists()), None)
    devices = hid.find()
    blocked = [d.path for d in devices if not os.access(d.path, os.R_OK | os.W_OK)]
    if rules and not blocked:
        detail = "Installed. Controllers can be used without sudo."
    elif rules:
        detail = (f"Installed, but {', '.join(blocked)} isn't accessible yet: unplug the controller "
                  "or receiver and plug it in again.")
    else:
        detail = "Lets your user talk to the controller and the receiver without sudo. One time, then replug."
    out.append({"id": "udev", "title": "Device permissions (udev rules)", "ok": bool(rules) and not blocked,
                "need": "settings", "detail": detail, **_cmd(None if rules else _rules_command()),
                "file": None if rules else _rules_file()})

    sg = Path("/sys/class/scsi_generic").exists()
    boot = _sg_at_boot()
    out.append({"id": "sg", "title": "Update-mode access (sg kernel module)", "ok": sg, "need": "updates",
                "detail": ("Loaded" + ("" if boot else ", but not at boot: the command below makes it permanent")
                           + ".") if sg else
                          "Needed only to back up or update firmware: the controller shows up as a USB disk then.",
                **_cmd(None if sg and boot else (
                    "echo sg | sudo tee /etc/modules-load.d/sg.conf && sudo modprobe sg",
                    ["sg is the kernel's SCSI generic driver. In update mode the controller is a USB disk that "
                     "takes special commands; sg is how a program sends them. It's part of the kernel, nothing is downloaded.",
                     "echo sg | sudo tee /etc/modules-load.d/sg.conf writes one line, sg, to a file that tells "
                     "the system to load that driver at every start (tee writes it as administrator).",
                     "sudo modprobe sg loads it right now, so you don't have to restart.",
                     "To undo it: sudo rm /etc/modules-load.d/sg.conf"],
                    [_doc("modules-load.d", f"{MAN}/man5/modules-load.d.5.html"),
                     _doc("modprobe", f"{MAN}/man8/modprobe.8.html"),
                     _doc("tee", f"{MAN}/man1/tee.1.html"),
                     _doc("The sg driver", "https://tldp.org/HOWTO/SCSI-Generic-HOWTO/")]))})

    try:
        seven = installer.find_7zip()
    except installer.ExtractError:
        seven = None
    out.append({"id": "7zip", "title": "7-Zip", "ok": bool(seven), "need": "updates",
                "detail": f"Found ({seven})." if seven else
                          "Needed to unpack GameSir's firmware from GameSir Connect's installer.",
                **_cmd(None if seven else _with(install_command("7zip"),
                    "7-Zip unpacks archives. GameSir ships its firmware inside the GameSir Connect installer, "
                    "which is one.", _doc("7-Zip", "https://www.7-zip.org/")))})

    try:
        bundled = installer.load_manifest(fw_dir)
    except (OSError, KeyError, ValueError):
        bundled = []
    out.append({"id": "firmware", "title": "Firmware downloaded", "ok": bool(bundled), "need": "updates",
                "detail": f"{len(bundled)} files in {fw_dir}." if bundled else
                          "Use Download firmware on the Firmware page. It fetches GameSir Connect and extracts it.",
                **_cmd(None)})

    try:
        import bleak  # noqa: F401
        ble = True
    except ImportError:
        ble = False
    bluez = bool(shutil.which("bluetoothctl"))
    out.append({"id": "bluetooth", "title": "Bluetooth", "ok": ble and bluez, "need": "optional",
                "detail": "Ready." if ble and bluez else
                          ("BlueZ (bluetoothctl) is missing." if ble else
                           "The bleak Python package is missing: reinstall with the gui extra."),
                **_cmd(None if ble and bluez else
                       _with(install_command("bluez"), "BlueZ is Linux's Bluetooth system.",
                             _doc("BlueZ", "https://github.com/bluez/bluez")) if ble else
                       (".venv/bin/pip install -e '.[gui]'",
                        ["Run it in the padsquire folder. pip installs this app again with its gui extras, "
                         "which include bleak, the Bluetooth library it uses. It only changes the app's own "
                         ".venv folder, not the system."],
                        [_doc("pip install", "https://pip.pypa.io/en/stable/cli/pip_install/"),
                         _doc("bleak", "https://bleak.readthedocs.io/")]))})

    download = jlboot.find()
    out.append({"id": "device", "title": "Controller connected", "ok": bool(devices or download),
                "need": "optional",
                "detail": ("Found " + " and ".join("by cable" if d.link == "cable" else "through the 2.4 GHz receiver"
                                                   for d in devices) + "." if devices else
                           "In update mode." if download else
                           "Plug in the cable or the 2.4 GHz receiver and hold Menu + View until the light is green."),
                **_cmd(None)})
    return out


def devices(find_usb=hid.find) -> dict:
    """What is plugged in right now, from sysfs: cheap enough to poll every couple of seconds."""
    return {"usb": [{"address": d.path, "name": "GameSir Nova 2 Lite", "link": "usb",
                     "via": "dongle" if d.pid == 0x1098 else "cable"} for d in find_usb()],
            "download": [{"path": d.path, "chip": d.vendor} for d in jlboot.find()]}
