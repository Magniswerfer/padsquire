# PadSquire

**Linux tools for GameSir controllers.** Configure and update the GameSir **Nova 2 Lite** on Linux,
and configure it on macOS, without Windows. Unofficial and open source, built for interoperability by studying how the
controller talks over USB and Bluetooth.

![The settings app, Sticks page](docs/screenshot.png)

## What it does

- **A settings app in your browser** (`padsquire gui`). It finds the controller by itself when
  you plug it in, and shows every setting with the controller drawing as the map. Dead zones are
  drawn as you drag, with the live stick or trigger position on top. On the Buttons page, the
  buttons you press light up as the computer receives them.
- **Every setting GameSir Connect has for this controller.** That covers dead zones, stick
  options, button remapping (19 buttons, each to any other or up to three pressed together), vibration strength with a test per
  motor, the Home light and quick triggers.
- **A stick check** that GameSir's apps don't have. It measures how far each stick drifts when
  left alone and whether it reaches full tilt in every direction, then suggests the inner and
  outer dead zones for your controller, to apply with one click.
- **Profiles**, as many as you like, kept on your computer: save the controller's settings under
  a name and switch with one click. Only what differs is written, and the controller keeps it on
  any device. A profile saves to a file and adds from one, to keep it or pass it on to another
  Nova 2 Lite.
- **Firmware updates**, for the controller and its 2.4 GHz receiver. The app installs GameSir's
  own firmware exactly as GameSir Connect does, after a full backup of the device. See
  [Firmware updates](#firmware-updates).
- **Help** inside the app. It checks what your computer still needs and gives the command for
  each item, with what every part does and links to its documentation, plus answers for when
  something doesn't work, a guide to every button, and PadSquire's own updates.

| | Cable | 2.4 GHz receiver | Bluetooth |
|---|---|---|---|
| Read and change settings | yes | yes | built, not tested on hardware yet |
| Remap every button | yes | yes | back buttons only, not tested |
| Live sticks, triggers and buttons | yes (macOS too) | yes | no |
| Stick check | yes (macOS too) | yes | no |
| Profiles | yes | yes | built, not tested |
| Controller firmware update | yes | no (use the cable) | no |
| Receiver firmware update | | yes | |

Tested on one Nova 2 Lite (firmware 7701 and 8201) and its receiver (firmware 2800). On macOS,
settings and remapping work by cable; see [On a Mac](#on-a-mac) for what doesn't yet.

## Get started

You need Linux or macOS, and Python 3.11 or newer (most Linux distributions have it; on a Mac,
`brew install python` with [Homebrew](https://brew.sh/), or the installer from
[python.org](https://www.python.org/downloads/macos/)). Run:

```sh
curl -fsSL https://padsquire.com/install.sh | sh
```

`curl` downloads the [installer](install.sh) and `sh` runs it. You can also download it, read it
first, and run it with `sh install.sh`. It needs no root, except for one step it asks about:

1. It makes a private Python environment in `~/.local/share/padsquire/venv` and installs
   PadSquire into it, so nothing is installed system-wide
   ([venv](https://docs.python.org/3/library/venv.html)).
2. It links the `padsquire` command into `~/.local/bin`.
3. It adds PadSquire to your app menu. On macOS it makes a small PadSquire app in
   `~/Applications` instead, which Launchpad and Spotlight find.
4. On Linux, only if you say yes, it installs a
   [udev](https://man7.org/linux/man-pages/man7/udev.7.html) rule so you can use the controller
   without `sudo`. It shows the commands first; this is the one step that asks for your password.
   macOS needs nothing here.

Then open **PadSquire** from your app menu (on macOS: Launchpad or Spotlight). It opens in its own
window (with Chrome, Chromium, Brave, Edge or Vivaldi; otherwise in a browser tab) and stops by
itself when you close it. The
window uses its own small browser profile in `~/.cache/padsquire/window` (for a Flatpak browser, as
on a Steam Deck: `~/.var/app/<browser>/cache/padsquire/window`), so it keeps its size and none of
your extensions or cookies come along; deleting that folder is safe.
**Setup and help** in the app tells you anything that's still missing, and explains each command
before you run it.

- **Update:** run the same command again.
- **Remove:** `curl -fsSL https://padsquire.com/install.sh | sh -s -- --uninstall`.
  Your firmware downloads and flash backups stay in `~/.local/share/padsquire`.
- **With pipx** instead: `pipx install 'padsquire[gui]'`, then `padsquire menu` for the app-menu
  entry, or the app in `~/Applications` on macOS ([pipx](https://pipx.pypa.io/)).
- **From source**, for development: `git clone https://github.com/Magniswerfer/padsquire`, then in
  that folder `python3 -m venv .venv && .venv/bin/pip install -e '.[gui,test]'` and
  `.venv/bin/padsquire gui`.

The app runs on `127.0.0.1` only and opens a one-time link. Other websites in your browser can't
reach it: the server rejects requests without the link's session cookie, requests for any other
host name, and API calls without a custom header. Fonts are bundled, so nothing loads from the
internet.

### On a Mac

Settings work on macOS by cable: macOS's own driver serves the controller's settings channel, so
there's nothing to set up and no `sudo`. The same installer works on a Mac.

- **Works:** every setting and remap by cable, the live sticks, triggers and buttons view, the
  stick check and profiles, tested on one controller. The receiver and Bluetooth should work the same way but aren't
  tested on a Mac yet.
- **Firmware updates, backups and restarts** work by cable too (tested on one controller). In
  update mode the controller is a USB disk, and macOS lets only an administrator take a disk
  away from its disk driver, so these ask for your password in macOS's own dialog. A small
  PadSquire helper then runs as administrator until the controller restarts, passing on only
  PadSquire's update commands. On a Mac each button does its whole job in one go (switch,
  step, restart), because there the controller leaves update mode by itself soon after.
  macOS may also ask whether "Python" may access a disk: that's the same helper.
- **Not tested on a Mac yet:** the receiver's firmware.
- **Playing games:** in PC/Xbox mode (green) Steam recognises it as an Xbox controller, because
  Steam brings its own driver; macOS itself has none for that mode. For games outside Steam, hold
  Menu + View until the light is blue: macOS sees a DualShock 4 there and supports it without a
  driver. The settings stay on the controller either way.

## Connect the controller

- **By cable:** plug it in and hold **View + Menu** for 2 seconds until the Home light is
  **green** (PC/Xbox mode). Each press moves to the next mode: green, red (Switch), blue
  (PlayStation). In blue mode it pretends to be a DualShock 4 and can't be configured.
- **Through the 2.4 GHz receiver:** plug the receiver in, turn the controller on, wait for it to
  link, then switch it to green the same way.
- **Over Bluetooth:** hold **Home + A** for 2 seconds until the light flashes **yellow** (Android
  mode), pair it in your system's Bluetooth settings, then use **Find controllers** in the app.

## Firmware updates

On the app's **Firmware** page (on a Mac, see [On a Mac](#on-a-mac) for the password): download the firmware (the app checks for new official firmware
and verifies its checksums), switch the controller or the receiver to update mode, make a
backup, install, restart. The controller's firmware is installed by cable; the receiver's through
the receiver itself.

What keeps it safe:

- **A backup first.** The whole flash is read twice and compared before anything is written, and
  every write needs a backup of the same device. The backup is also your way back: writing it
  back restores the old firmware.
- **Only the firmware area is written.** Settings, the Bluetooth address and pairing, and the
  factory stick calibration are never touched, and the whole flash is compared before and after.
- **Every block is checked** against the device's own checksum. A failed write keeps the device
  in update mode so it can be done again; it is never restarted half-written.
- Writes follow the same order as GameSir's own updater, and use only "erase a block" and "write
  a block". The command that burns the chip's key can't be sent.

Backups are kept in `~/.local/share/padsquire/backups`, readable only by you. They contain
your controller's Bluetooth pairing and calibration: copy them somewhere safe, and don't share
them.

Still unknown: how to reach update mode if the firmware itself no longer starts. The ways in all
go through the firmware. That's why a failed write is never followed by a restart.

## Known firmware quirks

Found on firmware 8201. The app works around each one:

- **Six on/off settings can't be changed by cable or receiver.** They are low battery warning,
  play while charging, swap sticks, invert sticks X/Y, swap LB/LT and swap RB/RT. The controller
  keeps its value. The app shows them read-only. Bluetooth is not tested yet.
- **Outer dead zone above 25% is broken.** The stick at full tilt comes out on the opposite side.
  The app stops at 25%.
- **The anti-dead zone has no effect by cable or receiver.** The controller stores the value
  but doesn't use it. Bluetooth is not tested yet.
- **Vibration strength goes in steps of 25%.** Other values are rounded down. The Nova 2 Lite has
  two vibration motors, in the grips, and none in the triggers.
- **Remapping takes buttons only:** a button can't act as a stick direction or be switched off.
- **Sometimes the controller stops responding right after a change**, mostly through the receiver.
  The change is kept, and a restart fixes it; the Buttons page has a button for that.
- Auto-sleep and profiles get no answer by cable or receiver.

## Command line

Everything in the app also works from a terminal:

```sh
padsquire usb info                              # firmware, battery, MAC
padsquire usb deadzone left-stick-inner 3       # set a dead zone (percent)
padsquire usb vibration --left-grip 50 --right-grip 50
padsquire update                                # fetch the newest firmware
padsquire uboot --help                          # update mode: backup, restore, flash, restart
```

`usb` works by cable and through the receiver; `ble` does the same over Bluetooth, and
`--simulate` tries any command without a controller. `padsquire gui --simulate receiver` (or
`cable`, `bluetooth`) opens the settings app on a simulated controller that behaves like the real
one on that connection. On a Mac, `uboot` commands that use update mode run with `sudo`, and need
`--restart` (the controller leaves update mode once a command lets go of it). `padsquire --help`
lists everything. For
automatic weekly firmware checks there is a systemd timer in `contrib/systemd/`.

## How it works

In green mode the controller shows up as `3537:100F` by cable, or `3537:1098` through the
receiver, with a vendor HID interface. GameSir Connect talks to it in 64-byte reports using the
same "GCM" protocol the Android app uses over Bluetooth, so one client drives all three. Firmware
updates use the chip's built-in USB update mode (JieLi "UBOOT1.00"), where the device shows up as a
USB disk that takes special commands. All the details are in [PROTOCOL.md](PROTOCOL.md); testing
on hardware is described in [HARDWARE.md](HARDWARE.md).

## Tests

```sh
.venv/bin/pip install -e '.[gui,test]'
.venv/bin/pytest
```

The repository contains no GameSir files. Tests build synthetic firmware files, installers,
flash images and sysfs trees shaped like the real ones. They replay replies captured from a real
controller, and include a fake chip in update mode for the firmware writer.

## Roadmap

- Test Bluetooth on hardware, including the six settings refused by cable.
- Find the cause of the occasional stop after a change.
- Controller firmware updates through the 2.4 GHz receiver (JieLi's over-the-air updater, which
  GameSir Connect doesn't use for this controller): research.
- Macros on the back buttons: the command is known (`0x53` over Bluetooth, `f6` by cable) but
  its frames aren't decoded yet.
- Other GameSir controllers.

## Disclaimer

Unofficial and not affiliated with GameSir. Firmware updates can brick hardware. The safety
checks above reduce that risk but cannot remove it. Tested on one controller and one receiver so
far.

## Licence

The code is free software under the [GNU Affero General Public License](LICENSE), version 3 or
any later version (`AGPL-3.0-or-later`). You may use, change and share it. If you share a
changed version, or run one for others over a network, you must offer its source code under the
same licence. The settings app's "Source code" link does that for you as long as you keep it.

The protocol notes in [PROTOCOL.md](PROTOCOL.md) are under
[Creative Commons Attribution-ShareAlike 4.0](LICENSES/CC-BY-SA-4.0.txt), so other projects can
reuse them.

The bundled fonts (Young Serif, Atkinson Hyperlegible Next and Atkinson Hyperlegible Mono) are
under the [SIL Open Font License](https://openfontlicense.org/); their licences are in
[`src/padsquire/web/static/ds/fonts/`](src/padsquire/web/static/ds/fonts/).
[`src/padsquire/web/static/vendor/three.js`](src/padsquire/web/static/vendor/three.js) is
[three.js](https://threejs.org/), under the MIT licence (its notice is at the end of the file).
