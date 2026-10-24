# SPDX-License-Identifier: AGPL-3.0-or-later
"""Profiles: named sets of settings kept on this computer, one settings file each.

A profile is a settings file (settingsfile.py) with a "name" added, stored as
<profiles dir>/<id>.json, so any profile can be exported as a settings file and any settings file
imported as a profile. Switching to one is loading it: only what differs is written, each setting
read back. The controller keeps what was written until it is changed, on any device.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from . import settingsfile

NAME_MAX = 60


class ProfileError(ValueError):
    pass


def _clean_name(name) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ProfileError("a profile needs a name")
    name = " ".join(name.split())
    if len(name) > NAME_MAX:
        raise ProfileError(f"a profile name can be at most {NAME_MAX} characters")
    return name


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "profile"


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, pid: str) -> Path:
        if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9-]{1,48}", pid):
            raise ProfileError("no such profile")
        return self.root / f"{pid}.json"

    def _load(self, path: Path) -> dict | None:
        try:
            doc = json.loads(path.read_text())
            settingsfile.parse(doc)
            return doc if isinstance(doc.get("name"), str) else None
        except (OSError, ValueError):
            return None                # unreadable or not a profile: left alone, not listed

    def list(self) -> list[dict]:
        """Every readable profile, sorted by name: its id, name and the document."""
        out = []
        for path in sorted(self.root.glob("*.json")):
            doc = self._load(path)
            if doc is not None:
                out.append({"id": path.stem, "name": doc["name"], "doc": doc})
        return sorted(out, key=lambda p: p["name"].lower())

    def get(self, pid: str) -> dict:
        doc = self._load(self._path(pid))
        if doc is None:
            raise ProfileError("no such profile")
        return doc

    def _taken(self, name: str, but: str | None = None) -> bool:
        return any(p["name"].lower() == name.lower() and p["id"] != but for p in self.list())

    def _write(self, pid: str, doc: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(doc, f, indent=2)
                f.write("\n")
            os.replace(tmp, self._path(pid))      # a crash leaves the old file or the new, never half
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def create(self, name, doc: dict) -> str:
        """Store a settings document (checked first) as a new profile. Returns its id."""
        name = _clean_name(name)
        settingsfile.parse(doc)
        if self._taken(name):
            raise ProfileError(f"there is already a profile called {name}")
        base = _slug(name)
        pid, n = base, 2
        while self._path(pid).exists():
            pid, n = f"{base}-{n}", n + 1
        self._write(pid, {**doc, "name": name})
        return pid

    def replace(self, pid: str, doc: dict) -> None:
        """New settings for a profile, keeping its name."""
        name = self.get(pid)["name"]
        settingsfile.parse(doc)
        self._write(pid, {**doc, "name": name})

    def rename(self, pid: str, name) -> None:
        doc = self.get(pid)
        name = _clean_name(name)
        if self._taken(name, but=pid):
            raise ProfileError(f"there is already a profile called {name}")
        self._write(pid, {**doc, "name": name})

    def delete(self, pid: str) -> None:
        self.get(pid)
        self._path(pid).unlink()
