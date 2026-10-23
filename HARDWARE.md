# First contact with a real Nova 2 Lite

Everything in this guide is read-only or reversible. There is no flash command yet, on purpose:
it comes after this checklist has confirmed the USB update mode.

## 0. Install

```sh
git clone https://github.com/Magniswerfer/gamesir-linux && cd gamesir-linux
sudo dnf install 7zip
pip install --user -e '.[ble]'
sudo cp contrib/udev/70-gamesir.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

## 1. Firmware feed (no controller needed)

```sh
gamesir-linux update
```

First real test of GameSir's update feed: it should download GameSir Connect, verify its SHA-512
and extract three firmware files. If it fails, the error message names every URL it tried.

## 2. Guided probe (about 10 minutes)

```sh
gamesir-linux probe --guided
```

It walks through each connection mode (cable in XInput/DInput/Switch, dongle, both update modes,
Bluetooth) and saves a `gamesir-probe-*.json` report. **Share that file.** It answers:

| Question | Where in the report |
|---|---|
| Which USB product ID belongs to which mode | `usb[].pid` per snapshot |
| Whether Linux binds a gamepad driver (xpad / hid-generic) | `interfaces[].driver`, `input` |
| The HID layout of the configuration interface (needed for dongle config) | `hid[].report_descriptor` |
| What update mode looks like: disk vendor/model, JieLi IDs | `storage[]` in the update-mode snapshots |
| The real Bluetooth service map | `bluetooth.services` |
| Whether replies are read back or notified | `bluetooth.experiments[].verdict` |

The probe never changes a setting. Over Bluetooth it only sends three queries (info, battery and
one dead-zone read), and only to the characteristic the official app uses.

## 3. Bluetooth configuration

In Bluetooth mode (try Android mode first: Home + A, LED yellow):

```sh
gamesir-linux ble scan
gamesir-linux ble info
gamesir-linux ble deadzone left-stick-inner        # note the value
gamesir-linux ble deadzone left-stick-inner 3      # change it (read back automatically)
```

If `scan` finds nothing, pair once with `bluetoothctl` and retry. If `info` times out but the
probe's verdict says `notify:865f`, set `NOTIFY_REPLIES = True` in `src/gamesir_linux/ble.py`.

## 4. Do settings survive switching to the dongle?

1. Over Bluetooth, set something you can feel, e.g. `gamesir-linux ble feature swap-sticks on`.
2. Switch to the 2.4 GHz dongle and check the sticks are swapped (e.g. in Steam's controller test).
3. Back in Bluetooth: `gamesir-linux ble feature swap-sticks off`.

If the swap carries over, "configure over Bluetooth, play on the dongle" works today.

## If something goes wrong

Hold Home + M for about 10 seconds to force the controller off. `gamesir-linux ble restore --yes`
resets every setting to factory defaults.
