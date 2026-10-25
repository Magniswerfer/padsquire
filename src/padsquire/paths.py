# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where PadSquire keeps its files, and the one-time move from the old name's folders.

Until October 2026 the project was called gamesir-linux, and kept firmware and flash backups in
~/.local/share/gamesir-linux and the loader cache in ~/.cache/gamesir-linux. Backups hold the
controller's firmware, Bluetooth pairing and calibration, so `migrate()` moves those folders over
without ever overwriting or dropping a file.
"""

from __future__ import annotations

import os
from pathlib import Path

NAME = "padsquire"
OLD_NAME = "gamesir-linux"


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


DATA_DIR = data_home() / NAME
FIRMWARE_DIR = DATA_DIR / "firmware"
BACKUP_DIR = DATA_DIR / "backups"
PROFILES_DIR = DATA_DIR / "profiles"
LOADER_CACHE = cache_home() / NAME / "loaders"
# the app window's own browser profile: its size and nothing else worth keeping
BROWSER_PROFILE = cache_home() / NAME / "window"


def _sudo_owner() -> tuple[int, int] | None:
    """The user who ran sudo, when we run as root through it (macOS update mode needs root)."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return None
    try:
        return int(os.environ["SUDO_UID"]), int(os.environ["SUDO_GID"])
    except (KeyError, ValueError):
        return None


def give_back(path: Path) -> Path:
    """Under sudo, hand a file or folder we made to the user who ran sudo, so that their own
    PadSquire can read it afterwards. Otherwise nothing."""
    owner = _sudo_owner()
    if owner is not None:
        os.chown(path, *owner)
    return path


def make_dirs(path: Path) -> Path:
    """mkdir -p, giving every folder it creates back to the sudo user (see give_back)."""
    missing = [p for p in [path, *path.parents] if not p.exists()]
    path.mkdir(parents=True, exist_ok=True)
    for p in reversed(missing):
        give_back(p)
    return path


def _merge(old: Path, new: Path, moved: list[Path], left: list[Path]) -> None:
    """Move everything in `old` that `new` doesn't have; keep clashes in `old`."""
    for child in sorted(old.iterdir()):
        target = new / child.name
        if not target.exists() and not target.is_symlink():
            child.rename(target)
            moved.append(child)
        elif child.is_dir() and not child.is_symlink() and target.is_dir() and not target.is_symlink():
            _merge(child, target, moved, left)
        else:
            left.append(child)
    try:
        old.rmdir()                       # only when empty
    except OSError:
        pass


def _move(old: Path, new: Path) -> list[str]:
    if not old.is_dir() or old.is_symlink():
        return []
    if new.exists() and new.resolve() == old.resolve():
        return []                         # new is already a link to the old folder
    if not new.exists() and not new.is_symlink():
        new.parent.mkdir(parents=True, exist_ok=True)
        try:
            old.rename(new)
            return [f"Moved {old} to {new}."]
        except OSError as e:
            try:                          # can't move it: keep using it through a link
                new.symlink_to(old, target_is_directory=True)
                return [f"Couldn't move {old} to {new} ({e.strerror}), so {new} now links to it."]
            except OSError:
                return [f"Couldn't move {old} to {new} ({e.strerror}). Your files are still in {old}: "
                        f"move them to {new} by hand."]
    moved: list[Path] = []
    left: list[Path] = []
    _merge(old, new, moved, left)
    msg = [f"Moved {len(moved)} file(s) or folder(s) from {old} into {new}."] if moved else []
    if left:
        msg.append(f"Left in {old}, because {new} already has a file or folder with the same name: "
                   + ", ".join(str(p.relative_to(old)) for p in left) + ". Nothing was overwritten.")
    return msg


def migrate(data: Path | None = None, cache: Path | None = None) -> list[str]:
    """Move the old gamesir-linux folders to the PadSquire ones. Returns what happened, for the user;
    empty when there was nothing to do (so the message is shown once)."""
    data, cache = data or data_home(), cache or cache_home()
    return _move(data / OLD_NAME, data / NAME) + _move(cache / OLD_NAME, cache / NAME)
