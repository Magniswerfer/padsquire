# SPDX-License-Identifier: AGPL-3.0-or-later
"""PadSquire's own updates, for the settings app: is there a newer release, and installing it.

Releases go to PyPI (a `v*` tag, see .github/workflows/release.yml), so that is where the newest
version is read: https://pypi.org/pypi/padsquire/json. Asked only when the page asks, never in
the background.

Only an installation made by install.sh (its own Python environment in
~/.local/share/padsquire/venv) is updated in place, with that environment's pip, exactly as
the installer does it. pipx and development installations get the command to run instead.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__, paths

PYPI = "https://pypi.org/pypi/padsquire/json"
INSTALLER_VENV = paths.DATA_DIR / "venv"


class UpdateError(RuntimeError):
    pass


def version_key(text: str) -> tuple:
    """"0.1.10" > "0.1.9"; a pre-release ("0.2.0rc1") sorts below its release ("0.2.0")."""
    m = re.match(r"(\d+(?:\.\d+)*)(.*)", text.strip())
    if not m:
        return (0, 0, 0, 0, text)
    numbers = [int(n) for n in m.group(1).split(".")]
    rest = m.group(2)
    return (*numbers, *([0] * (3 - len(numbers))), 0 if rest else 1, rest)


def newest(url: str = PYPI, timeout: float = 10.0) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)["info"]["version"]
    except (OSError, ValueError, KeyError) as e:
        raise UpdateError(f"couldn't ask PyPI for the newest version: {e}") from e


@dataclass(frozen=True)
class Install:
    kind: str                       # "installer" | "pipx" | "source" | "other"
    command: str | None             # what to run when the app can't update itself


def how_installed(prefix: Path | None = None) -> Install:
    prefix = Path(prefix or sys.prefix).resolve()
    if prefix == INSTALLER_VENV.resolve():
        return Install("installer", None)
    if "pipx" in prefix.parts:
        return Install("pipx", "pipx upgrade padsquire")
    if _editable():
        return Install("source", "git pull")
    return Install("other", f"{sys.executable} -m pip install --upgrade 'padsquire[gui]'")


def _editable() -> bool:
    """Installed from a checkout with pip install -e (direct_url.json says so)."""
    try:
        from importlib.metadata import distribution
        text = distribution("padsquire").read_text("direct_url.json") or "{}"
        return bool(json.loads(text).get("dir_info", {}).get("editable"))
    except Exception:
        return False


def status(url: str = PYPI) -> dict:
    latest = newest(url)
    install = how_installed()
    return {"current": __version__, "latest": latest,
            "newer": version_key(latest) > version_key(__version__),
            "install": install.kind, "command": install.command}


def install(version: str, python: str = sys.executable, run=subprocess.run) -> str:
    """Install exactly `version` into this (the installer's) environment, as install.sh does.
    Returns pip's output. The running app keeps its old code until it restarts."""
    if how_installed().kind != "installer":
        raise UpdateError("this PadSquire wasn't installed with the installer; update it with its own command")
    if not all(c.isalnum() or c in ".-+" for c in version):
        raise UpdateError(f"{version!r} is not a version")
    # --no-cache-dir: pip keeps PyPI's list of versions for a few minutes, which can hide a new one
    done = run([python, "-m", "pip", "install", "--upgrade", "--no-cache-dir", f"padsquire[gui]=={version}"],
               capture_output=True, text=True, timeout=600)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip().splitlines()[-3:]
        raise UpdateError("pip couldn't install it: " + " ".join(tail))
    return done.stdout


def restart() -> None:
    """Start the new version; it finds this one still running and stops it (see web.server.serve)."""
    subprocess.Popen([sys.executable, "-m", "padsquire", "gui"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
