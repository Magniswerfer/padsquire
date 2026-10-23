# gamesir-linux

Linux-native configuration and firmware tools for GameSir controllers, starting with the
**Nova 2 Lite** (GameSir model code T4N2). Unofficial, built by reverse-engineering GameSir
Connect (Windows) and the GameSir Android app.

> **Status:** configuring the controller **over the USB cable works on real hardware** (Nova 2
> Lite, firmware 7701): reading every setting, changing them, and live stick and trigger
> feedback. Bluetooth and the 2.4 GHz dongle are built but not yet tested on hardware. Firmware
> can be downloaded, extracted and verified, but never flashed. Reverse-engineering notes are in
> [PROTOCOL.md](PROTOCOL.md); the hardware checklist is [HARDWARE.md](HARDWARE.md).

![The settings app, Sticks page](docs/screenshot.png)

## What works

| | Cable | Bluetooth | 2.4 GHz dongle |
|---|---|---|---|
| Firmware version, MAC, battery | yes | untested | not yet |
| Read dead zones, feature toggles, vibration, Home light, quick triggers, back buttons | yes | untested | not yet |
| Change them | Home light confirmed, the rest untested | untested | not yet |
| Live stick and trigger positions | yes | no | not yet |
| Auto-sleep, profiles | no (the controller doesn't answer over USB) | untested | not yet |

- **Settings app in your browser** (`gamesir-linux gui`): the controller drawing is the map; click
  a stick, a trigger or the back buttons to configure them. Dead zones are drawn as you drag, with
  the live stick or trigger position on top, so you can see the effect straight away. A button
  guide labels every control and lists the modes, lights and shortcuts. `--simulate` lets you try
  it without a controller.
- **Command line** (`gamesir-linux usb …` and `gamesir-linux ble …`): the same settings, by cable
  or over Bluetooth. Every change is read back to confirm the controller took it.
- **Firmware feed** (`gamesir-linux update`): follows GameSir Connect's own update feed, verifies
  each release's SHA-512 and extracts the firmware. A systemd timer can run it weekly.
- **Firmware tools**: extract firmware from a GameSir Connect installer (no Windows, no Wine),
  decode and verify `.ufw` files, and dry-run an update through the safety checks.
- **Probe** (`gamesir-linux probe`): a read-only report of how the controller appears in each mode,
  for bring-up and bug reports.

## Install

```sh
git clone https://github.com/Magniswerfer/gamesir-linux && cd gamesir-linux
python3 -m venv .venv && .venv/bin/pip install -e '.[gui]'
ln -s "$PWD/.venv/bin/gamesir-linux" ~/.local/bin/      # put it on your PATH
```

Python 3.11 or newer. The cable commands need nothing beyond Python; Bluetooth needs `bleak`
(the `ble` extra) and the settings app needs `aiohttp` (the `gui` extra, which includes both).
`update` and `extract` need 7-Zip (`sudo dnf install 7zip`, or `p7zip` elsewhere).

To use the controller without `sudo`, install the udev rules:

```sh
sudo cp contrib/udev/70-gamesir.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

For an app-menu entry: `cp contrib/desktop/gamesir-linux.desktop ~/.local/share/applications/`.

## Connect the controller

**By cable** (recommended, and the only connection tested so far): plug it in and hold **View +
Menu** for 2 seconds until the Home light turns **green** (PC/Xbox mode). In the blue mode it
pretends to be a PlayStation controller and can't be configured.

**Over Bluetooth**: hold **Home + A** for 2 seconds until the light flashes **yellow** (Android
mode), then pair it.

## The settings app

```sh
gamesir-linux gui                # opens in your default browser
gamesir-linux gui --simulate     # try it without a controller
```

Click **Find controllers**: it lists the controller on the cable and any in Bluetooth range.

The app runs a small server on `127.0.0.1` only and opens a one-time link. The link sets a session
cookie; the server rejects requests without it, requests for any other host name (DNS rebinding),
and API calls without a custom header, so other websites open in your browser can't reach your
controller. Fonts are bundled (Barlow, SIL Open Font License), so nothing loads from the internet.

## Command line

```sh
gamesir-linux usb info                              # firmware, battery, MAC
gamesir-linux usb deadzone                          # list all dead zones
gamesir-linux usb deadzone left-stick-inner 3       # set one (percent)
gamesir-linux usb feature swap-sticks on
gamesir-linux usb vibration --left-grip 50 --right-grip 50
```

Swap `usb` for `ble` to do the same over Bluetooth, or add `--simulate` to try any command
without a controller.

Firmware:

```sh
gamesir-linux update                 # fetch the newest firmware (or: update --check)
gamesir-linux extract ~/Downloads/GameSir\ Connect\ Setup\ 1.16.7.exe   # from an installer
gamesir-linux ufw ~/.local/share/gamesir-linux/firmware/T4Nlite2_V182_2026-04-24.ufw

# Dry run: what would happen to a controller on firmware 7701?
gamesir-linux plan --target controller --pid AC695X_TRANS --vid N2LT --version 7701 --dongle-version 2800
```

Automatic weekly firmware checks:

```sh
cp contrib/systemd/gamesir-linux-update.* ~/.config/systemd/user/
systemctl --user enable --now gamesir-linux-update.timer
```

## How it talks to the controller

In green mode the controller shows up as `3537:100F` with a vendor HID interface (usage page
`0xFF7A`). GameSir Connect's Windows DLL talks to it in 64-byte reports, and the commands turned
out to be the same GCM protocol the Android app uses over Bluetooth, so one client drives both.
Details, including the controller's out-of-the-box settings, are in [PROTOCOL.md](PROTOCOL.md).

## Tests

```sh
pytest                                              # synthetic firmware and devices only
GAMESIR_INSTALLER=path/to/setup.exe pytest          # also verifies a real installer
```

The repo contains no GameSir files. Tests build synthetic `.ufw` files, installers and sysfs trees
with the same structure as the real ones, replay replies captured from a real controller, and
serve fake update feeds from a local HTTP server.

## Roadmap

1. ~~Firmware extraction, `.ufw` parser, safety gate~~
2. ~~Bluetooth config CLI (GCM over BLE), tested against a simulator~~
3. ~~Firmware update feed, read-only hardware probe~~
4. ~~Browser-based settings app~~
5. ~~First real hardware: configuration over the USB cable, live input, button guide~~
6. Test the 2.4 GHz dongle (expected as `3537:1098`) and Bluetooth on hardware
7. Remap every button and record macros (`f5` / `f6` commands, still to decode)
8. USB update mode: device detection and flash dump (`gamesir-linux uboot`, first backup taken
   on hardware), then a first supervised update

## Disclaimer

Unofficial and not affiliated with GameSir. Flashing firmware can brick hardware; there is
deliberately no flash command yet, and when there is, the safety checks will reduce that risk but
cannot remove it.
