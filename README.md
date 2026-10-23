# gamesir-linux

Linux-native configuration and firmware tools for GameSir controllers, starting with the
**Nova 2 Lite** (GameSir model code T4N2).

> **Status:** firmware handling is built and tested. Bluetooth configuration and the settings app
> are built and tested against a simulated controller, but have **never touched a real one**. Got the hardware? Start
> with [HARDWARE.md](HARDWARE.md). Reverse-engineering notes are in [PROTOCOL.md](PROTOCOL.md).

![The settings app, Sticks page](docs/screenshot.png)

## What works today

- **Settings app in your browser** (`gamesir-linux gui`): everything below in one window, with the
  controller itself as the map: click a stick, a trigger or the back buttons to configure them.
  Back-button mapping, dead zones (drawn live as you drag), quick triggers, vibration, Home light
  colour, profiles, power settings, factory reset and firmware downloads. Works in any browser on
  any distro; `--simulate` lets you try it without a controller.
- **Configure over Bluetooth** (`gamesir-linux ble`): firmware version and battery, dead zones,
  feature toggles (stick swap, D-pad diagonal lock, axis inversion, …), vibration strength,
  auto-sleep, factory reset. Every change is read back to confirm the controller took it.
  `--simulate` runs any command against a simulated controller.
- **Keep firmware current** (`gamesir-linux update`): follows GameSir Connect's own update feed,
  downloads new releases, verifies their SHA-512 and extracts the firmware. A systemd timer can
  run it weekly.
- **Extract firmware** from a GameSir Connect Windows installer you already have, on Linux.
  No Windows, no Wine.
- **Inspect and verify `.ufw` files**: the JieLi container format is decoded, every partition's
  CRC is checked, and the device identity baked into the firmware is read.
- **Plan an update (dry run):** given what the device reports, pick the right firmware and run
  the safety checks (model, region, version chain, controller/dongle pairing).
- **Probe a controller** (`gamesir-linux probe --guided`): read-only report of how it appears
  over USB and Bluetooth in every mode, for bring-up and bug reports.

## Install

```sh
sudo dnf install 7zip          # needed for `update` and `extract`
pip install --user -e '.[gui]'
```

Python 3.11+. Dependencies: `bleak` (Bluetooth) and `aiohttp` (the local web server behind the
GUI). To use the controller without `sudo`, install the udev rules in `contrib/udev/` (see
[HARDWARE.md](HARDWARE.md)). For an app-menu entry:
`cp contrib/desktop/gamesir-linux.desktop ~/.local/share/applications/`.

## The settings app

```sh
gamesir-linux gui                # opens in your default browser
gamesir-linux gui --simulate     # try it without a controller
```

It runs a small server on `127.0.0.1` only and opens a one-time link. The link sets a session
cookie; the server rejects requests without it, requests for any other host name (DNS rebinding),
and API calls without a custom header, so other websites open in your browser can't reach your
controller. Fonts are bundled (Barlow, SIL Open Font License), so nothing loads from the internet.

Over Bluetooth only the back buttons can be remapped, as in GameSir's phone app. Remapping every
button needs the USB/dongle protocol, which isn't supported yet; the app says so where relevant.

## Use

```sh
gamesir-linux update                 # fetch the newest firmware (or: update --check)

# or, from an installer you downloaded yourself:
gamesir-linux extract ~/Downloads/GameSir\ Connect\ Setup\ 1.14.5.exe

gamesir-linux ufw ~/.local/share/gamesir-linux/firmware/T4Nlite2_V182_2026-04-24.ufw

# Dry run: what would happen to a controller on firmware 8101?
gamesir-linux plan --target controller --pid AC695X_TRANS --vid N2LT --version 8101 --dongle-version 2700
```

Until device detection exists, `plan` takes the device's details by hand.

Automatic weekly updates:

```sh
cp contrib/systemd/gamesir-linux-update.* ~/.config/systemd/user/
systemctl --user enable --now gamesir-linux-update.timer
```

Bluetooth configuration: switch the controller to Bluetooth mode, then:

```sh
gamesir-linux ble scan                              # find it
gamesir-linux ble info                              # firmware, battery, MAC
gamesir-linux ble deadzone                          # list all dead zones
gamesir-linux ble deadzone left-stick-inner 3       # set one
gamesir-linux ble feature swap-sticks on
gamesir-linux ble vibration --left-grip 5 --right-grip 5
gamesir-linux ble --simulate info                   # try it without a controller
```

Or over the USB cable: hold Menu + View until the LED is green (PC/Xbox mode), then use the same
commands with `usb` instead of `ble`:

```sh
gamesir-linux usb info
gamesir-linux usb deadzone
```

Settings are stored on the controller, so (if that holds on real hardware) you can configure
over Bluetooth and then switch back to the 2.4 GHz dongle.

## Tests

```sh
pytest                                              # synthetic firmware and devices only
GAMESIR_INSTALLER=path/to/setup.exe pytest          # also verifies a real installer
```

The repo contains no GameSir files. Tests build synthetic `.ufw` files, installers and sysfs trees
with the same structure as the real ones, and serve fake update feeds from a local HTTP server.

## Roadmap

1. ~~Firmware extraction, `.ufw` parser, safety gate~~
2. ~~Bluetooth config CLI (GCM protocol over BLE, via `bleak`), tested against a simulator~~
3. ~~Firmware update feed, read-only hardware probe~~
4. ~~Browser-based settings app~~ (macros still to come)
5. First real hardware: run [HARDWARE.md](HARDWARE.md), fix what reality disagrees with
6. USB update mode: device detection and SCSI transport via `SG_IO`, flash dump, first update
7. Macros over Bluetooth
8. Configuration through the 2.4 GHz dongle (needs the Windows DLL's HID protocol), which also
   unlocks remapping every button

## Disclaimer

Unofficial and not affiliated with GameSir. Flashing firmware can brick hardware; the safety
checks reduce that risk but cannot remove it.
