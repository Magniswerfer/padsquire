# SPDX-License-Identifier: AGPL-3.0-or-later
"""Update mode over raw USB (macOS): JieLi's SCSI commands in USB mass storage Bulk-Only Transport."""
import struct

import pytest

pytest.importorskip("usb")

from padsquire import jlboot  # noqa: E402

INQUIRY = bytes(8) + b"BR23    UBOOT1.00       1.00"


class Endpoint:
    def __init__(self, address, device):
        self.bEndpointAddress, self.bmAttributes, self.device = address, 0x02, device   # bulk

    def write(self, data, timeout):
        return self.device.host_wrote(bytes(data))

    def read(self, size, timeout):
        return self.device.host_reads(size)


class Interface(list):
    bInterfaceNumber, bInterfaceClass, bInterfaceSubClass, bInterfaceProtocol = 0, 0x08, 0x06, 0x50


class Ctx:
    def managed_claim_interface(self, device, intf):
        device.claimed = intf

    def managed_release_interface(self, device, intf):
        device.claimed = None

    def dispose(self, device, close_handle=True):
        device.disposed = True


class FakeDisk:
    """Answers CBWs: INQUIRY with its data, anything else with no data; status from `fail`."""

    def __init__(self, driver_attached=True, fail=False):
        self._ctx, self.claimed, self.disposed = Ctx(), None, False
        self.driver, self.fail, self.queue, self.cbws, self.data_out = driver_attached, fail, [], [], []
        self.expect_data_out = 0
        self.intf = Interface([Endpoint(0x81, self), Endpoint(0x02, self)])

    def get_active_configuration(self):
        return [self.intf]

    def is_kernel_driver_active(self, i):
        return self.driver

    def detach_kernel_driver(self, i):
        self.driver = False

    def attach_kernel_driver(self, i):
        self.driver = True

    def host_wrote(self, data):
        if self.expect_data_out:
            self.data_out.append(data)
            self.expect_data_out = 0
            self.queue.append(self._csw())
            return len(data)
        sig, tag, length, flags, lun, n = jlboot.CBW.unpack(data[:15])
        assert sig == b"USBC" and len(data) == 31 and lun == 0
        self.tag, cdb = tag, data[15:15 + n]
        self.cbws.append((cdb, length, flags))
        if flags & 0x80:
            self.queue.append((INQUIRY if cdb[0] == 0x12 else bytes(16))[:length])
            self.queue.append(self._csw())
        elif length:
            self.expect_data_out = length
        else:
            self.queue.append(self._csw())
        return len(data)

    def _csw(self):
        return jlboot.CSW.pack(b"USBS", self.tag, 0, 1 if self.fail else 0)

    def host_reads(self, size):
        return self.queue.pop(0)


def test_inquiry_over_bulk_only_transport():
    disk = FakeDisk()
    dev = jlboot.UsbBotDevice(disk)
    assert disk.claimed == 0 and not disk.driver                  # taken from macOS's disk driver
    assert jlboot.JLBoot(dev.execute).inquiry() == ("BR23", "UBOOT1.00", "1.00")
    cdb, length, flags = disk.cbws[0]
    assert cdb == bytes([0x12, 0, 0, 0, 36, 0]) and (length, flags) == (36, 0x80)
    dev.close()
    assert disk.claimed is None and disk.driver and disk.disposed   # and handed back


def test_data_out_and_a_failed_status():
    disk = FakeDisk(driver_attached=False)
    dev = jlboot.UsbBotDevice(disk)
    assert dev.execute(b"\xfb\x06" + bytes(14), b"\x01\x02", 0) == b""
    assert disk.data_out == [b"\x01\x02"] and disk.cbws[0][1:] == (2, 0x00)
    disk.fail = True
    with pytest.raises(jlboot.JLBootError, match="status 1"):
        dev.execute(bytes([0x12, 0, 0, 0, 36, 0]), None, 36)
    dev.close()
    assert not disk.driver                                         # it had none to give back


def test_uboot_devices_from_usb_ids(monkeypatch):
    class D:
        def __init__(self, pid, address):
            self.idProduct, self.bus, self.address = pid, 1, address
    monkeypatch.setattr(jlboot, "MACOS", True)
    monkeypatch.setattr(jlboot, "_usb_devices", lambda: [D(0x2342, 5), D(0x3442, 6), D(0x1234, 7)])
    assert [(d.path, d.vendor, d.chip) for d in jlboot.find()] == [("usb:1:5", "BR23", "br23"), ("usb:1:6", "BD19", "bd19")]
    assert len(jlboot.disks()) == 3
