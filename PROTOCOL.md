# GameSir Nova 2 Lite — protocol notes

Reverse-engineering notes for a Linux-native config and firmware tool.
Sources: GameSir Connect 1.14.5 (Windows, Electron) and GameSir app 5.8.20 (Android).
Status: **static analysis only — nothing here has been tested on hardware yet.**

GameSir's internal name for this controller is **T4N2** (Nova 2 Lite). Don't confuse it with
**T4N lite** (the original Nova Lite): different hardware and firmware. Flashing one with the
other's firmware bricks it.

## Hardware

| Part | Chip | Evidence |
|---|---|---|
| Controller | JieLi AC695x (BR23) | `UdiskProductName: "AC695X_TRANS"` in Connect's device table |
| 2.4 GHz dongle | JieLi AC632N | `AC632N_TRANS` string in dongle firmware |

USB vendor ID `0x3537`. Connect 1.16.7 registers the T4N2 DLL for PIDs `0x100F`, `0x1141`,
`0x105A`, and its device table lists `0x100F` (wired) and `0x1098` (wireless, via the dongle) as
config PIDs.

### USB identities (confirmed on hardware, 2026-10-23)

The USB identity follows the controller's mode, which **Menu + View** (hold 2 s) cycles:
green PC/Xbox, red Switch, blue Other Devices. The dongle takes on the same identity as the
controller behind it.

| Setup | USB ID | Interfaces |
|---|---|---|
| Wired, green (PC/Xbox) | `3537:100F` "2.4G" / "XBOX 360 For Windows" | if00 `ff/5d/01` Xbox 360 (`xpad`, names it "GameSir Nova 2 Lite"); if01 HID keyboard + consumer; if02 HID vendor page `0xFF7A`, 64-byte in/out, no report ID |
| Wired or via dongle, blue (Other Devices) | `054c:09cc` "Sony Computer Entertainment" / "Wireless Controller" | one HID interface, stock DualShock 4 descriptor, `hid-playstation`; no vendor channel |
| Dongle, no controller linked | `3537:2106` "Zikway" / "HID zk" | one HID interface, the same `0xFF7A` descriptor as if02 above |

The `0xFF7A` interface is the SAPJoy config channel (Connect's table says `UsagePage: 65535`,
which does not match the descriptor literally). Its report descriptor:
`067aff0901a1010902a1000903150025ff75089540810209059102c0c0`.
`0x2106` is in neither Connect's table nor the DLL's PID list. The dongle through green mode
(expected `0x1098`) has not been seen yet.

## Three ways to talk to it

### 1. Bluetooth LE: "GCM" protocol (easiest, fully readable)

Used by the Android app. The controller advertises two Bluetooth identities:

- `GameSir-Nova 2 Lite`: classic BT HID gamepad
- `GameSir-Nova 2 Lite_xxxx`: BLE config channel (also `_G` variant)
- `GameSir-Nova 2 Lite_DFU`: BLE firmware-update mode

GATT service `00008650-0000-1000-8000-00805f9b34fb`, characteristics `0x8655` and `0x865F`.
The write and notify roles of each still need confirming on hardware.

**Packet format:** `[cmd, subtype, length, ...payload, checksum]`
- `length` = total packet length including header and checksum
- `checksum` = 8-bit sum of all preceding bytes

Example, dead zones (cmd `0x07`):
```
query left-stick inner:   07 01 06 00 00 0E
set   left-stick inner=5: 07 01 06 01 05 14
```
Dead-zone ids: 1/2 L/R stick inner, 3/4 L/R stick outer, 5/6 L/R trigger front,
7/8 L/R trigger back, 9/10 L/R stick reverse (anti-dead-zone).

Action types used across commands: `0x00` query, `0x01` set, `0x02` clear/reset, `0xFF` change notify.

Command table (`com.xiaoji.sdk.gcm.GcmProtocol`):

| Cmd | Meaning | Cmd | Meaning |
|---|---|---|---|
| `0x01` | basic info | `0x1F` | key mapping |
| `0x04` | immediate vibration | `0x20` | slave firmware version |
| `0x05` | lighting effect | `0x21` | trigger quick mode |
| `0x07` | dead zones | `0x23` | feature toggles (D-pad lock, stick swap, inversions…) |
| `0x08` | key protocol / input mode | `0x24` | get info (basic, host, slave, MAC…) |
| `0x09` | mode | `0x25` | vibration |
| `0x14` | device control | `0x26` | general settings (auto-sleep…) |
| `0x15` | restore defaults | `0x27` | gyro settings |
| `0x1E` | key layout replace (ABXY) | `0x28` | immediate settings |
| `0x29` | config slots | `0x53` | button macros |
| `0x50`–`0x52` | "UUID" get/set | `0x8F` | DFU main firmware version |

**BLE request flow** (`BluetoothLeService`, implemented in `ble.py`): subscribe to notifications on
`0x865F` and `0x8655`, write each command to `0x865F` with response, wait ~60 ms, then *read*
`0x865F` for the reply (the Nova 2 Lite config disables notification replies). On connect the app
sends `01 01` (basic info) and `08 02 05 00 0F` (input mode query), reading after each.

**Replies** echo the command byte and end with the same checksum. Known layouts:

| Request | Reply |
|---|---|
| 6-byte query/set (`0x07`, `0x23`, `0x26`) | value at byte 4 |
| `25 01 0D …` vibration | bytes 4–11: L grip, L trigger, L pressed, L sync, R grip, R trigger, R pressed, R sync |
| `24 01 04 29` host info | byte 3: battery (bit 7 = charging, if like the PC app; unconfirmed) |
| `01 01` basic info (no checksum) | 19 bytes, or 20 starting `0xC9`: MAC [1:7], hw [7:9], firmware = `hex(b11)+hex(b12)` → `"8201"`, production date [13:17], GCM version [17:19] |

**Commands without a length byte.** Two commands break the `[cmd, sub, len, …]` pattern:

| Command | Frame | Notes |
|---|---|---|
| `0x1F` key mapping | `[1F, action, map_id, target, flag, sum]` | `flag` `FF` = disabled, `FE` = macro, `01` = keyboard key, else a 2nd gamepad key |
| `0x21` quick trigger | `[21, action, L, R, sum]` | `1` = on, `2` = off; query sends `21 00 01 01 23` |

Other commands decoded for the GUI:

| Command | Query | Set | Reply |
|---|---|---|---|
| `0x29` profiles | `29 01 05 00 2F` | `29 01 06 01 <slot> sum` | `[4]` slot count, `[5]` current slot |
| `0x05/0x08` button light | `05 08 06 00 <key> sum` | `05 08 0A 01 <key> R G B <effect> sum` | `[4]` key, `[5:8]` RGB, `[8]` effect |

**Key codes.** The app uses codes where `0x1xx` = gamepad keys and `0x2xx` = keyboard keys; on the
air a key is its low byte. The lookup tables (`KeyCodeUtil`) are nested branches that decompile
badly, so they were recovered by *executing their Dalvik bytecode* for every input with a small
interpreter. Both directions agree exactly (46 entries). Mapping IDs: L4 = 1, R4 = 2, L5 = 3, R5 = 4,
A/B/X/Y = 11–14. The Nova 2 Lite exposes only L4/R4 as remappable over Bluetooth, to 16 gamepad
buttons, plus 16 stick directions on firmware ≥ 6900 (`Nova2LiteDevCfg`).

**The Home light** (key `450`, scan `0xC2`) is the Nova 2 Lite's only light, with one effect
(solid, `1`). Colours travel as RGB (`lightingColorMode` 0); the app converts to and from HSB in the UI.

**Assumptions the GUI makes, to check on hardware:** dead zones are percent of travel (shown
0–50); vibration levels 0–4 (GameSir Connect maps levels 0–4 to 0/25/50/75/100%); auto-sleep is
in minutes; whether the Nova 2 Lite answers the profile query at all (the GUI hides profiles if not).

The `0x50`–`0x52` "UUID encryption" commands send the current Unix timestamp and date.
They look like registration bookkeeping, not an authentication gate. Verify on hardware.

Nova 2 Lite specifics (`Nova2LiteDevCfg`): macro loop needs firmware ≥ 6400, macro settings ≥ 6900.
Back buttons are key codes 459 and 461.

### 2. USB HID: "SAPJoy" protocol (partly decoded from the DLL; first query confirmed)

GameSir Connect loads `sapjoyuniapis_t4n2_x86_v2608251.dll` (1.16.7; 1.14.5 had `…_v2606151`)
via FFI. The DLL embeds hidapi and libusb. Findings from Ghidra (12.1.4, headless):

**Opening the device.** `FUN_10053840(vid, pid, usage_page)` with VID `0x3537` and usage page
`0xFF7A`, trying PIDs `0x100F`, `0x1098`, `0x1141`, `0x10BD`, `0x10ED`, `0x10EF` in order. Some
functions branch first: a `0xFF7A` device with PID `0x105A` takes a T3XS-style path
(`FUN_10037170`), PID `0x10BE` another (`FUN_10039d60`). PID `0x100F` uses the generic path below.

**Transfer** (`FUN_10053c20`): drain queued input reports (1 ms reads, logged as "drop N pkgs"),
`hid_write` a 64-byte report (hidapi report ID 0 first, so 65 bytes to hidraw), then
`hid_read_timeout(200 ms)` up to 11 times, **skipping reports whose first byte is `0xF4`**. The
first other report is the reply; there is no echo or checksum check.

**Commands** are `[cmd, action, …]` with action `00` get, `01` set:

| Bytes | DLL function |
|---|---|
| `01 01` | GetVersion / GetDeviceInfo / GetMacAddress (one query, three parsers) |
| `f5 00` / `f5 01` | Get / SetGamepadParam |
| `f6 00` / `f6 01` `f6 02` / `f6 03` `f6 04` | GetMacroStep / SetMacroStep / RecordMacro |
| `1e …` | SetABXYRotationTest |
| `f2 00 04 f6` (raw `hid_write`) | UpdateFirmware: **never send** |

`SetISPMode` does nothing for PID `0x100F` (only the `0x105A` / `0x10BE` paths act).

**`01 01` reply, confirmed on hardware (wired, 2026-10-23), reply in 1 ms:**

```
01 df 25 f1 38 8d  01 00  00  00  77 01  00 00 00 00  04 02  ff  00 …
MAC [0:6]          hw     radio   fw     date         GCM    battery
```

This is the 19-byte form of GCM's basic-info reply, so `gcm.basic_info_parse(reply[:19])`
parses it unchanged: firmware `7701`, GCM version 2.4. Connect turns the firmware bytes into a
hex string (`"7701"`), and a trailing `01` marks the international build. Byte 18 is battery:
`255` = charging (Connect's `chargeState: 255 == battery`); the wired PID `0x100F` uses this,
other models use bit 7 (`supportRichCharginInfo`).

**GCM works on this channel** (confirmed 2026-10-23, `gamesir-linux usb`, `hid.py`). The
section 1 frames are sent zero-padded to 64 bytes. Replies come in two shapes:

| Shape | Example | Checksum |
|---|---|---|
| A fresh GCM frame | `24 01 05 52 7c 00 …` (host info) | inside the frame, at `length - 1` |
| The request echoed with the value filled in | `07 01 06 00 05 0e 00 … 21` (dead zone) | byte 63, over the whole report; the in-frame checksum is the request's |
| No length byte, one byte longer than the request | `1f 00 01 00 00 00 20 00 …` (key mapping L4) | last byte of the frame, then zero padding |

Read-only results from one controller (firmware 7701), i.e. its out-of-the-box values:

| Setting | Value | What it says about units |
|---|---|---|
| Dead zones, stick inner / outer / anti | 5 / 0 / 0 | |
| Dead zones, trigger front / back | 5 / 95 | percent on a 0–100 scale; "back" looks like the full-press point, not an end dead zone (unconfirmed) |
| Vibration, grips / triggers | 75 / 0 | percent, **not** 0–4 |
| Features (`0x23`) | all off except auto-power-on-dock | |
| Host info battery | `0x52` = 82, bit 7 clear | on the cable, yet "not charging" by the bit-7 rule; basic info byte 18 said `0xff` |
| Auto-sleep (`0x26`) | no reply | not supported over USB, or not on this firmware |
| Config slots (`0x29`) | no reply | the GUI hides profiles over USB |
| Trigger quick mode (`0x21`) | both off | |
| Home light (`0x05`) | white, effect 1 | **writes work**: set to red from the GUI, read back as (255, 40, 40) |
| Back buttons L4 / R4 (`0x1F`) | scan 0, flag 0: not assigned | |

Still to decode: the `f5` gamepad-param and `f6` macro layouts.

### 3. USB firmware update: JieLi "UDisk" mode

Flow in GameSir Connect:
1. The controller is put into update mode: hold **M + X + B** while plugging in,
   or the DLL sends `SetISPMode` ("Switch MCU UDisk").
2. It re-enumerates as a USB mass-storage device.
3. JieLi's own `jl_firmware_upgrade_x86.dll` ("Firmware Upgrade DLL 1.21") pushes the `.ufw`
   file with SCSI pass-through using vendor opcodes `0xFD 0xFB 0xFC`.

On Linux this is `SG_IO` on `/dev/sg*`, no kernel driver needed.
Prior art: [kagaimiq/jl-uboot-tool](https://github.com/kagaimiq/jl-uboot-tool) (MIT) speaks
JieLi's USB loader protocol and lists AC695N as working. It's worth trying first for a
**full flash dump**, which would give a backup to restore from.

## Firmware files

**Where new firmware comes from:** GameSir Connect contains a remote firmware feed
(`updateRemotelyFirmwareConfig` / `UPDATE_FIRMWARE_PACKAGE`, OSS endpoint
`https://gamesir-app.oss-accelerate.aliyuncs.com/pc/`), but `appConfig.enableFirmwareUpdate` is
`false` and nothing ever calls it. Firmware only ships inside app releases, announced by
electron-updater at `https://pc-connect-update.gamesir-cdn.com/latest.yml` with installers at
`<base>/<version>/<file>` (the app also tries `<base>/releases/<version>/`). `updater.py` follows
that feed.

Bundled in the installer under `resources/dfu/packages/`:

| File | Target | Version | Notes |
|---|---|---|---|
| `T4Nlite2_V182_2026-04-24.ufw` | controller | 8201 | International build; requires installed ≥ 5601 |
| `T4Nlite2_V82_2026-04-23.ufw` | controller | 8200 | China build; requires installed ≥ 5600 |
| `T4N_2_LITE_DOG_V28_2025_11_05.ufw` | dongle | 2800 | |

- The firmware payloads are encrypted for the chip. **The tool never decrypts them**, only
  verifies and delivers them.
- The two regional builds differ in about 66% of their bytes. **The last digit of the version
  appears to be the region** (0 = China, 1 = International), inferred from GameSir's table.
  The tool refuses to cross regions.
- The MD5s listed in Connect's JS do not match the shipped controller files, and the app never
  checks them. The tool uses SHA-256 and the container's own CRCs instead.
- `ZW.ufw` (VID `N2LT`) and `AC632N.ufw` (VID `N2DG`) are generic **rescue images** the app picks
  by chip and key group when a device can't identify itself. Version `1.0.0` is a placeholder.
  Never select them automatically.

## .ufw container format (cracked, implemented in `src/gamesir_linux/ufw.py`)

Verified against all 44 `.ufw` files in GameSir Connect 1.14.5 for the header and table CRCs,
and fully (every partition) for the 8 files of the Nova 2 Lite / Super Nova family.

**ENC scrambling:** XOR with a keystream from a 16-bit shift register using the CRC-16 polynomial
`0x1021` (keystream byte = low byte, shift left once per byte). Symmetric.

```
0x00  header, 64 bytes, ENC key 0xFFFF
      u16 header_crc    CRC-16/XMODEM of descrambled header[2:64]
      u16 table_crc     CRC-16/XMODEM of the raw (scrambled) entry table
      u32 file_size
      u16 entry_count
      u16 ? (4)   u32 ? (0x200)
      char chip[16]     "AC695X" controller, "AC632N" dongle
0x40  entry table: entry_count x 80 bytes, each ENC key 0xFFFF
      u8 type, u8 flags, u16 index, u16 crc, u16 0, u32 offset, u32 length, u32 padded_length,
      ..., char name[16] at +64
```

Nova 2 Lite controller partitions: `info.log`, `isd_config.ini`, `ota.bin`, `script.ver`,
`flash.bin`, `flash2.bin` (same size as `flash.bin`, likely a backup bank), `farg.cfg`,
`blimit.bin`, `tail.bin` (ends exactly at end of file).

**Partition CRCs:** CRC-16/XMODEM of the raw payload for firmware partitions. Small config
partitions are block-scrambled and their CRC covers the descrambled bytes: each 32-byte block
uses ENC with key `0x3496 ^ (absolute_file_offset >> 2)`.

**Device identity**, from `isd_config.ini`:

| Firmware | `PID` | `VID` |
|---|---|---|
| Nova 2 Lite controller | `AC695X_TRANS` | `N2LT` |
| Nova 2 Lite dongle | `AC632N_TRANS` | `N2DG` |
| Super Nova controller / dongle | `AC695X_TRANS` | `N2S` / `NSDG` |

These match GameSir's `UdiskProductName` / `UDiskVersionId` metadata, and are what JieLi's
updater compares against the device (`JL_getUfwPidVid` vs `JL_queryDevicePidVid`) before
flashing. `script.ver` holds the JieLi build-tool version (`AC695X-v0.01-cfg_tool-v0.08`).

## Safety rules for the flasher (implemented in `safety.py`)

1. File must match its manifest SHA-256 and pass every container CRC.
2. Controller firmware only on controllers, dongle firmware only on dongles.
3. Device-reported JieLi PID and VID must equal the firmware's; unknown identity blocks.
4. Region digit must match; enforce the `MinimumVersion` chain; no downgrades;
   same version only with `--reinstall`.
5. Warn when the dongle is older than the dongle firmware shipped in the same release.
6. Still to build: flash dump before the first write (if `jl-uboot-tool` can reach it),
   and a dry run that does everything except the final write.

## Open questions (need hardware)

- Dongle PID in green mode (Connect expects `0x1098`), and the update-mode identities
- What `3537:2106` (idle dongle, same vendor channel) answers to
- Whether replies really are read from `0x865F` (vs. notified); `ble.NOTIFY_REPLIES` flips it
- Units: the full range of dead zones and vibration (percent, but is 100 the maximum?), what trigger
  "back" means, auto-sleep (assumed minutes; no reply over USB), battery encoding (bit 7 looks wrong)
- Whether the BLE config channel works from BlueZ while the controller is in PC (XInput) mode
- SAPJoy HID report layouts
- Whether UDisk mode exposes enough of the loader protocol for a full flash dump
