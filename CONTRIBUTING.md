# Contributing to PadSquire

Thank you for helping. PadSquire talks to real hardware and can rewrite a controller's
firmware, so this guide asks for more care than most projects do. It covers four ways to
help, from the easiest to the most involved:

1. [Reporting a problem](#reporting-a-problem)
2. [Testing on hardware](#testing-on-hardware) (Bluetooth, macOS and other controllers are
   the biggest gaps)
3. [Documenting the protocol](#documenting-the-protocol)
4. [Contributing code](#contributing-code)

Read [the ground rules](#ground-rules) first, whichever you pick. They apply to everyone.

## Ground rules

These protect your controller, the people who run PadSquire, and the project.

- **Never send `FC 12` (write chip key).** It burns eFuses on the chip and can't be undone.
  PadSquire refuses to send it. Don't add code that sends it, and don't run it by hand.
- **Never send anything to a device advertising as `GameSir-Nova 2 Lite_DFU`.** That is the
  controller's Bluetooth firmware-update mode, and nobody has studied it yet.
- **Read before you write.** Try a read-only experiment first. Before you change a setting,
  note its current value, and set it back afterwards. `padsquire usb restore --yes` (or `ble`)
  resets every setting to factory defaults if you lose track.
- **Firmware writes on hardware PadSquire hasn't seen go in four stages**, and you check the
  result of each one before you start the next:
  1. Update mode and `uboot info`. This is read-only.
  2. A full flash backup, read twice and compared, kept outside the repository.
  3. A restore of that same backup, to test writing with data you know is good.
  4. Only then an update, through `safety.py` and `jlwrite.py`, keeping the reserved areas
     (BTIF, EXIF, USERIF, VM).

  The tools refuse to write without a verified backup of the same device. Keep it that way.
- **No GameSir files in the repository.** That means no firmware (`*.ufw`), installers
  (`*.exe`), DLLs, APKs, flash dumps (`*.bin`), images or text copied from GameSir's apps,
  and no decompiled code. Describe what the code does, in your own words, with the byte
  values it sends and receives. Function names and addresses (such as `FUN_10053c20`) are fine
  as references. Tests build synthetic files shaped like the real ones; see `tests/`.
- **Don't share flash backups.** They hold the controller's Bluetooth pairing and factory stick
  calibration. Probe reports and `padsquire usb info` include the controller's MAC address. That
  is usually harmless to share, but you may replace it with `xx` before you post.
- **Interoperability is the aim.** PadSquire exists so that people can use and update hardware
  they own on Linux and macOS. Contributions that defeat copy protection, unlock paid features,
  or are aimed at other people's devices don't belong here.

## Reporting a problem

[Open an issue](https://github.com/Magniswerfer/padsquire/issues/new/choose) and pick the
template that fits. The most useful bug report includes:

- What you did, what you expected, and what happened.
- Your system (Linux distribution or macOS version), PadSquire's version (`padsquire --version`),
  the controller's firmware (`padsquire usb info`, or `ble info` over Bluetooth), and
  how it was connected: cable, 2.4 GHz receiver or Bluetooth, and which light colour.
- The output of `padsquire probe --guided`. It walks you through each mode and saves a
  `gamesir-probe-*.json` file that never changes a setting. Attach the file.
- For a protocol problem, a log of every frame. Run the command again with `PADSQUIRE_DEBUG=1`
  in front, such as `PADSQUIRE_DEBUG=1 padsquire usb info`, and attach what it prints.

## Testing on hardware

What still needs a real controller is listed in [HARDWARE.md](HARDWARE.md): Bluetooth above
all, then the receiver on a Mac, blue (DualShock 4) mode on macOS, and the open questions at the
end of [PROTOCOL.md](PROTOCOL.md). One careful report on any of these moves the project further
than most code changes.

Report it with the **Hardware test** issue template. Say for each item whether it worked, give
the exact command, and paste what it printed. "It didn't work" is a result too, and an
important one: say so, with the output.

Other GameSir controllers are welcome, but start read-only. Run `padsquire probe --guided` and
post the report. Don't try a firmware command on a controller PadSquire doesn't know, and don't
flash firmware meant for another model: the T4N2 (Nova 2 Lite) and the T4N lite (the original
Nova Lite) are different hardware, and one's firmware bricks the other.

## Documenting the protocol

[PROTOCOL.md](PROTOCOL.md) is the project's memory. It's what the code is checked against, and
other projects reuse it under [CC BY-SA 4.0](LICENSES/CC-BY-SA-4.0.txt). By adding to it you
agree to license your addition under the same terms.

### What a note says

Every finding answers these questions, so that someone else can check it:

- **What**: the exact bytes, both the request and the reply, in hex, with the meaning of each
  field. Use a table for anything with more than three fields. Use the names the code uses
  (`gcm.DeadZone`, `gcm.Key`), so a reader can find it.
- **Where it was seen**: the connection (cable, receiver or Bluetooth), the light colour or
  mode, the firmware version, the operating system if it matters, and the date
  (`2026-10-23`). Something seen on firmware 8201 by cable is not yet known for Bluetooth
  or for 7701. Say so.
- **How sure it is**: one of the levels below, in bold, in the note itself.
- **The source**: the app and version (`GameSir Connect 1.16.7`, `GameSir app 5.8.20`),
  the class or function, or the command you ran on hardware.

### How sure it is

| Level | Means | Wording in PROTOCOL.md | In code |
|---|---|---|---|
| Static analysis | Read from an app or DLL, not seen on hardware | "decoded from…", "not yet run" | an `ASSUMPTION:` comment |
| Seen once | Observed on hardware a single time | "seen once (date)" | an `ASSUMPTION:` comment |
| **Confirmed** | Reproduced on hardware, read back, values restored | "**confirmed** on hardware (date, firmware, connection)" | no marker |
| **Refuted** | Hardware contradicts what was assumed | "**not**…", with what happened instead | the code changed to match |

A finding is **confirmed** only when all of these hold:

1. **It was reproduced.** It happened at least twice, from a fresh connection, not two reads
   in a row of the same thing.
2. **The write was read back.** For a setting, you set it, read it back from the controller,
   and saw it stick. A reply that only echoes the request proves nothing on its own: some
   settings are refused and read back unchanged (see the six feature toggles).
3. **The effect was observed where that makes sense.** A stored value isn't a working one.
   The anti-dead zone is stored by cable but changes nothing on the stick output. Check the
   gamepad's own output (`evtest`, or the live view in the app), not only the settings channel.
4. **It's limited to what you tried.** Name the firmware and connection, and leave the rest
   open.
5. **The original value was put back**, and the note says what the default was.

When two sources disagree, such as the DLL and the hardware, the hardware wins. Keep a line
on what the app's code suggested, because the next firmware may behave differently.

Negative results belong in the notes too: "no reply by cable (2.2 s timeout)", "refused,
reads back unchanged", "tried six times, didn't happen". They save the next person a day.

### When a finding changes

Change everything that depends on it, in the same pull request:

- **PROTOCOL.md**: update the section. Keep the history short ("confirmed 2026-10-23;
  earlier notes assumed…") instead of leaving the old claim standing. Move the item out of
  "Open questions" if it's answered.
- **The code**: remove the `ASSUMPTION` comment once something is confirmed. Fix the code if
  it was refuted.
- **The simulator** (`src/padsquire/simulator.py`): it answers the way PROTOCOL.md says the
  real controller does on each connection. Update it, and add a test to
  `tests/test_simulator.py`.
- **Captured replies**: when you have a real reply, add it as a fixture next to the existing
  ones (`tests/test_hid.py`, "Replies captured from a real Nova 2 Lite"). Say where it came from
  in a comment, and replace the MAC with a made-up one if you like.
- **README.md, HARDWARE.md and the app's Help text**, if users see the difference: a quirk,
  something that now works, or something that can now be tested.

### Style

Plain English and short sentences. Use bold for the claim and the level, and backticks for
bytes, commands and names in the code. Hex is lowercase in frames (`f1 01 09`) and
`0x`-prefixed when it's a single value (`0xFF7A`). Put new findings in the section they belong
to, not at the end. PROTOCOL.md is where technical detail goes. The README speaks to people who
use the app and leads with what they can do.

## Contributing code

### Setting up

```sh
git clone https://github.com/Magniswerfer/padsquire
cd padsquire
python3 -m venv .venv
.venv/bin/pip install -e '.[gui,test]'
.venv/bin/pytest
.venv/bin/padsquire gui --simulate cable     # or receiver, bluetooth
```

`python3 -m venv .venv` makes a private Python environment in the `.venv` folder, so nothing is
installed system-wide ([venv](https://docs.python.org/3/library/venv.html)). `pip install -e`
installs PadSquire from this folder in editable mode, so your changes take effect without
reinstalling; `[gui,test]` adds the settings app's and the tests' dependencies
([pip install](https://pip.pypa.io/en/stable/cli/pip_install/)). To undo it, delete `.venv`.

`--simulate` runs the settings app on a fake controller that behaves like the real one on that
connection, so most work needs no hardware. After changing Python under `web/`, restart the
app. Static files (`app.js`, `app.css`) are served fresh, so a reload is enough.

[CLAUDE.md](CLAUDE.md) has a map of the source tree (the "Layout" section).

### What a change needs

- **Tests.** `pytest` must pass on Linux and macOS (CI runs both, on Python 3.11 and 3.13).
  New behaviour comes with a test, and the tests need no hardware: use the simulator, the
  captured replies, and synthetic sysfs trees, files and flash images the way the existing
  tests do. Anything that writes flash is tested against the fake chip in
  `tests/test_jlwrite.py`.
- **A path in the settings app.** The app is the product. A new capability gets its place in
  the app, an entry on the Setup and help page if it needs something installed or set up, and
  troubleshooting text, not only a CLI command.
- **Commands that explain themselves.** Every command shown to users says what each part does
  (what `sudo` is for, what gets written where, how to undo it) and links to the official
  documentation. Follow the pattern in `src/padsquire/web/setup.py`.
- **Plain copy in sentence case.** "Add a profile from a file", not "Add a Profile From a File". Say what happens, not
  how it works.
- **Our name.** The project is PadSquire. "GameSir" names only what PadSquire works with, never
  part of our own name. Public wording leads with interoperability.
- **The safety checks stay.** `jlwrite.py` is the only code that erases or writes flash. The
  `jlboot.py` allowlist, the backup checks in `safety.py` and the reserved-area checks in
  `jlflash.py` don't get weaker. A change to any of them needs a test that shows the refusal
  still happens.
- **No new runtime dependencies** without a reason in the pull request. The settings app has no
  build step: vanilla JavaScript, a strict content security policy, and nothing loaded from the
  internet.
- **Shared design files** (`web/static/ds/`, `pad-model.js`, `vendor/`, `models/`, `img/`,
  `data/padsquire.svg`) come from the website repository through `tools/sync-design.sh`.
  Change them there.

### Code style

Match the code around you. In this project that means:

- Small modules with one job. `gcm.py` holds only pure functions (frames in, values out, no
  I/O), and the transports (`ble.py`, `hid.py`) only move bytes.
- Short comments that say why, or what a byte means, not what the next line does.
- An `SPDX-License-Identifier: AGPL-3.0-or-later` line at the top of every new source file
  (`CC-BY-SA-4.0` for protocol notes).
- Constants named after what the hardware does (`OUTER_DEAD_ZONE_MAX`,
  `USB_READONLY_FEATURES`), with a comment pointing to the PROTOCOL.md section.

### Commits and pull requests

- One change per pull request. A protocol finding and the code that uses it belong together.
  A rename across the codebase goes in its own pull request.
- Commit messages start with the area and say what changed for the user, in sentence case,
  no full stop: `Firmware page: offer a reinstall of the version already on it`,
  `Live view on macOS, from the controller's own state reports`.
- Fill in the pull request template. It asks how you tested the change, and on what hardware.
- If you ran something on hardware, say what, on which firmware and connection, and paste the
  output.

By contributing code you agree to license it under the
[GNU Affero General Public License](LICENSE), version 3 or later, and protocol notes under
[CC BY-SA 4.0](LICENSES/CC-BY-SA-4.0.txt). Only contribute work you have the right to share.

## Releases

Only maintainers release. They bump `version` in `pyproject.toml` and push a matching `v*` tag,
and GitHub Actions publishes to PyPI.
