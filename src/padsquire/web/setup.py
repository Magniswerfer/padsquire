# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-run checks for the settings app: what this computer still needs, and the command for it.

Everything here only looks (files, sysfs, PATH); nothing is changed. The page shows each check
with its command and a copy button.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .. import desktop, hid, installer, jlboot

RULES_NAME = desktop.RULES_NAME
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
    if hid.MACOS:
        name = {"7zip": "sevenzip"}.get(package, package)
        return (f"brew install {name}",
                [f"brew install {name} downloads {package} from Homebrew and installs it for your user. "
                 "Homebrew is the usual way to install command-line tools on a Mac; it needs to be "
                 "installed first.", f"To undo it: brew uninstall {name}"],
                [_doc("Homebrew", "https://brew.sh/"), _doc("brew install", "https://docs.brew.sh/Manpage#install-options-formulacask-")])
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
    source = desktop.rules_path()
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
    source = desktop.rules_path()
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


def _seven_zip_check() -> dict:
    try:
        seven = installer.find_7zip()
    except installer.ExtractError:
        seven = None
    return {"id": "7zip", "title": "7-Zip", "ok": bool(seven), "need": "updates",
            "detail": f"Found ({seven})." if seven else
                      "Needed to unpack GameSir's firmware from GameSir Connect's installer.",
            **_cmd(None if seven else _with(install_command("7zip"),
                "7-Zip unpacks archives. GameSir ships its firmware inside the GameSir Connect installer, "
                "which is one.", _doc("7-Zip", "https://www.7-zip.org/")))}


def _firmware_check(fw_dir: Path) -> dict:
    try:
        bundled = installer.load_manifest(fw_dir)
    except (OSError, KeyError, ValueError):
        bundled = []
    return {"id": "firmware", "title": "Firmware downloaded", "ok": bool(bundled), "need": "updates",
            "detail": f"{len(bundled)} files in {fw_dir}." if bundled else
                      "Use Download firmware on the Firmware page. It fetches GameSir Connect and extracts it.",
            **_cmd(None)}


def checks(fw_dir: Path) -> list[dict]:
    """Each check: id, title, ok, need ("settings", "updates" or "optional"), detail, and for
    anything missing a command with a plain explanation of each part and links to its documentation."""
    if hid.MACOS:
        return _macos_checks(fw_dir)
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

    out += [_seven_zip_check(), _firmware_check(fw_dir)]

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
                       (f"{sys.executable} -m pip install 'padsquire[gui]'",
                        ["This runs pip from PadSquire's own Python environment and installs PadSquire again "
                         "with its gui extras, which include bleak, the Bluetooth library it uses. It only "
                         "changes PadSquire's own environment, not the system. Running the installer again "
                         "does the same."],
                        [_doc("pip install", "https://pip.pypa.io/en/stable/cli/pip_install/"),
                         _doc("bleak", "https://bleak.readthedocs.io/")]))})

    out.append(_device_check(devices, jlboot.find()))
    return out


def _device_check(devices, download=()) -> dict:
    return {"id": "device", "title": "Controller connected", "ok": bool(devices or download),
            "need": "optional",
            "detail": ("Found " + " and ".join("by cable" if d.link == "cable" else "through the 2.4 GHz receiver"
                                               for d in devices) + "." if devices else
                       "In update mode." if download else
                       "Plug in the cable or the 2.4 GHz receiver and hold Menu + View until the light is green."),
            **_cmd(None)}


def _macos_checks(fw_dir: Path) -> list[dict]:
    """On macOS the system's own HID driver serves the settings channel: nothing to set up for
    settings. Update mode asks for the administrator password each session (macos_helper)."""
    out = []
    try:
        import hid as _hidapi  # noqa: F401
        hidapi = True
    except ImportError:
        hidapi = False
    out.append({"id": "hidapi", "title": "USB access (hidapi)", "ok": hidapi, "need": "settings",
                "detail": "Ready. macOS's own driver serves the controller's settings channel; no sudo needed."
                          if hidapi else "The hidapi Python package is missing.",
                **_cmd(None if hidapi else (
                    f"{sys.executable} -m pip install hidapi",
                    ["This runs pip from PadSquire's own Python environment and installs hidapi, the "
                     "library PadSquire uses to talk to USB devices on macOS. It only changes PadSquire's "
                     "own environment, not the system.",
                     f"To undo it: {sys.executable} -m pip uninstall hidapi"],
                    [_doc("pip install", "https://pip.pypa.io/en/stable/cli/pip_install/"),
                     _doc("hidapi", "https://github.com/trezor/cython-hidapi")]))})

    out.append({"id": "play", "title": "Playing games on a Mac", "ok": True, "need": "optional",
                "detail": "In PC/Xbox mode (light green) Steam recognises the controller as an Xbox controller: "
                          "Steam brings its own driver, macOS has none for that mode. For games outside Steam, "
                          "hold Menu + View until the light is blue: macOS supports that mode, a DualShock 4, "
                          "without a driver. Change settings in green; they stay on the controller.",
                **_cmd(None)})

    try:
        import libusb_package  # noqa: F401
        import usb  # noqa: F401
        usb_ok = True
    except ImportError:
        usb_ok = False
    out.append({"id": "updates", "title": "Firmware updates (asks for your password)", "ok": usb_ok,
                "need": "updates",
                "detail": ("Ready. In update mode the controller is a USB disk, and macOS lets only an "
                           "administrator take a disk away from its own disk driver. So switching to update "
                           "mode, and the restart button, ask for your password in macOS's own dialog, once "
                           "per session. A small PadSquire helper then runs as administrator until the "
                           "controller restarts, passing on only PadSquire's update commands. PadSquire "
                           "never sees the password.") if usb_ok else
                          "The pyusb and libusb-package Python packages are missing.",
                **_cmd(None if usb_ok else (
                    f"{sys.executable} -m pip install pyusb libusb-package",
                    ["This runs pip from PadSquire's own Python environment and installs pyusb and "
                     "libusb-package, which PadSquire uses to reach the controller's update mode on macOS. "
                     "It only changes PadSquire's own environment, not the system.",
                     f"To undo it: {sys.executable} -m pip uninstall pyusb libusb-package"],
                    [_doc("pip install", "https://pip.pypa.io/en/stable/cli/pip_install/"),
                     _doc("pyusb", "https://github.com/pyusb/pyusb")]))})
    out += [_seven_zip_check(), _firmware_check(fw_dir)]

    try:
        import bleak  # noqa: F401
        ble = True
    except ImportError:
        ble = False
    out.append({"id": "bluetooth", "title": "Bluetooth", "ok": ble, "need": "optional",
                "detail": "Ready. macOS asks once whether PadSquire's Python may use Bluetooth." if ble else
                          "The bleak Python package is missing: reinstall with the gui extra.",
                **_cmd(None if ble else (
                    f"{sys.executable} -m pip install 'padsquire[gui]'",
                    ["This runs pip from PadSquire's own Python environment and installs PadSquire again "
                     "with its gui extras, which include bleak, the Bluetooth library it uses. It only "
                     "changes PadSquire's own environment, not the system."],
                    [_doc("pip install", "https://pip.pypa.io/en/stable/cli/pip_install/"),
                     _doc("bleak", "https://bleak.readthedocs.io/")]))})

    out.append(_device_check(hid.find()))
    return out


def devices(find_usb=hid.find) -> dict:
    """What is plugged in right now, from sysfs: cheap enough to poll every couple of seconds."""
    return {"usb": [{"address": d.path, "name": "GameSir Nova 2 Lite", "link": "usb",
                     "via": "dongle" if d.pid == 0x1098 else "cable"} for d in find_usb()],
            "download": [{"path": d.path, "chip": d.vendor} for d in jlboot.find()]}
