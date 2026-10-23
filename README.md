# gamesir-linux

Linux-native configuration and firmware tools for GameSir controllers, starting with the
**Nova 2 Lite** (GameSir model code T4N2).

> **Status:** firmware handling is built and tested. Bluetooth configuration is built and tested
> against a simulated controller, but has **never touched a real one**. See
> [PROTOCOL.md](PROTOCOL.md) for the reverse-engineering notes.

## What works today

- **Configure over Bluetooth** (`gamesir-linux ble`): firmware version and battery, dead zones,
  feature toggles (stick swap, D-pad diagonal lock, axis inversion, …), vibration strength,
  auto-sleep, factory reset. Every change is read back to confirm the controller took it.
  `--simulate` runs any command against a simulated controller.

- **Extract firmware** from the official GameSir Connect Windows installer, on Linux.
  No Windows, no Wine, no download from GameSir's servers.
- **Inspect and verify `.ufw` files**: the JieLi container format is decoded, every partition's
  CRC is checked, and the device identity baked into the firmware is read.
- **Plan an update (dry run):** given what the device reports, pick the right firmware and run
  the safety checks (model, region, version chain, controller/dongle pairing).

## Install

```sh
sudo dnf install 7zip          # only needed for `extract`
pip install --user -e '.[ble]'
```

Python 3.11+. `bleak` (for Bluetooth) is the only dependency.

## Use

```sh
# 1. Download "GameSir Connect" for Windows from gamesir.com, then:
gamesir-linux extract ~/Downloads/GameSir\ Connect\ Setup\ 1.14.5.exe

# 2. Inspect a firmware file
gamesir-linux ufw ~/.local/share/gamesir-linux/firmware/T4Nlite2_V182_2026-04-24.ufw

# 3. Dry run: what would happen to a controller on firmware 8101?
gamesir-linux plan --target controller --pid AC695X_TRANS --vid N2LT --version 8101 --dongle-version 2700
```

Until device detection exists, `plan` takes the device's details by hand.

Bluetooth configuration: switch the controller to Bluetooth mode and pair it, then:

```sh
gamesir-linux ble scan                              # find it
gamesir-linux ble info                              # firmware, battery, MAC
gamesir-linux ble deadzone                          # list all dead zones
gamesir-linux ble deadzone left-stick-inner 3       # set one
gamesir-linux ble feature swap-sticks on
gamesir-linux ble vibration --left-grip 5 --right-grip 5
gamesir-linux ble --simulate info                   # try it without a controller
```

Settings are stored on the controller, so (if that holds on real hardware) you can configure
over Bluetooth and then switch back to the 2.4 GHz dongle.

## Tests

```sh
pytest                                              # synthetic firmware only
GAMESIR_INSTALLER=path/to/setup.exe pytest          # also verifies a real installer
```

The repo contains no GameSir files. Unit tests build synthetic `.ufw` files with the same
structure; the integration test runs against an installer you provide.

## Roadmap

1. ~~Firmware extraction, `.ufw` parser, safety gate~~
2. ~~Bluetooth config CLI (GCM protocol over BLE, via `bleak`), tested against a simulator~~
   Next: button remapping and macros
3. USB update mode: device detection and SCSI transport via `SG_IO`
4. First real hardware: confirm USB IDs, GATT roles, flash dump, then the first update
5. Configuration through the 2.4 GHz dongle (needs the Windows DLL's HID protocol)

## Disclaimer

Unofficial and not affiliated with GameSir. Flashing firmware can brick hardware; the safety
checks reduce that risk but cannot remove it.
