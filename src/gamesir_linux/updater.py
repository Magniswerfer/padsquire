"""Keep extracted firmware current by following GameSir Connect's own update feed.

GameSir has no standalone firmware feed (the app has one, but it is disabled and
never called), so new firmware only ships inside new GameSir Connect releases.
Those are announced at an electron-updater "generic" feed:

    https://pc-connect-update.gamesir-cdn.com/latest.yml

    version: 1.14.5
    files:
      - url: GameSir Connect Setup 1.14.5.exe
        sha512: <base64>
        size: 144813640
    path: GameSir Connect Setup 1.14.5.exe
    sha512: <base64>
    releaseDate: '2026-07-06T...'

The installer itself lives at <base>/<version>/<file> (that is where the 1.14.5
download link on gamesir.com points); the app also tries <base>/releases/<version>/
and <base>/<file>, so we do too.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__, installer

FEED_URL = "https://pc-connect-update.gamesir-cdn.com/latest.yml"
USER_AGENT = f"gamesir-linux/{__version__} (+https://github.com/Magniswerfer/gamesir-linux)"
TIMEOUT = 30


class UpdateError(RuntimeError):
    pass


@dataclass
class Release:
    version: str
    file: str
    sha512: str          # base64, as electron-builder writes it
    size: int | None
    release_date: str | None


# -- feed parsing ------------------------------------------------------------------

def _scalar(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def parse_feed(text: str) -> Release:
    """Parse the small, fixed subset of YAML electron-builder emits."""
    top: dict[str, str] = {}
    files: list[dict[str, str]] = []
    section = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        line = raw.strip()
        if indent == 0:
            key, _, value = line.partition(":")
            section = key if not value.strip() else None
            if value.strip():
                top[key] = _scalar(value)
        elif section == "files":
            if line.startswith("- "):
                files.append({})
                line = line[2:]
            if files and ":" in line:
                key, _, value = line.partition(":")
                files[-1][key.strip()] = _scalar(value)

    version = top.get("version")
    if not version:
        raise UpdateError("update feed has no version")
    exe = next((f for f in files if f.get("url", "").lower().endswith(".exe")), None)
    file = (exe or {}).get("url") or top.get("path")
    sha512 = (exe or {}).get("sha512") or top.get("sha512")
    if not file or not sha512:
        raise UpdateError("update feed does not name an installer and its checksum")
    size = (exe or {}).get("size")
    return Release(version, file, sha512, int(size) if size and size.isdigit() else None,
                   top.get("releaseDate"))


def version_key(v: str | None) -> tuple:
    parts = []
    for p in (v or "0").split("."):
        digits = "".join(c for c in p if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def candidate_urls(feed_url: str, release: Release) -> list[str]:
    base = feed_url.rsplit("/", 1)[0] + "/"
    name = urllib.parse.quote(release.file)
    if "/" in release.file:
        return [urllib.parse.urljoin(base, release.file)]
    return [f"{base}{release.version}/{name}", f"{base}releases/{release.version}/{name}", f"{base}{name}"]


# -- network -----------------------------------------------------------------------

def _open(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def fetch_release(feed_url: str = FEED_URL) -> Release:
    try:
        with _open(feed_url) as r:
            return parse_feed(r.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        raise UpdateError(f"could not read the update feed {feed_url}: {e}") from e


def download(release: Release, feed_url: str, dest: Path, progress=None) -> str:
    """Download the installer to `dest`, verifying size and SHA-512. Returns the URL used."""
    want = base64.b64decode(release.sha512)
    errors = []
    for url in candidate_urls(feed_url, release):
        try:
            resp = _open(url)
        except urllib.error.HTTPError as e:
            errors.append(f"{url}: HTTP {e.code}")
            continue
        except urllib.error.URLError as e:
            errors.append(f"{url}: {e.reason}")
            continue
        h, done = hashlib.sha512(), 0
        with resp, open(dest, "wb") as out:
            while chunk := resp.read(1 << 20):
                h.update(chunk)
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, release.size)
        if release.size is not None and done != release.size:
            raise UpdateError(f"download is {done} bytes, feed says {release.size}")
        if h.digest() != want:
            raise UpdateError("downloaded installer does not match the feed's SHA-512; refusing it")
        return url
    raise UpdateError("installer not found at any known location:\n  " + "\n  ".join(errors))


# -- the update itself --------------------------------------------------------------

def installed_version(fw_dir: Path) -> str | None:
    try:
        return json.loads((fw_dir / "manifest.json").read_text()).get("gamesir_connect_version")
    except (OSError, ValueError):
        return None


@dataclass
class UpdateResult:
    current: str | None
    latest: str
    updated: bool
    manifest: dict | None = None
    url: str | None = None


def update(fw_dir: Path, *, feed_url: str = FEED_URL, check_only: bool = False, force: bool = False,
           keep_installer: Path | None = None, progress=None) -> UpdateResult:
    fw_dir = Path(fw_dir)
    current = installed_version(fw_dir)
    release = fetch_release(feed_url)
    newer = version_key(release.version) > version_key(current)
    result = UpdateResult(current, release.version, False)
    if check_only or not (newer or force):
        return result

    fw_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gamesir-update-", dir=fw_dir.parent) as tmp:
        tmp = Path(tmp)
        exe = tmp / (Path(release.file).name or "setup.exe")
        result.url = download(release, feed_url, exe, progress)
        staged = tmp / "firmware"
        result.manifest = installer.extract(exe, staged)
        if keep_installer:
            shutil.copy2(exe, keep_installer)
        # Swap in the new firmware only once everything above succeeded.
        old = tmp / "previous"
        if fw_dir.exists():
            os.replace(fw_dir, old)
        os.replace(staged, fw_dir)
    result.updated = True
    return result
