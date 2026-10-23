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

USB vendor ID `0x3537`. Product IDs registered for T4N2: `0x100F`, `0x1141`, `0x105A`
(which PID is controller, dongle, or mode is still unknown; confirm with `lsusb`).

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

The `0x50`–`0x52` "UUID encryption" commands send the current Unix timestamp and date.
They look like registration bookkeeping, not an authentication gate. Verify on hardware.

Nova 2 Lite specifics (`Nova2LiteDevCfg`): macro loop needs firmware ≥ 6400, macro settings ≥ 6900.
Back buttons are key codes 459 and 461.

### 2. USB HID: "SAPJoy" protocol (Windows app; needs disassembly)

GameSir Connect does not use GCM over USB. It loads
`sapjoyuniapis_t4n2_x86_v2606151.dll` via FFI. That DLL contains an embedded hidapi and talks
to the controller with HID feature, output and input reports. The command set differs from GCM
(raw ids seen: `0x31` gyro, `0x32`, `0x34` stick curve).

Exported API, which is the feature list to replicate: key mapping, stick curves, stick mapping,
trigger params, vibration, lighting, D-pad params, macros, report rate, calibration, gyro,
config pages, `SetISPMode`, `UpdateFirmware`. Structs are in the JS (`main.js`, `SAPJOYUNI_*`).

Next step: disassemble the DLL's `SAPJoyUniDevice_*` functions, or capture USB traffic
(Windows VM + usbmon), to get report IDs and byte layouts.

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

Bundled in the installer under `resources/dfu/packages/`, no download server involved:

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

- Which PID is which: controller (wired), dongle, update mode
- Whether replies really are read from `0x865F` (vs. notified); `ble.NOTIFY_REPLIES` flips it
- Units: dead zones, vibration levels, auto-sleep (assumed minutes), battery encoding
- Whether the BLE config channel works from BlueZ while the controller is in PC (XInput) mode
- SAPJoy HID report layouts
- Whether UDisk mode exposes enough of the loader protocol for a full flash dump
