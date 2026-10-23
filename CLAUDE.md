# gamesir-linux: notes for Claude Code

Linux tools for the GameSir **Nova 2 Lite** (GameSir model code T4N2): CLI + browser GUI for
configuration over Bluetooth LE, plus firmware extraction/verification. Everything was built by
reverse-engineering GameSir Connect 1.14.5 (Windows) and the GameSir Android app 5.8.20, and
tested against a **simulated** controller. The first real hardware session starts now.

## Commands

```sh
pip install --user -e '.[gui,test]'      # deps: bleak, aiohttp; 7zip needed for update/extract
pytest                                   # 93 tests; GAMESIR_INSTALLER=<setup.exe> adds a real-installer test
gamesir-linux probe --guided             # read-only report of the controller in every mode -> JSON
gamesir-linux ble scan | info | deadzone | feature | vibration | sleep | restore
gamesir-linux gui [--simulate]           # browser app on 127.0.0.1
gamesir-linux update | extract | ufw | plan
```

## Layout

- `gcm.py`: GCM protocol frames (pure functions). `client.py`: NovaClient, write then read back.
- `ble.py`: bleak transport (write 0x865F, wait 60 ms, read 0x865F). `simulator.py`: fake device.
- `web/server.py` + `web/static/`: aiohttp GUI (no build step; vanilla JS, strict CSP).
- `ufw.py`, `jlcrypt.py`, `installer.py`, `updater.py`, `safety.py`: firmware side.
- `probe.py`: read-only hardware probe. `PROTOCOL.md`: all reverse-engineering notes.

## First hardware session: what to confirm, in order

Follow HARDWARE.md. Run `gamesir-linux probe --guided` first and read its JSON before changing code.

1. **BLE reply path.** `bluetooth.experiments[].verdict` says `read:865f` or `notify:865f`.
   If notify, set `NOTIFY_REPLIES = True` in `ble.py`. If neither, inspect the raw reads/notifications.
2. **Basic info layout.** Check `firmware_version` from `ble info` matches what GameSir reports
   (parsed as `hex(b11) + hex(b12)` in `gcm.basic_info_parse`).
3. **Battery encoding.** The bit 7 = charging assumption is in `gcm.Battery`.
4. **Units.** Dead zones are assumed to be percent (GUI shows 0–50), vibration 0–4, auto-sleep minutes.
   Read the current values before writing anything; adjust the GUI ranges in `web/static/app.js`.
5. **Profiles (0x29).** Does the controller answer? The GUI hides the page if not.
6. **Persistence.** Do settings made over Bluetooth survive switching to the 2.4 GHz dongle?
7. **USB IDs.** The probe's `usb[].pid` per mode. Connect's table says wired config PID 0x100F,
   dongle config PID 0x1098; update-mode identity should be `AC695X_TRANS` / `N2LT` (controller)
   and `AC632N_TRANS` / `N2DG` (dongle).

## Rules

- **Never flash firmware.** There is deliberately no flash command. USB update mode comes later,
  after the probe has confirmed what update mode looks like, and with a flash dump first.
- Prefer read-only experiments; when writing a setting, note the original value and restore it.
- Never send anything to a device advertising as `GameSir-Nova 2 Lite_DFU`.
- Mark any newly confirmed or refuted assumption in PROTOCOL.md, and drop the "ASSUMPTION" notes
  in code once confirmed.
- No GameSir files in the repo (`*.ufw`, `*.exe` are gitignored).
- Keep the user-facing copy plain and in sentence case.
