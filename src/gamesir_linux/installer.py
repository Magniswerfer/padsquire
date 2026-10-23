"""Extract firmware and its metadata from a GameSir Connect Windows installer.

GameSir Connect is an Electron app packed in an NSIS installer. Firmware lives
as plain files under resources/dfu/packages/; the version table lives in the
app's JavaScript inside resources/app.asar. Unpacking the NSIS layer needs a
7-Zip binary (Fedora/Nobara: `sudo dnf install 7zip`); everything else is
pure Python.

Nothing from GameSir is redistributed by this project: users point the tool
at an installer they downloaded themselves.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from . import ufw

MAIN_JS = "dist/electron/main.js"
_SEVEN_ZIP_NAMES = ("7zz", "7z", "7za")


class ExtractError(RuntimeError):
    pass


# -- asar ---------------------------------------------------------------------

def read_asar(path: Path) -> tuple[dict, int]:
    """Return (header tree, data offset) of an Electron asar archive."""
    with open(path, "rb") as f:
        head = f.read(16)
        if len(head) < 16:
            raise ExtractError("app.asar is truncated")
        _, header_size, _, json_len = struct.unpack("<4I", head)
        tree = json.loads(f.read(json_len))
    return tree, 8 + header_size


def asar_file(path: Path, member: str) -> bytes:
    tree, base = read_asar(path)
    node = tree
    for part in member.split("/"):
        try:
            node = node["files"][part]
        except KeyError:
            raise ExtractError(f"{member} not found in {path.name}") from None
    if node.get("unpacked"):
        return (path.parent / (path.name + ".unpacked") / member).read_bytes()
    with open(path, "rb") as f:
        f.seek(base + int(node["offset"]))
        return f.read(node["size"])


# -- firmware table -------------------------------------------------------------

_FW_OBJECT = re.compile(r'\{[^{}]*?Path:"packages/[^"]+\.ufw"[^{}]*\}')
_FW_FIELD = re.compile(r'(\w+):"([^"]*)"')


def firmware_table(main_js: str) -> list[dict[str, str]]:
    """Every firmware object literal that has an Identifier, e.g.
    {Type:"International", Version:"8201", Path:"packages/...ufw", MinimumVersion:"5601",
     Identifier:"T4N2International"}."""
    out, seen = [], set()
    for m in _FW_OBJECT.finditer(main_js):
        fields = dict(_FW_FIELD.findall(m.group(0)))
        key = (fields.get("Identifier"), fields.get("Path"))
        if fields.get("Identifier") and key not in seen:
            seen.add(key)
            out.append(fields)
    return out


# -- manifest -------------------------------------------------------------------

@dataclass
class FirmwareInfo:
    file: str
    sha256: str
    identifier: str          # GameSir's identifier, e.g. "T4N2International"
    target: str              # "controller" or "dongle"
    region: str | None       # "international", "china" or None (dongles)
    version: int
    minimum_version: int | None
    chip: str
    jl_pid: str | None
    jl_vid: str | None


def _region(fw: dict[str, str]) -> str | None:
    return {"International": "international", "Internal": "china"}.get(fw.get("Type", ""))


def _target(identifier: str) -> str:
    return "dongle" if identifier.lower().endswith("dongle") else "controller"


def find_7zip() -> str:
    for name in _SEVEN_ZIP_NAMES:
        exe = shutil.which(name)
        if exe:
            return exe
    raise ExtractError("7-Zip not found. On Fedora/Nobara: sudo dnf install 7zip")


def _unpack(seven: str, archive: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [seven, "x", "-y", f"-o{out}", str(archive),
         "resources/app.asar", "resources/dfu/packages/*", "$PLUGINSDIR/app-*.7z"],
        capture_output=True, text=True,
    )


def extract(installer: str | Path, out_dir: str | Path, model: str = "T4N2") -> dict:
    """Unpack `installer`, copy the firmware for `model` into `out_dir`, and
    write out_dir/manifest.json. Returns the manifest."""
    installer, out_dir = Path(installer), Path(out_dir)
    seven = find_7zip()
    with tempfile.TemporaryDirectory(prefix="gamesir-") as tmp:
        tmp = Path(tmp)
        proc = _unpack(seven, installer, tmp)
        if not (tmp / "resources" / "app.asar").exists():
            # Since 1.16.7 the app sits in a nested archive, $PLUGINSDIR/app-32.7z (or app-64.7z).
            inner = sorted((tmp / "$PLUGINSDIR").glob("app-*.7z"))
            if inner:
                proc = _unpack(seven, inner[-1], tmp)
        asar = tmp / "resources" / "app.asar"
        if proc.returncode != 0 or not asar.exists():
            raise ExtractError(
                "could not unpack the installer; is it a GameSir Connect setup .exe?\n"
                + proc.stderr.strip()[-500:]
            )
        app_version = json.loads(asar_file(asar, "package.json")).get("version")
        table = firmware_table(asar_file(asar, MAIN_JS).decode("utf-8", "replace"))
        wanted = [fw for fw in table if fw["Identifier"].startswith(model)]
        if not wanted:
            raise ExtractError(f"installer has no firmware for model {model}")

        out_dir.mkdir(parents=True, exist_ok=True)
        infos = []
        for fw in wanted:
            src = tmp / "resources" / "dfu" / fw["Path"]
            if not src.exists():
                raise ExtractError(f"{fw['Path']} is listed but missing from the installer")
            data = src.read_bytes()
            parsed = ufw.parse(data)
            problems = parsed.problems()
            if problems:
                raise ExtractError(f"{src.name} failed verification: {'; '.join(problems)}")
            dst = out_dir / src.name
            dst.write_bytes(data)
            infos.append(FirmwareInfo(
                file=src.name,
                sha256=hashlib.sha256(data).hexdigest(),
                identifier=fw["Identifier"],
                target=_target(fw["Identifier"]),
                region=_region(fw) if _target(fw["Identifier"]) == "controller" else None,
                version=int(fw["Version"]),
                minimum_version=int(fw["MinimumVersion"]) if fw.get("MinimumVersion") else None,
                chip=parsed.chip,
                jl_pid=parsed.jl_pid,
                jl_vid=parsed.jl_vid,
            ))

    manifest = {
        "source": installer.name,
        "gamesir_connect_version": app_version,
        "model": model,
        "firmware": [asdict(i) for i in infos],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def load_manifest(fw_dir: str | Path) -> list[FirmwareInfo]:
    data = json.loads((Path(fw_dir) / "manifest.json").read_text())
    return [FirmwareInfo(**fw) for fw in data["firmware"]]
