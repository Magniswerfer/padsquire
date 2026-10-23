# gamesir-linux: notes for Claude Code

Linux tools for the GameSir **Nova 2 Lite** (GameSir model code T4N2): a browser settings app (the
main interface) and a CLI, for configuration by cable, through the 2.4 GHz receiver or over
Bluetooth, and firmware updates of the controller and the receiver. Built by reverse-engineering
GameSir Connect 1.14.5/1.16.7 (Windows) and the GameSir Android app 5.8.20. Tested on one
controller (firmware 7701 and 8201) and its receiver (2800); Bluetooth is not yet tested on hardware.

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[gui,test]'   # no system pip on this box; 7zip for update/extract
pytest                                   # 183 tests; GAMESIR_INSTALLER=<setup.exe> adds a real-installer test,
                                         # GAMESIR_BACKUP=<dump.bin> GAMESIR_UFW=<.ufw> a real layout check
gamesir-linux probe --guided             # read-only report of the controller in every mode -> JSON
gamesir-linux ble scan | info | deadzone | feature | vibration | sleep | restore
gamesir-linux usb ...                    # same actions over the cable (green LED mode), via hid.py
gamesir-linux gui [--simulate]           # browser app on 127.0.0.1
gamesir-linux update | extract | ufw | plan
gamesir-linux uboot find | enter --confirm | info | backup | restart   # download mode
gamesir-linux uboot test-write | restore | flash ... --confirm        # writes (need a backup)
```

## Layout

- `gcm.py`: GCM protocol frames (pure functions). `client.py`: NovaClient, write then read back.
- `ble.py`: bleak transport (write 0x865F, wait 60 ms, read 0x865F). `simulator.py`: fake device.
- `hid.py`: USB transport, GCM in 64-byte reports on the `0xFF7A` hidraw interface (confirmed).
- `web/server.py` + `web/static/`: aiohttp GUI (no build step; vanilla JS, strict CSP). Connects
  over BLE or USB; over USB it skips the reads in `USB_UNSUPPORTED` (no reply, 2.2 s timeout each).
- `ufw.py`, `jlcrypt.py`, `installer.py`, `updater.py`, `safety.py`: firmware side.
- `jlboot.py` + `cli_uboot.py`: JieLi USB download mode over SG_IO (BR23 controller, BD19 receiver),
  read-only (an allowlist refuses erase, flash write and the chip-key write). Needs the `sg` module.
- `jlflash.py`: reads a flash layout (file table, app directory) and checks that an image keeps
  every reserved area; `safety.choose_flash_image` picks the image against a backup. `plan`
  refuses without `--backup`.
- `jlwrite.py`: the only code that erases or writes flash (FB01, FB04), inside one window that
  may not touch a reserved area; `burn()` follows JieLi's order. Tests: `tests/test_jlwrite.py`.
- `updatemode.py`: the download-mode steps (enter, backup, restore, flash, restart) shared by
  `cli_uboot.py` and `web/update.py`, which runs them as background jobs for the Firmware page.
- `web/setup.py`: Setup and help checks and `/api/devices` (sysfs polling for plug in/out).
  `web/update.py`: update-mode jobs (controller and receiver), including the restart button.
- `liveinput.py`: sticks, triggers and buttons from the xpad evdev node.
- `probe.py`: read-only hardware probe. `PROTOCOL.md`: all reverse-engineering notes.

## Status and open questions

Confirmed on hardware (details in PROTOCOL.md): every setting by cable and receiver; remapping
(`f1`); the controller's version through the receiver (`f3` state reports); live input via xpad;
firmware backup, restore and update of the controller (BR23) and the receiver (BD19). Quirks the
tools work around: six feature toggles refused by USB (`USB_READONLY_FEATURES`), outer dead zone
above 25% broken (`gcm.OUTER_DEAD_ZONE_MAX`), vibration in steps of 25, no trigger motors.

Open:

1. **Bluetooth on hardware**: the checklist is in HARDWARE.md (reply path, battery bit 7, the six
   refused toggles, auto-sleep and profiles, remapping).
2. **Gamepad output stops after a write**, intermittently, mostly through the receiver; a restart
   clears it (PROTOCOL.md "Open: gamepad output..."). Reproduce with `GAMESIR_DEBUG=1` (logs every
   frame) and an evdev events-per-second monitor; by cable it didn't happen in 19 remaps.
3. **Recovery if the firmware no longer starts**: TBD (PROTOCOL.md section 3, "Recovery").
4. Controller firmware updates through the receiver (JieLi's HID over-the-air updater): research.

## Rules

- **Firmware writes on hardware the tools haven't seen go in stages, each confirmed by the user
  before the next** (decided 2026-10-23; PROTOCOL.md section 3): (1) update mode and identify,
  read-only; (2) full flash backup, read twice and compared, kept outside the repo; (3) a restore
  of that backup; (4) only then an update, through `safety.py` and `jlwrite`, keeping the
  reserved areas (BTIF, EXIF, USERIF, VM). The tools enforce "no write without a verified backup
  of the same device"; keep it that way.
- **Never send `FC 12` (write chip key).** It burns eFuses and is irreversible. Don't implement it.
- Prefer read-only experiments; when writing a setting, note the original value and restore it.
- Never send anything to a device advertising as `GameSir-Nova 2 Lite_DFU`.
- Mark any newly confirmed or refuted assumption in PROTOCOL.md, and drop the "ASSUMPTION" notes
  in code once confirmed.
- No GameSir files in the repo (`*.ufw`, `*.exe` are gitignored).
- Keep the user-facing copy plain and in sentence case.
- The settings app is the product: a new capability gets its GUI path, its Setup and help entry
  and its troubleshooting text, not just a CLI command.
- Every command shown to users explains each part in plain words (what sudo is for, what gets
  written where, how to undo it) and links official documentation (`web/setup.py` pattern).
- Changing Python under `web/` needs the running app restarted; static files are served fresh.
- `pkill -f <pattern>` can match the shell running it: stop processes by PID.
