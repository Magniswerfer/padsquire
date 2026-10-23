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

### 3. USB firmware update: JieLi UBOOT1.00 (decoded from the DLLs, not yet run)

Decoded 2026-10-23 from `sapjoyuniapis_t4n2_x86_v2608251.dll` (GameSir) and
`jl_firmware_upgrade_x86.dll` (JieLi), cross-checked against
[kagaimiq/jl-uboot-tool](https://github.com/kagaimiq/jl-uboot-tool) (MIT) and its docs.

**Entering update mode.** For PID `0x100F` GameSir's `UpdateFirmware` writes **`f2 00 04 f6`**
on the `0xFF7A` HID channel (a GCM-style frame: command `f2`, length 4, checksum), closes the
handle and waits for a mass-storage device ("existUDisk"). `SetISPMode` does nothing for this PID.
Older notes also list holding **M + X + B** while plugging in. On BR23 chips the firmware can only
hand over to the **mask-ROM "UBOOT1.00" download mode** (via an NVRAM boot tag), so the controller
should re-enumerate as a USB disk whose SCSI INQUIRY says vendor `BR23`, product `UBOOT1.00`.
The ROM calibrates its clock from USB SOF packets, which other full/low-speed devices on the same
bus can disturb; a USB 2.0 hub isolates it.

**Recovery.** UBOOT1.00 is in mask ROM, and a BR23 that finds no valid firmware in flash enters
it by itself. An interrupted flash should therefore leave the controller recoverable over USB.
Not yet verified on this controller.

**Protocol.** SCSI vendor commands over `SG_IO` (16-byte CDBs, big-endian fields):

| CDB | Meaning | JieLi DLL method |
|---|---|---|
| `FB 06 addr32 size16 – crc16` | write RAM (loader upload; BR23 ROM wants MengLi-encrypted data) | `FUN_1000ae40` |
| `FD 07 addr32 size16` | read RAM | `FUN_1000ad80` |
| `FB 08 addr32 arg16` | jump (start the loader) | `FUN_1000b190` |
| `FC 0A` | online device: flash type and JEDEC ID | `FUN_1000c140` |
| `FC 09 00 AC 69 00` | read chip key, unlocks flash reads | `FUN_1000c4d0` |
| `FC 14` | USB buffer size | `FUN_1000c6e0` |
| `FD 05 addr32 size16` | read flash (loader) | |
| `FB 01 addr32` / `FB 42 addr32` | erase 4 KB sector / smaller erase near the end | `FUN_1000bf60` / `FUN_1000bd80` |
| `FB 04 addr32 size16 – crc16` | write flash | `FUN_1000b440` |
| `FC 13 addr32 size16` | flash CRC16, raw (verification; jl-uboot-tool's `FLASH_CRC16_RAW`) | `FUN_1000b2f0` |
| `FC 0C addr32` | run app / reset | `FUN_1000c7e0` |
| `FC 85`, `FC 86`, `FC 1A 01` | vendor reads used while building the image (not decoded) | |
| **`FC 12 key16`** | **write chip key into eFuses: irreversible. The DLL never calls it** (checked in the decompiled code and the machine code) | `FUN_1000c5e0` |

The upgrade thread (`FUN_10007260`) checks the device is `uboot1.00`, reads its chip name and
compares it with the `.ufw`'s, uploads the chip's loader (`br23`: address **`0x12000`**, the same
as jl-uboot-tool's), then builds a full flash image and burns it with `FUN_1000b560`:

1. Erase `0x1000`, write the image's 64-byte header there and CRC-check it; only then erase `0`.
2. Erase `0x2000`…end of the writable area, write the image top-down in 4 KB chunks.
3. Rewrite sectors `0x0000` and `0x1000` last.
4. CRC16-verify every 4 KB chunk (`FC 13`); retry the whole burn up to 5 times; `FC 0C` to restart.

**Our read-only side** (`jlboot.py`, `gamesir-linux uboot`; tested against a simulated chip only).
The ROM answers only `FB 06`, `FD 07` and `FB 08`; everything else, restart included, needs the
loader running. The loader file is sent as-is: the BR23 ROM decrypts MengLi uploads in place
(jl-uboot-tool's `memory-rw-mengli-crypt` quirk). Before uploading, a 64-byte `FD 07` at the
loader address tells whether the loader is already running: the ROM MengLi-encrypts memory
reads and the loader doesn't, so the loader's first bytes only read back decrypted from the
loader itself. `backup` reads the flash twice in 4 KB steps, compares the reads, then checks
each sector against `FC 13`; if every sector disagrees the CRC variant is not XMODEM, which the
first dump will settle.

**Confirmed on hardware (2026-10-23, firmware 7701):**

| What | Result |
|---|---|
| `f2 00 04 f6` on the `0xFF7A` channel | the controller drops off USB and is back within ~1 s as `4c4a:2342`, "BR23UBOOT1.00", full speed, `usb-storage` |
| SCSI INQUIRY | vendor `BR23`, product `UBOOT1.00`, revision `1.00` |
| Loader upload and jump | br23loader from jl-uboot-tool at `0x12000`, argument `0x0001`, sent as-is: works; RAM then reads back as the decrypted loader |
| SCSI residue | **unusable**: the ROM sends the data but reports all of it as residue (`resid` = length), so it is ignored |
| Chip key (`FC 09`) | `0x3496`, the same as the block-scrambling key base of the `.ufw` config partitions |
| Online device (`FC 0A`) | type 3 (SPI NOR on SPI0), JEDEC ID bytes `14 60 85`: Puya (`0x85`), capacity `0x14` = **1 MB** |
| USB buffer size (`FC 14`) | 32768 bytes |
| Full dump (`gamesir-linux uboot backup`) | 1 MB read twice in 5 s, both reads identical |
| Flash CRC (`FC 13`) | **CRC-16/XMODEM** (`jlcrypt.crc16`): matched the dump in all 256 sectors |

First look at the dump (kept outside the repo): sector `0x0000` is blank (all `FF`); the image
header is at `0x1000` with the chip identity `AC695X_TRANS` in plain ASCII; the last used
sector is `0x7B000`, about half the flash; 154 of 256 sectors are blank.

**Restart** (`FC 0C`, argument 1) boots the firmware in flash. The controller first shows up
briefly as `3537:2106` "Zikway HID zk" (the identity seen before only on the idle dongle), then
as `3537:100F` in green mode within about a second. Afterwards firmware, MAC and settings were
unchanged (`7701`, dead zone 5). A 2.4 GHz dongle plugged in elsewhere re-enumerated at the
same moment.

**Flash layout, from the dump** (2026-10-23, firmware 7701; offline analysis, nothing sent).
The flash is 1 MB but the layout only uses the first 512 KB; `0x80000`–`0xFFFFF` is blank.

| Range | What | Notes |
|---|---|---|
| `0x00000`–`0x00FFF` | blank | `NEED_RESERVED_4K=YES` |
| `0x01000` | flash header, 32 bytes | `N2LT` and `AC695X_TRANS` in plain text; the CRC fields don't match a plain CRC-16 of the raw bytes (not decoded) |
| `0x01020` | file table, 4 x 32 bytes, each ENC key `0xFFFF` | `u8 type, u8 ?, u16 crc, u32 offset, u32 length, u32 index, char name[16]`; offsets relative to `0x1000` |
| `0x010A0` | `uboot.boot`, `0x1FE8` bytes | second-stage boot; CRC field not a plain CRC-16 of the raw bytes |
| `0x03088` | `isd_config.ini`, `0x89` bytes | CRC-16/XMODEM of the raw bytes matches |
| `0x04000` | `app_dir_head`: the app area | scrambled per 32-byte block with ENC key **`chip_key ^ ((addr - 0x4000) >> 2)`**, chip key `0x3496` |
| `0x04000`–`0x63FFF` | `PRCT` ("CODE"): `app_area_head`, `app.bin` (mapped at `0x1E00120`, the ini's `ENTRY`), `cfg_tool.bin` | the last 32 bytes (`0x63FE0`) hold runtime data that isn't in GameSir's image (see below) |
| `0x64000`–`0x77FFF` | `VM`, settings store | starts `55 AA AA 55`; 0x1B7 bytes used |
| `0x79000` | `BTIF` | `22 66 60 00` then `8d 38 f1 25 df d9`, likely the Bluetooth address |
| `0x7A000` | `EXIF` | blank |
| `0x7B000`–`0x7EFFF` | `USERIF` ("FEND") | ~930 bytes of 16-bit values `0x200`–`0x3C0`: looks like factory stick/trigger calibration |
| `0x7F000` (file table) | `key_mac` | blank |

**Which image:** `flash.bin` is laid out from address 0 (`PRCT` at 0, `VM` at `0x63000`);
`flash2.bin` from `0x1000` (`PRCT` at `0x1000`, `VM` at `0x64000`), the layout on this
controller. JieLi's DLL presumably picks one with `JL_queryDeviceConfigEOffset` (the ini has
`EOFFSET = 1`). **This controller takes `flash2.bin`, written at `0x1000`.**

**What an 8201 update would change:** the 8201 `flash2.bin` (`0x63000` bytes) covers exactly
`0x1000`–`0x63FFF`. Its boot area (`0x1000`–`0x3FFF`: header, file table, `uboot.boot`,
`isd_config.ini`) is byte-identical to this controller's, and its app directory decrypts with
key `0x3496`, so it ships scrambled for this chip key and needs no re-encryption. Every
reserved area (`VM`, `BTIF`, `EXIF`, `USERIF`, `key_mac`) is at the same address in 7701 and
8201, outside the image.

**The last 32 bytes of the code area.** GameSir's image has 6 bytes at `0x63FF8`; this
controller has `a4 00 30 a0 07 00 00 00` at `0x63FE0`, `22 84` at `0x63FF0` and 8 bytes at
`0x63FF8`. They don't follow the record JieLi's updater writes there (below), so they were
probably written by the factory burner. Not decoded; the official update overwrites them.

**How GameSir Connect 1.16.7 actually flashes the T4N2** (from `main.js`, the worker scripts
in `resources/dfu/` and `jl_firmware_upgrade_x86.dll`; static analysis):

- GameSir's DLL is **not** what burns the flash. Its `UpdateFirmware` imports JieLi ordinals
  15, 9, 5, 3, 6, which in the shipped JieLi DLL are `setDualUbootEnabled`,
  `queryDeviceConfigEOffsetByPath`, `getUfwDataCrc`, `getUfwConfigEOffset`, `getUfwPidVid`: it
  was linked against an older JieLi DLL without the three `ConfigEOffset` exports. Its log
  strings (`JL_loadUfwData`, `JL_upgradeDevice`) fit that older numbering. Only its `f2 00 04 f6`
  write matters.
- Two update workers exist. `upgradWoker.js` drives a second library, `JL_upgradeUtility_x86.dll`,
  over HID (reports prefixed `0f 00 00 3c f0 00 00 00`), through the running firmware.
  `udiskUpgradWoker.js` drives `jl_firmware_upgrade_x86.dll` by export name against the
  download-mode disk: `JL_loadUfwData`, `JL_queryDevicePidVid` (must equal the `.ufw`'s
  PID/VID), then **`JL_upgradeDevice(udisk, false, true, null)`**. The T4N2 uses the disk path:
  its code reports progress as `UDiskUpgradeProgressChange` / `UDiskUpgradeFinished`, and its
  device entry carries `UDiskVersionId: "N2LT"`, `UdiskProductName: "AC695X_TRANS"`.
- `JL_loadUfwData` (`FUN_1000ded0`) verifies the `.ufw`, checks `tail.bin` (magic
  `0x36659213`/`0x4273`), and files each firmware partition by two fields: header byte `0x0D`
  (`0x01` or `0x10`; ours `0x10`) and the CRC field of its `app_dir_head` entry (`0xFFFF` =
  laid out from 0, `0x00FF` = from `0x1000`, `0x0FFF`/`0xFF00` = from `0x10000`). The `.ufw`'s
  `EOFFSET` (`isd_config.ini`, 1 → `0x1000`) picks one: **`flash2.bin`** for the Nova 2 Lite.
- The upgrade thread (`FUN_10007260`) checks the device's flash is at least the image header's
  size field (`0x7F000`), overwrites the image's **last 16 bytes** with a runtime record (header
  byte 2 > `0x0F`; decoded below), then **prepends
  `EOFFSET` bytes** (`FUN_10008c50`), giving a `0x64000`-byte image from address 0.
- The burn (`FUN_1000b560`): header first into the spare sector (sector 0 here), CRC-checked;
  erase `0x2000` up to the **image length** (the first `false`; with `true` it would erase up to
  the flash size minus `EOFFSET`, VM and calibration included); write top-down in 4 KB chunks;
  rewrite `0x1000` then `0`; CRC16 (`FC 13`) every 4 KB from 0 to the image end; retry up to 5
  times; `FC 0C` restart (the `true`). **Everything from `0x64000` up is never erased or written.**

**The 16-byte record** (`std::string::replace` of the image's last 16 bytes, so its size
stays): `"dll\0"` (a string the DLL copies over its `FF` defaults), `01 15` (`0x1501`), the
erase-all flag (`00`), `FF`, then 6 bytes from the `.ufw`'s `JLDGAUTH` block, then `FF FF`. The
block is `blimit.bin` (type `0xA1`, 144 bytes): block-descrambled like the other config
partitions, then ENC with the 16-bit key at its offset `0x0E` (those two bytes stay as they
are); it starts `JLDGAUTH`, has a CRC-16 of `0x0C`–`0x8F` at `0x0A`, and if flag bit 2 at `0x1E`
is set the record takes its bytes `0x18`–`0x1D`. For 8201 the record is
`646c6c00 0115 00ff 855682ed e98c ffff`; the last 8 bytes are already in `flash2.bin`, so only
`0x63FF0`–`0x63FF7` change.

**Writing, confirmed on hardware (2026-10-23):** `uboot test-write` erased, wrote and erased
back sector `0xFF000` (device CRC and read-back matched; whole flash unchanged afterwards), then
`uboot restore` burned the backup's own `0x0000`–`0x63FFF` with the full JieLi sequence: every
sector verified, everything above `0x64000` unchanged, and after `FC 0C` the controller booted
7701 with the same MAC and settings. **M + X + B** while plugging in also reaches download mode,
but through the firmware (it enumerates as `2106`, then `100F`, then `BR23 UBOOT1.00`), so it is
no way back if the firmware itself is broken.

**Writing** (`jlwrite.py`): sector erase `FB 01 addr32`
(reply echoes the command) and flash write `FB 04 addr32 size16 00 crc16` with the data CRC
little-endian, as jl-uboot-tool sends it; JieLi's DLL puts it big-endian at bytes 9–10 instead.
Loaders from BR17 on are documented to ignore it, and every sector is CRC-checked afterwards
either way. After each erase the sector's CRC must equal that of 4 KB of `FF`, as JieLi checks.

The JieLi DLL's obfuscated strings are hex, then ENC with key **`0x6D52`** (an earlier note here
said `0x7117 ^ (pos >> 2)`; that was wrong).

The JieLi DLL's obfuscated strings decode (see below) to the loader resource type `LOADER`, `uboot1.00`, the chip names `br22 br23 br25 br27 br30 bd19
bd29 uc03` (one wrapped loader resource each), `JLDGAUTH` and `jielikeji2010ipo`.

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
6. A verified flash backup of the same controller is required (`plan --backup`). From it,
   exactly one image in the `.ufw` must fit: its code starts where the controller's does, it
   decrypts with the controller's chip key, it ends inside the code area, and `VM`, `BTIF`,
   `EXIF`, `USERIF` and `key_mac` are at the same addresses and outside the write range
   (`jlflash.check_update`). For the Nova 2 Lite that is `flash2.bin` at `0x1000`–`0x63FFF`.
7. The flasher (`uboot flash`, `uboot restore`, `uboot test-write`) first proves it is the
   backup's controller (chip key, flash ID, and the `BTIF` and `USERIF` sectors byte for byte),
   writes only inside `[0, end of the code area)`, retries a burn that fails its CRC check up to
   3 times, then reads the whole flash back twice: the code area must equal the image and
   everything above it must be unchanged. It never restarts the controller by itself.

## Open questions (need hardware)

- Dongle PID in green mode (Connect expects `0x1098`), and the update-mode identities
- What `3537:2106` (idle dongle, same vendor channel) answers to
- Whether replies really are read from `0x865F` (vs. notified); `ble.NOTIFY_REPLIES` flips it
- Units: the full range of dead zones and vibration (percent, but is 100 the maximum?), what trigger
  "back" means, auto-sleep (assumed minutes; no reply over USB), battery encoding (bit 7 looks wrong)
- Whether the BLE config channel works from BlueZ while the controller is in PC (XInput) mode
- SAPJoy HID report layouts
- A full flash dump through the BR23 loader; then the image-building details listed in section 3
