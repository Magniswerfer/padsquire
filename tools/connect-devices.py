#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""List every model in a GameSir Connect installer's device registry, grouped by protocol handler.

    tools/connect-devices.py "GameSir Connect Setup 1.16.7.exe" [--json | --registry]

Models that share a handler class speak the same protocol. Reads the installer you point it at;
nothing is copied anywhere. Needs 7-Zip, like `padsquire extract`. DEVICES.md holds the result
for 1.16.7. `--registry` writes what PadSquire ships to recognise controllers:

    tools/connect-devices.py <setup.exe> --registry > src/padsquire/data/connect-devices.json
"""

import argparse
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from padsquire import installer, ufw


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("installer", type=Path)
    ap.add_argument("--json", action="store_true", help="print the raw table as JSON")
    ap.add_argument("--registry", action="store_true",
                    help="print the table PadSquire ships (src/padsquire/data/connect-devices.json)")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="gamesir-") as tmp:
        tmp = Path(tmp)
        asar = installer.unpack(args.installer, tmp)
        js = installer.asar_file(asar, installer.MAIN_JS).decode("utf-8", "replace")
        version = json.loads(installer.asar_file(asar, "package.json")).get("version")
        chips = {}
        for f in sorted((tmp / "resources" / "dfu" / "packages").glob("*.ufw")):
            try:
                chips[f.name] = ufw.parse(f.read_bytes()).chip
            except ufw.UfwError:
                chips[f.name] = "?"
    table, dlls = installer.device_table(js), installer.dll_pids(js)
    if args.registry:
        models = [{"model": d["model"], "name": d["name"], "udisk": d["udisk"],
                   "ids": [{"pid": m["pid"], "mode": m["mode"], "usage_page": m.get("usage_page")}
                           for m in d["modes"]]}
                  for d in table if d["modes"]]
        print(json.dumps({"source": f"GameSir Connect {version}", "models": models}, indent=1))
        return 0
    if args.json:
        print(json.dumps({"connect": version, "devices": table, "dll_pids": dlls, "ufw_chips": chips},
                         indent=1))
        return 0
    by_handler = defaultdict(list)
    for d in table:
        by_handler[d["handler"] or "-"].append(d)
    print(f"GameSir Connect {version}")
    for dll, pids in dlls.items():
        print(f"  {dll}: " + ", ".join(f"{p:#06x}" for p in pids))
    for handler, devs in by_handler.items():
        print(f"\nhandler {handler}")
        for d in devs:
            ident = " ".join(x for x in (d["udisk"], d["udisk_version"]) if x)
            print(f"  {d['model']:<16} {d['name']:<38} {ident}")
            for m in d["modes"]:
                page = m.get("usage_page")
                page = f"{page & 0xFFFF:#06x}" if isinstance(page, int) else "?"
                extra = "DLL" if m.get("dll") == "true" else ""
                print(f"      {m['mode']:<9} 3537:{m['pid']:04x} usage page {page} {extra} {m.get('udisk') or ''}")
    print("\nfirmware chips")
    for name, chip in chips.items():
        print(f"  {chip:<8} {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
