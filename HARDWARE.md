# Testing on hardware

How to check PadSquire against a real controller, and what to report. For everyday use, start
the settings app (`padsquire gui`) and follow its Setup and help page instead.

## The probe

```sh
padsquire probe --guided
```

It walks you through each connection mode (cable in each colour, the 2.4 GHz receiver, update
mode, Bluetooth) and saves a `gamesir-probe-*.json` report of how the controller appears in each:
USB IDs, which driver Linux binds, the HID layout of the configuration interface, and over
Bluetooth the service map and whether replies are read back or notified. **Attach that file to a
bug report.** The probe never changes a setting; over Bluetooth it sends only three read queries.

Report what you test with the "Hardware test" issue template. Before you start, read the ground
rules and "How sure it is" in [CONTRIBUTING.md](CONTRIBUTING.md).

## Still to test: Bluetooth

Everything else has been tested on a Nova 2 Lite (firmware 7701 and 8201) and its receiver (2800).
Over Bluetooth (Android mode: hold Home + A until the light flashes yellow, then pair; if
`padsquire ble scan` finds nothing, pair once with `bluetoothctl` and retry):

1. **Do replies come back?** `padsquire ble info`. If it times out, run the probe: when
   `bluetooth.experiments[].verdict` says `notify:865f`, set `NOTIFY_REPLIES = True` in
   `src/padsquire/ble.py`.
2. **The basic info and battery layout** over Bluetooth (by cable, the battery's bit 7 is not a
   charging flag; see `gcm.Battery`).
3. **The six settings the controller refuses by cable or receiver**: low battery warning, play
   while charging, swap sticks, invert sticks X/Y, swap LB/LT, swap RB/RT. Do they change over
   Bluetooth? (`padsquire ble feature play-while-charging on`, then read it back.)
4. **The anti-dead zone.** By cable and receiver it is stored but has no effect on the stick
   output. Does it work over Bluetooth, or in another mode?
5. **Auto-sleep and profiles**, which get no answer by cable.
6. **Button remapping.** Over Bluetooth the app uses the Android app's back-button command; does
   the cable's full remapping (`f1`) work there too?
7. **Do settings carry over?** Set something you can feel over Bluetooth, switch to the receiver,
   and check it there.

Anything you change, set it back afterwards; `padsquire ble restore --yes` resets every
setting to factory defaults.

## Still to test: the Cyclone 2

PadSquire can read a Cyclone 2 but not change it yet; to set one up today, use
[Deadband](https://github.com/broroeror/gamesir-linux-tools). Its protocol is in
[PROTOCOL-CYCLONE2.md](PROTOCOL-CYCLONE2.md), worked out from GameSir Connect and checked against
what Deadband and cyclone2-linux saw on hardware; PadSquire hasn't run it yet.
Everything below only reads. **Don't** try `usb`, `uboot` or a firmware file on it: those are
for the Nova 2 Lite.

1. **How it shows up.** With the cable, then the receiver, in each mode the controller offers:
   `padsquire probe --label c2-cable-xinput` (change the label each time). For the PlayStation
   and Switch modes add `--device 054c:09cc` or `--device 057e:2009`, since those use Sony's and
   Nintendo's IDs. Expected for the settings: `3537:101d` (or `1053`) by cable, `3537:102a` or
   `100b` through the receiver, each with an interface on usage page `0xFFF0`.
2. **Is it found?** `padsquire c2 find` lists it without sending anything. If not, attach the
   probe report.
3. **Does it answer?** `PADSQUIRE_DEBUG=1 padsquire c2 info`: firmware, battery and the active
   profile. The debug log shows every report (replies should start with `10`). Compare the battery with GameSir Connect or the controller's lights.
4. **Do the profiles read right?** `padsquire c2 dump` saves every profile, raw and decoded, to
   a JSON file. Set a few things you can recognise in GameSir Connect on Windows (a profile name,
   a dead zone, a colour), dump again, and check that the decoded values match.
5. Both by cable and through the receiver.

Attach the probe reports, the dumps and the debug output to the hardware test report.

## Firmware updates

Use the app's Firmware page, or the same steps on the command line:

```sh
padsquire uboot enter --confirm         # add --receiver for the 2.4 GHz receiver
padsquire uboot info                    # chip, chip key, flash size
padsquire uboot backup                  # whole flash, read twice and compared
padsquire uboot flash <file.ufw> --backup <backup.bin> --installed-version <version> --confirm
padsquire uboot restart                 # add --receiver for the receiver
```

Update mode needs the `sg` kernel module; the app's Setup and help page explains it. Backups go to
`~/.local/share/padsquire/backups`, readable only by you: they hold the device's Bluetooth
pairing and calibration. Copy them somewhere safe.

For a **controller or receiver the tool hasn't seen before**, go one step at a time and read the
output before the next: update mode and `info` only, then a backup, then a restore of that same
backup (`uboot restore`) to test writing with known-good data, and only then an update. The tool
refuses a layout it doesn't understand.

## If something goes wrong

- **The controller doesn't respond:** hold Home for about 5 seconds to turn it off, turn it
  on, and hold Menu + View until the light is green. The receiver: unplug it and plug it back in.
- **A firmware write failed:** don't restart or unplug. Run the same step again, or write the
  backup back. The device stays in update mode until a write has gone through.
- **Settings in a bad state:** `padsquire usb restore --yes` (or `ble`) resets them to factory
  defaults.
