# SPDX-License-Identifier: AGPL-3.0-or-later
"""Desktop integration: the app-menu entry, opening the settings app in its own window, and the
udev rules file that ships inside the package.

Nothing here needs root. The menu entry and icon go to the user's own folders
(~/.local/share/applications and ~/.local/share/icons), as the freedesktop.org specs describe.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import webbrowser
from importlib.resources import files
from pathlib import Path

from . import paths

DATA = files("padsquire") / "data"
RULES_NAME = "70-gamesir.rules"
ENTRY_NAME = "padsquire.desktop"

# Chromium-based browsers can show a page as its own window, without tabs or address bar.
APP_WINDOW_BROWSERS = ["chromium-browser", "chromium", "google-chrome-stable", "google-chrome",
                       "brave-browser", "brave", "microsoft-edge-stable", "microsoft-edge", "vivaldi-stable",
                       "vivaldi"]


def rules_path() -> Path:
    """The udev rules file inside the installed package (a real file, for `sudo install`)."""
    return Path(str(DATA / RULES_NAME))


def rules_text() -> str:
    return (DATA / RULES_NAME).read_text()


def _entry(exe: str) -> str:
    return f"""[Desktop Entry]
Type=Application
Name=PadSquire
GenericName=Controller settings
Comment=Settings and firmware updates for GameSir controllers
Exec={exe} gui
Icon=padsquire
Terminal=false
StartupNotify=true
Categories=Settings;HardwareSettings;Game;
Keywords=gamesir;nova;controller;gamepad;remap;dead zone;firmware;
"""


def _exe() -> str:
    """The padsquire command to put in the menu entry: an absolute path, so it works even when
    ~/.local/bin isn't on the menu's PATH."""
    me = Path(sys.argv[0])
    found = str(me.absolute()) if me.name == "padsquire" and me.exists() else shutil.which("padsquire")
    if found:
        return f'"{found}"' if " " in found else found
    return f"{sys.executable} -m padsquire"


def entry_file() -> Path:
    return paths.data_home() / "applications" / ENTRY_NAME


def icon_file() -> Path:
    return paths.data_home() / "icons" / "hicolor" / "scalable" / "apps" / "padsquire.svg"


def install_entry() -> list[Path]:
    """Add PadSquire to the app menu. Returns the files written."""
    entry, icon = entry_file(), icon_file()
    for f in (entry, icon):
        f.parent.mkdir(parents=True, exist_ok=True)
    icon.write_bytes((DATA / "padsquire.svg").read_bytes())
    entry.write_text(_entry(_exe()))
    _refresh(entry.parent)
    return [entry, icon]


def remove_entry() -> list[Path]:
    removed = []
    for f in (entry_file(), icon_file()):
        if f.exists():
            f.unlink()
            removed.append(f)
    _refresh(entry_file().parent)
    return removed


def _refresh(applications: Path) -> None:
    """Most desktops notice new entries by themselves; this makes the rest catch up."""
    if shutil.which("update-desktop-database") and applications.is_dir():
        subprocess.run(["update-desktop-database", str(applications)], capture_output=True, check=False)


def open_app(url: str, tab: bool = False) -> str:
    """Open the settings app: in its own window when a Chromium-based browser is installed,
    otherwise in the default browser. Returns how it was opened."""
    if not tab:
        for name in APP_WINDOW_BROWSERS:
            exe = shutil.which(name)
            if exe:
                subprocess.Popen([exe, f"--app={url}"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return f"window ({name})"
    webbrowser.open(url)
    return "browser tab"
