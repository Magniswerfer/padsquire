<!-- SPDX-License-Identifier: CC-BY-SA-4.0 -->
# GameSir Cyclone 2 — protocol notes

Protocol notes for the Cyclone 2 (GameSir model code C2), made for interoperability so that it
can be configured from Linux and macOS, as EU law allows (Software Directive 2009/24/EC,
Article 6). Licensed under [CC BY-SA 4.0](LICENSES/CC-BY-SA-4.0.txt); the code is
AGPL-3.0-or-later. The Nova 2 Lite's notes, and the levels of certainty used here, are in
[PROTOCOL.md](PROTOCOL.md) and [CONTRIBUTING.md](CONTRIBUTING.md).

**Status: static analysis**, decoded on 2026-10-26 from GameSir Connect 1.16.7 (`main.js`: the
device table entry `C2`, the handler `proxyC2`, its command builder and its profile layout).
Nothing here has been run by PadSquire yet. Two other projects have used this protocol on
hardware, and where they did, the note says "seen on hardware by" them. Everything they saw agrees
with Connect's code:

- [Deadband](https://github.com/broroeror/gamesir-linux-tools) (MIT) configures the Cyclone 2 in
  full, writes included, from captures of GameSir's app (checked 2026-10-27). Its register map
  (`vendors/gamesir/config.py`) gives offsets inside these same profile images.
- [cyclone2-linux](https://github.com/vdemonchy/cyclone2-linux) (GPL-3.0; its `docs/protocol.md`)
  reads the battery and sets the lighting.

Code: `src/padsquire/c2.py` (frames and parsers), `src/padsquire/c2usb.py` (hidraw transport,
read-only), `padsquire c2 find | info | dump`, `SimulatedC2` in `simulator.py`.

## USB identities

From Connect's device table (all VID `0x3537`, configuration on usage page `0xFFF0`):

| PID | Mode in Connect | Notes |
|---|---|---|
| `0x101D` | Wired | firmware `GS_C2_DEVICE` 1.2.3 |
| `0x1053` | Wired | a second hardware edition, update-mode name `GS_C2_ADC_DEVICE`, its own firmware 3.5.6 |
| `0x102A` | Wireless | through the receiver |
| `0x100B` | Wireless | through the receiver. **Seen on hardware** by cyclone2-linux (2026-06-03): the receiver in XInput mode, "Xbox 360 Controller for Windows", vendor interface 1 |

cyclone2-linux also saw these, none of them in Connect's table: DS4 mode as Sony's `054c:09cc`
(the `playstation` driver; its battery reads a constant ~5%), Switch mode as Nintendo's
`057e:2009` (the `nintendo` driver, coarse battery), HID mode as `3537:0575`, which is also what
the receiver shows with the controller off.

Firmware: the controller (`GS_C2_DEVICE_V123`), the ADC edition (`GS_C2_ADC_DEVICE_V356`) and the
receiver (`GS_C2_Dongle_V119`, `V121`) are JieLi `.ufw` files for the AC695X family (BR23, the
Nova 2 Lite's chip), each as a `_Key` and a `_No_Key` build. Their `isd_config.ini` is not
readable with the Nova 2 Lite's scheme (`ufw.py` gets no config from them). Connect updates the
controller over HID with JieLi's updater (`JL_upgradeUtility_x86`, retrying with the
`_No_Key` file when the first answers `-3`), not through the download-mode disk. PadSquire does
none of this.

## Framing

64-byte reports with report IDs on the `0xFFF0` interface.

- **Requests**: output report `0x0F`: `0f <command> <arguments...>`, zero-padded to 64 bytes
  (report ID included; cyclone2-linux writes 64 bytes on hidraw and the controller answers).
- **Replies**: the command type in byte 1, on input report `0x10`. **Seen on hardware** by
  Deadband (it reads profiles from `10 05` replies) and cyclone2-linux (`10 06 00`, an
  acknowledgement, after its writes).
- **State**: input report `0x12`, streamed after the host sends anything (cyclone2-linux:
  nothing arrives before; Connect sends a heartbeat when it opens the device).

Connect queues one request at a time: it waits for any reply, resends after 200 ms, gives up
after 10 sends. An acknowledgement with byte 2 = `1` means busy: ask again.

## Commands (Connect's `CommandType` and its builder)

| Byte 1 | Name | Request | Notes |
|---|---|---|---|
| `01` / `02` | Enter / exit profile config | `0f 01`, `0f 02 <save>` | not used by `proxyC2` |
| `03` | Write profile | `0f 03 <profile> <offset hi> <offset lo> <length> <data...>` | up to 58 bytes each |
| `04` | Read profile | `0f 04 <profile> <offset hi> <offset lo> <length>` | |
| `05` | Read profile reply | `.. 05 <profile> <offset hi> <offset lo> <length> <data...>` | complete when offset + length = the profile's size |
| `06` | Acknowledgement | `.. 06 <busy>` | |
| `07` | Switch profile | `0f 07 <profile>` | 1-4, or 5 for Shift |
| `08` | Save profile to EEPROM | | not used |
| `09` / `0a` | Firmware version | `0f 09` | reply: controller version as ASCII digits in bytes 4-8, receiver's in 12-16; Connect shows them dot-separated (`123` → 1.2.3) |
| `0b` / `0c` | Current profile | `0f 0b` | reply byte 2. Connect shows 0 as profile 1, and switches to profile 1 when it reads more than 4 or the Shift layer |
| `0d` / `0e` | Light colours / play state | `0f 0d` reads; `0f 0d <playing> <frame>` sets the preview | |
| `0f` | Profile changed | from the controller: `<profile> <offset hi> <offset lo> <length hi> <length lo>` | profile `48` = the current profile number changed. Connect then reads the range back with `10` |
| `10` / `11` | Refresh profile | `0f 10 <profile> <offset hi> <offset lo> <length>` | like `04`, for a range |
| `12`-`15` | EEPROM write / read | | not used |
| `16` | Macro recording | `0f 16 <1 = L4, 2 = R4> <state>` | |
| `17` | Quick update | `0f 17 55 88` | starts a firmware update |
| `20` | Vibration test | `0f 20 66 55 <left> <right>` | Connect stops it with `0, 0` after up to 1 s |
| `f2` | Heartbeat | `0f f2 00` | sent when Connect opens the device |
| `f3` / `f4` | Key status | | |
| `fc` | Enter update mode | `0f fc` | |
| `fd` | Calibration | `0f fd <state>` | reply byte 5 mode, byte 6 bits: LT, RT, LS, RS, motion sensor |

PadSquire sends only `f2`, `09`, `0b` and `04` (`c2.READ_ONLY`); the transport refuses the rest.

cyclone2-linux's "battery request", `0f 03 00 00 ...`, is command `03` (write profile 0, offset 0,
length 0) by Connect's table. It wakes the stream on hardware and seems harmless, but PadSquire
uses the heartbeat instead, as Connect does.

## The 0x12 state report

From `proxyC2.parseGamepadState` and Connect's gamepad view (byte 0 is the report ID):

| Byte | Meaning |
|---|---|
| 1-4 | left X, left Y, right X, right Y, 0-255, 128 = centre |
| 5 | low nibble: D-pad, 0 = up, clockwise to 7, 8 or more = released. Bits 4-7: X, A, B, Y |
| 6 | bits: LB, RB, LT, RT, View, Menu, LS, RS |
| 7 | bits: 0 Home, 1 Capture, 3 L4, 4 R4, 5 M |
| 8, 9 | LT, RT, 0-255 |
| 35 | charge state. **Seen on hardware** (cyclone2-linux, 2026-06-05): `1` on the cable, `0` on battery, set even when full |
| 36 | battery level in percent. **Seen on hardware** by cyclone2-linux (`0x64` = 100 at full charge) and Deadband |
| 37 | the active lighting effect |
| 38-52 | the five light colours now shown, RGB |
| 53 | bits 0-1: a macro is being recorded; bit 4: on L4, bit 5: on R4 |

## Profiles

The settings are byte images the controller keeps: profiles 1-4 and the Shift layer (5), 680
bytes each, and the lighting profile (32), 635 bytes. Connect reads each in 58-byte pieces at
start-up, and writes back only the range of bytes that changed (from the first changed byte to
the last), in 58-byte pieces.

### Profile layout (680 bytes)

| Offset | Size | Content |
|---|---|---|
| 0 | 32 | name, UTF-8, zero-padded |
| 32 | 32 | vibration left, right, LT, RT (percent; Connect offers 0/25/50/75/100); audio: on, volume, mixer, mic mute, mic sensitivity; Shift on, Shift button; D-pad diagonal lock; swap A/B and X/Y in XInput; the same in Switch mode; report rate (0 = 250, 1 = 500, 2 = 1000 Hz); 17 reserved |
| 64 | 16 × 7 | buttons, in this order: up, down, left, right, LB, RB, LS, RS, A, B, X, Y, Home, View, Menu, Capture. Each: turbo, turbo speed, mapping on, three mapping targets, toggle |
| 176 | 159 | L4: the 7 button bytes, then a macro: flags (bit 0 on, bit 1 run while held, bit 2 repeat), repeat time (2 bytes, big-endian), number of steps, 30 steps of key, hold time (2), delay (2; none after the last step) |
| 335 | 159 | R4, the same |
| 494 | 28 | LT: turbo, turbo speed; dead zone on, start, end, anti start, anti end; mapping on and three targets; toggle; quick trigger status (bit 7 on, bit 0 mode 1, bit 1 mode 2), start, end; curve on, type, curvature, five points (x, y) |
| 522 | 28 | RT, the same |
| 550 | 32 | left stick: on, square; dead zone (5 bytes as above); curve (13 bytes as above); mapping: on, invert X, invert Y, axis ratio, mouse DPI, mapping index, cross, up, down, left, right, dead |
| 582 | 32 | right stick, the same |
| 614 | 33 | motion "aim": mode, trigger key, active axes (bit 0, bit 1), then the stick's 30 bytes from the dead zone on |
| 647 | 33 | motion "tilt", the same |

**Seen on hardware by Deadband**, at these same offsets: vibration left and right (32, 33;
default 75), report rate (46, values above), LT dead zone start, end, anti start, anti end
(`0x1F1`–`0x1F4`), its quick trigger (`0x1FA`) and curve (`0x1FE`), RT the same at +`0x1C`, the
left stick's square / circle switch (`0x227`), dead zones (`0x229`–`0x22C`) and curve (`0x22E`),
the right stick the same at +`0x20`. It writes with `03` into profiles 1-4 directly, and finds
that writes persist without a save command.

Dead zones are percentages of travel as Connect shows them: output starts at "start" and is full
at "end". Connect's reset values are 10-100 for the sticks and 5-95 for the triggers. Button codes
in mappings and macros come from Connect's key table (`bo.getButtonKey`), not yet listed here.

### Lighting profile (635 bytes)

| Offset | Size | Content |
|---|---|---|
| 0 | 1 | the active effect, 0-3 |
| 1 | 5 × 124 | five effects: key frame count, effect count, speed (stored as 20 − speed, 0-20), brightness (0-100), then 8 frames of 5 colours (RGB) |
| 621 | 14 | sound-reactive (bit 0), user effect index, profile light colour (RGB), raise to wake (bit 0), standby time, 7 reserved |

**This explains cyclone2-linux's lighting commands**, which work on hardware. They are profile
writes (`03`) to the lighting profile (32 = `0x20`), which that project read as an "LED
subsystem" with registers:

| cyclone2-linux | Read as a profile write |
|---|---|
| `0f 03 20 00 04 01 <0-100>` "brightness register" | effect 0, brightness |
| `0f 03 20 00 01 3a 01 05 0a 32 <colours...>` "enter static mode" | effect 0 from its start: 1 key frame, effect count 5, speed byte 10, brightness 50, frame colours |
| `0f 03 20 00 05 03 <rgb>` Left | effect 0, frame 0, colour 0 |
| `0f 03 20 00 08 03 <rgb>` Right | colour 1 |
| (`0b`, not used there) | colour 2 |
| `0f 03 20 00 0e 03 <rgb>` Logo | colour 3 |
| `0f 03 20 00 11 03 <rgb>` Centre | colour 4 |

It also found that writes sent back to back get dropped, and left about 60 ms between them;
Connect waits for each acknowledgement instead.

## Open (needs a Cyclone 2)

1. Whether every read PadSquire sends gets an answer, by cable and through the receiver
   (`padsquire c2 dump` saves everything it reads).
2. The firmware version reply's layout (bytes 4-8 and 12-16).
3. The parts of the profiles Deadband hasn't mapped: the button and macro blocks, motion, the
   lighting tail. Set something in GameSir Connect on Windows, dump, compare.
4. Writes: Deadband does them already; PadSquire would only after the reads are confirmed here,
   one setting at a time, read back and restored, as for the Nova 2 Lite.
