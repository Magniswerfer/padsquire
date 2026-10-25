# PadSquire: notes for Claude Code

Linux and macOS tools for the GameSir **Nova 2 Lite** (GameSir model code T4N2): a browser settings app (the
main interface) and a CLI, for configuration by cable, through the 2.4 GHz receiver or over
Bluetooth, and firmware updates of the controller and the receiver. Built for interoperability
by studying GameSir Connect 1.14.5/1.16.7 (Windows) and the GameSir Android app 5.8.20. Tested on one
controller (firmware 7701 and 8201) and its receiver (2800); Bluetooth is not yet tested on hardware.
Settings and the live view by cable also work on macOS (hidapi, `0xF4` state reports; confirmed
2026-10-25), and firmware backup, restore and update (raw USB, all four stages confirmed
2026-10-25): from the CLI with sudo, and in the app through `macos_helper` (an administrator
helper started via a PadSquire-named osacompile applet and `launchctl asuser`; one-go
`/api/update/run` actions). Steam on macOS owns the Xbox interface (own driver).

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[gui,test]'   # no system pip on this box; 7zip for update/extract
pytest                                   # 303 tests; PADSQUIRE_INSTALLER=<setup.exe> adds a real-installer test,
                                         # PADSQUIRE_BACKUP=<dump.bin> PADSQUIRE_UFW=<.ufw> a real layout check
padsquire probe --guided             # read-only report of the controller in every mode -> JSON
padsquire ble scan | info | deadzone | feature | vibration | sleep | restore
padsquire usb ...                    # same actions over the cable (green LED mode), via hid.py
padsquire gui [--simulate [cable|receiver|bluetooth]] [--tab]   # settings app on 127.0.0.1, own window; stops when closed
padsquire menu [--remove] | udev-rules # app-menu entry; print the packaged udev rules
padsquire update | extract | ufw | plan
padsquire uboot find | enter --confirm | info | backup | restart   # download mode
padsquire uboot test-write | restore | flash ... --confirm        # writes (need a backup)
```

## Layout

- `gcm.py`: GCM protocol frames (pure functions). `client.py`: NovaClient, write then read back.
- `ble.py`: bleak transport (write 0x865F, wait 60 ms, read 0x865F). `simulator.py`: fake device, one class per
  connection (`simulated(link)`), each answering as PROTOCOL.md records for it; keep it in step
  when a finding changes, and add a test to `tests/test_simulator.py`.
- `hid.py`: USB transport, GCM in 64-byte reports on the `0xFF7A` hidraw interface (confirmed).
  On macOS (`hid.MACOS`) it goes through hidapi instead (`HidapiTransport`); the app then hides
  update mode (`/api/meta` `update_mode`) and the Linux setup checks.
- `web/server.py` + `web/static/`: aiohttp GUI (no build step; vanilla JS, strict CSP). Connects
  over BLE or USB; over USB it skips the reads in `USB_UNSUPPORTED` (no reply, 2.2 s timeout each).
  `app.js` + `app.css` are the app's own; `static/ds/` (design system), `pad-model.js` (3D model),
  `vendor/three.js`, `models/`, `img/` and `data/padsquire.svg` are copies from the website repo:
  edit them there, then run `tools/sync-design.sh` (DESIGN.md lives in the website repo).
- `ufw.py`, `jlcrypt.py`, `installer.py`, `updater.py`, `safety.py`: firmware side.
- `jlboot.py` + `cli_uboot.py`: JieLi USB download mode over SG_IO (BR23 controller, BD19 receiver),
  read-only (an allowlist refuses erase, flash write and the chip-key write). Needs the `sg` module.
- `jlflash.py`: reads a flash layout (file table, app directory) and checks that an image keeps
  every reserved area; `safety.choose_flash_image` picks the image against a backup. `plan`
  refuses without `--backup`.
- `jlwrite.py`: the only code that erases or writes flash (FB01, FB04), inside one window that
  may not touch a reserved area; `burn()` follows JieLi's order. Tests: `tests/test_jlwrite.py`.
- `macos_helper.py`: macOS only. The settings app's administrator helper: forwards the allowed
  SCSI commands over a user-only Unix socket, holds the device for a session, sends an INQUIRY
  keepalive. Tests never start it (`tests/conftest.py` runs the Linux paths everywhere).
- `updatemode.py`: the download-mode steps (enter, backup, restore, flash, restart) shared by
  `cli_uboot.py` and `web/update.py`, which runs them as background jobs for the Firmware page.
- `web/setup.py`: Setup and help checks and `/api/devices` (sysfs polling for plug in/out).
  `web/update.py`: update-mode jobs (controller and receiver), including the restart button.
- `desktop.py`: app-menu entry, own app window, packaged udev rules (`data/`); on macOS the entry
  is `~/Applications/PadSquire.app` (icon rendered from the SVG with qlmanage/sips/iconutil) and
  the window comes from a browser in /Applications. `install.sh`: the curl installer for Linux and
  macOS (own venv in ~/.local/share/padsquire/venv). Releases: push a `v*` tag (PyPI).
- `paths.py`: data and cache folders; moves the folders of the old project name once at startup.
- `liveinput.py`: sticks, triggers and buttons from the xpad evdev node; on macOS from the
  controller's `0xF4` state reports instead (`state_from_report`, read through `HidTransport.live_state`;
  `ReportInput` gives the stick check the same pump/state from them).
- `stickcheck.py`: the Sticks page's check: drift and reach from the live input, with the dead
  zones it measures set to 0 for the test and put back (`zeroed`), and suggested dead zones.
- `settingsfile.py`: the settings file format (JSON, `padsquire-settings` version 1) that profiles
  are kept in: checks the whole file, lists what differs, writes each change with read-back. The
  page has no section of its own for it any more (profiles save to and add from files);
  `/api/settings/export|import` stay.
- `appupdate.py`: PadSquire's own updates (PyPI; the installer's venv updates itself), Help page.
- `profiles.py`: profiles kept on the computer (`paths.PROFILES_DIR`), each a settings file with a
  name; switching loads it like a settings file. The Profiles page; the controller's own slots
  (`0x29`, Bluetooth only, untested) are a section of it.
- `probe.py`: read-only hardware probe. `PROTOCOL.md`: all protocol notes (CC BY-SA 4.0).

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
   clears it (PROTOCOL.md "Open: gamepad output..."). Reproduce with `PADSQUIRE_DEBUG=1` (logs every
   frame) and an evdev events-per-second monitor; by cable it didn't happen in 19 remaps.
3. **Recovery if the firmware no longer starts**: TBD (PROTOCOL.md section 3, "Recovery").
4. Controller firmware updates through the receiver (JieLi's HID over-the-air updater): research.
5. **macOS**: blue (DualShock 4) mode for games untested; receiver and Bluetooth untested there;
   the app's one-go backup, restore and reinstall are confirmed on hardware; the receiver's
   firmware is untested there; the disk-access prompt names "Python". The curl installer from
   padsquire.com installs 0.2.0 from PyPI on a Mac and the app connects (confirmed 2026-10-25).

## Next steps (as of 2026-10-25)

Published: v0.2.0 (the newest: macOS, the compact settings app, profiles, the stick check,
in-app updates; v0.1.0 first) is on PyPI and GitHub Releases (trusted publishing, `pypi` environment), the repo
is public, and `https://padsquire.com/install.sh` redirects to the newest release's `install.sh`
(the website lives in `Magniswerfer/padsquire-website`; edit it only from its own repo). A new
release: bump `version` in `pyproject.toml`, then push a matching `v*` tag.

Confirmed on a Mac: `curl -fsSL https://padsquire.com/install.sh | sh` installs 0.2.0 from PyPI,
and the app opens and connects. Left: the same on Linux, and the in-app update, which can first
be tried with the release after 0.2.0.

Then: Bluetooth testing (the checklist in HARDWARE.md).

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
- The project is PadSquire ("Linux and macOS tools for GameSir controllers"). Use "GameSir" only for what it
  works with, never as part of our own name. Public wording leads with interoperability, not with
  how the protocol was found; technical detail belongs in PROTOCOL.md.
- The settings app is the product: a new capability gets its GUI path, its Setup and help entry
  and its troubleshooting text, not just a CLI command.
- Every command shown to users explains each part in plain words (what sudo is for, what gets
  written where, how to undo it) and links official documentation (`web/setup.py` pattern).
- Changing Python under `web/` needs the running app restarted; static files are served fresh.
- `pkill -f <pattern>` can match the shell running it: stop processes by PID.
