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
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from . import paths

DATA = files("padsquire") / "data"
RULES_NAME = "70-gamesir.rules"
ENTRY_NAME = "padsquire.desktop"

# Chromium and the browsers built on it can show a page as its own window, without tabs or address
# bar. Each names that window "<prefix>-127.0.0.1__-Default" (its Wayland app ID and X11 class;
# seen on Brave, the others follow Chromium's naming), and ignores --class for it. The menu entry
# repeats that name as StartupWMClass, so a dock or taskbar shows the window as PadSquire.
APP_WINDOW_BROWSERS = [("chromium-browser", "chromium"), ("chromium", "chromium"),
                       ("google-chrome-stable", "chrome"), ("google-chrome", "chrome"),
                       ("brave-browser", "brave"), ("brave", "brave"),
                       ("microsoft-edge-stable", "msedge"), ("microsoft-edge", "msedge"),
                       ("vivaldi-stable", "vivaldi"), ("vivaldi", "vivaldi")]
# The same browsers as Flatpaks (how a Steam Deck installs them), tried when none of the above is
# on PATH.
FLATPAK_BROWSERS = [("org.chromium.Chromium", "chromium"), ("com.google.Chrome", "chrome"),
                    ("com.brave.Browser", "brave"), ("com.microsoft.Edge", "msedge"), ("com.vivaldi.Vivaldi", "vivaldi")]


@dataclass(frozen=True)
class Browser:
    name: str             # for messages
    command: list[str]    # what starts it
    prefix: str           # of the name it gives its app windows
    profile: Path         # the app window's own profile (a Flatpak can only write in its own folder)

    @property
    def window_class(self) -> str:
        return f"{self.prefix}-127.0.0.1__-Default"


def app_browser() -> Browser | None:
    """The browser that opens the settings app in its own window, if there is one."""
    for name, prefix in APP_WINDOW_BROWSERS:
        exe = shutil.which(name)
        if exe:
            return Browser(name, [exe], prefix, paths.BROWSER_PROFILE)
    flatpak = shutil.which("flatpak")
    if flatpak:
        for app_id, prefix in FLATPAK_BROWSERS:
            if subprocess.run([flatpak, "info", app_id], capture_output=True, check=False).returncode == 0:
                return Browser(f"{app_id}, Flatpak", [flatpak, "run", app_id], prefix,
                               Path.home() / ".var" / "app" / app_id / "cache" / "padsquire" / "window")
    return None


def rules_path() -> Path:
    """The udev rules file inside the installed package (a real file, for `sudo install`)."""
    return Path(str(DATA / RULES_NAME))


def rules_text() -> str:
    return (DATA / RULES_NAME).read_text()


def _entry(exe: str, browser: Browser | None) -> str:
    wm_class = f"StartupWMClass={browser.window_class}\n" if browser else ""
    return f"""[Desktop Entry]
Type=Application
Name=PadSquire
GenericName=Controller settings
Comment=Settings and firmware updates for GameSir controllers
Exec={exe} gui
Icon=padsquire
{wm_class}Terminal=false
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
    entry.write_text(_entry(_exe(), app_browser()))
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


WINDOW_SIZE = (1280, 860)      # the first time; a smaller screen (a Steam Deck) gets it clamped


def open_app(url: str, tab: bool = False) -> str:
    """Open the settings app: in its own window when a Chromium-based browser is installed,
    otherwise in the default browser. Returns how it was opened."""
    browser = None if tab else app_browser()
    if browser:
        # Its own small profile: the window starts at WINDOW_SIZE and then keeps the size you give
        # it, instead of a size the browser saved for other 127.0.0.1 windows, and none of your
        # extensions or cookies come along.
        subprocess.Popen([*browser.command, f"--app={url}", f"--user-data-dir={browser.profile}",
                          f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",
                          "--no-first-run", "--no-default-browser-check"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return f"window ({browser.name})"
    webbrowser.open(url)
    return "browser tab"
