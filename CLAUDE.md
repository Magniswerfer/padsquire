# gamesir-linux: notes for Claude Code

Linux tools for the GameSir **Nova 2 Lite** (GameSir model code T4N2): CLI + browser GUI for
configuration over Bluetooth LE, plus firmware extraction/verification. Everything was built by
reverse-engineering GameSir Connect 1.14.5 (Windows) and the GameSir Android app 5.8.20, and
tested against a **simulated** controller. The first real hardware session starts now.

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[gui,test]'   # no system pip on this box; 7zip for update/extract
pytest                                   # 100 tests; GAMESIR_INSTALLER=<setup.exe> adds a real-installer test
gamesir-linux probe --guided             # read-only report of the controller in every mode -> JSON
gamesir-linux ble scan | info | deadzone | feature | vibration | sleep | restore
gamesir-linux usb ...                    # same actions over the cable (green LED mode), via hid.py
gamesir-linux gui [--simulate]           # browser app on 127.0.0.1
gamesir-linux update | extract | ufw | plan
```

## Layout

- `gcm.py`: GCM protocol frames (pure functions). `client.py`: NovaClient, write then read back.
- `ble.py`: bleak transport (write 0x865F, wait 60 ms, read 0x865F). `simulator.py`: fake device.
- `hid.py`: USB transport, GCM in 64-byte reports on the `0xFF7A` hidraw interface (confirmed).
- `web/server.py` + `web/static/`: aiohttp GUI (no build step; vanilla JS, strict CSP).
- `ufw.py`, `jlcrypt.py`, `installer.py`, `updater.py`, `safety.py`: firmware side.
- `probe.py`: read-only hardware probe. `PROTOCOL.md`: all reverse-engineering notes.

## First hardware session: what to confirm, in order

Follow HARDWARE.md. Run `gamesir-linux probe --guided` first and read its JSON before changing code.

1. **BLE reply path.** `bluetooth.experiments[].verdict` says `read:865f` or `notify:865f`.
   If notify, set `NOTIFY_REPLIES = True` in `ble.py`. If neither, inspect the raw reads/notifications.
2. **Basic info layout.** Confirmed over USB: the `01 01` reply on the wired `0xFF7A` HID channel
   parses with `gcm.basic_info_parse(reply[:19])` and gives firmware `7701`, as Connect would.
   Still check the BLE reply (20 bytes with `0xC9`?).
3. **Battery encoding.** The bit 7 = charging assumption is in `gcm.Battery`. Over USB, on the
   cable, host info gave `0x52` (bit 7 clear), so the assumption looks wrong. Check over BLE.
4. **Units.** Read over USB: dead zones look like percent 0–100 (trigger back = 95), vibration
   is percent (75 by default; the GUI now uses a 0–100 slider). Still open: is 100 the maximum,
   what trigger "back" means, and auto-sleep (no reply over USB). Original values are in PROTOCOL.md.
5. **Profiles (0x29).** Does the controller answer? The GUI hides the page if not.
6. **Persistence.** Do settings made over Bluetooth survive switching to the 2.4 GHz dongle?
7. **USB IDs.** Partly confirmed: wired green mode is 0x100F with a `0xFF7A` vendor HID channel;
   blue mode emulates a DualShock 4 (054c:09cc; probe it with `--device 054c:09cc`); the idle
   dongle is 0x2106. See PROTOCOL.md. Still open: the dongle in green mode (Connect says 0x1098)
   and update mode (`AC695X_TRANS` / `N2LT`, `AC632N_TRANS` / `N2DG`).

## Rules

- **Never flash firmware.** There is deliberately no flash command. USB update mode comes later,
  after the probe has confirmed what update mode looks like, and with a flash dump first.
- Prefer read-only experiments; when writing a setting, note the original value and restore it.
- Never send anything to a device advertising as `GameSir-Nova 2 Lite_DFU`.
- Mark any newly confirmed or refuted assumption in PROTOCOL.md, and drop the "ASSUMPTION" notes
  in code once confirmed.
- No GameSir files in the repo (`*.ufw`, `*.exe` are gitignored).
- Keep the user-facing copy plain and in sentence case.
