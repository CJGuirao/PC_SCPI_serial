"""Raw USB HID transport for OWON HDS200 / HDS300 oscilloscopes.

The scope enumerates as a two-interface composite device, VID 0x5345 /
PID 0x1234: interface 0 is a vendor-defined HID interface carrying ASCII SCPI
inside 64-byte interrupt reports (OUT 0x01, IN 0x81), interface 1 is mass
storage.  Verified on an HDS271, firmware V1.3.0: writing b"*IDN?" to 0x01 and
reading 0x81 returns b"OWON,HDS271,25520161,V1.3.0\n".

pyusb reaches the device through whatever driver Windows has bound -- including
libusb0.sys installed by Zadig -- so no COM port and no HID class driver are
needed.  This instrument has no serial interface at all.

Writes need their own generous timeout: measured on hardware, a 500 ms
interrupt OUT transfer times out, 3 s succeeds.  Reads must stay short so that
"no reply" is distinguishable from a slow one.

HdsHidTransport implements the subset of the pyserial Serial API that
OWONScopeController uses (write, read, readline, flush, close and a settable
``timeout), so it can stand in for a Serial object unchanged.
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional

REPORT_SIZE = 64
OWON_VID = 0x5345
HDS_PID = 0x1234
INTERFACE_HID = 0


class HdsUsbError(RuntimeError):
    """Raised when the USB HID transport cannot be used."""


def _usb_modules():
    try:
        import usb.core
        import usb.util
    except Exception as exc:
        raise HdsUsbError("pyusb is not installed: %s" % exc)
    return usb.core, usb.util


def _backend():
    """Prefer the libusb-1.0 DLL bundled by the libusb-package wheel."""
    try:
        import libusb_package
        backend = libusb_package.get_libusb1_backend()
        if backend is not None:
            return backend
    except Exception:
        pass
    try:
        import usb.backend.libusb1 as libusb1
        return libusb1.get_backend()
    except Exception:
        return None


def list_owon_devices(vid: int = OWON_VID) -> List[dict]:
    """Every attached OWON device, as plain dicts (safe to print or log)."""
    usb_core, usb_util = _usb_modules()
    devices = []
    for dev in usb_core.find(find_all=True, idVendor=vid, backend=_backend()):
        info = {"vid": dev.idVendor, "pid": dev.idProduct, "product": None, "serial": None}
        for key, attr in (("product", "iProduct"), ("serial", "iSerialNumber")):
            try:
                info[key] = usb_util.get_string(dev, getattr(dev, attr))
            except Exception:
                pass
        devices.append(info)
    return devices


class HdsHidTransport:
    """File-like SCPI channel over the scope's HID interface."""

    def __init__(self, vid=OWON_VID, pid=HDS_PID, serial=None, timeout=2.0, write_timeout=3.0,
                 report_size=REPORT_SIZE):
        self.vid = vid
        self.pid = pid
        self.serial = serial
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.report_size = report_size
        self.is_open = False
        self.description = ""
        self.last_error = None
        self._device = None
        self._interface = None
        self._ep_in = None
        self._ep_out = None
        self._buffer = bytearray()
        self._lock = threading.RLock()

    # --------------------------------------------------------------- opening
    def open(self) -> bool:
        usb_core, usb_util = _usb_modules()
        device = None
        for candidate in usb_core.find(find_all=True, idVendor=self.vid, idProduct=self.pid, backend=_backend()):
            if self.serial is None:
                device = candidate
                break
            try:
                if usb_util.get_string(candidate, candidate.iSerialNumber) == self.serial:
                    device = candidate
                    break
            except Exception:
                continue
        if device is None:
            raise HdsUsbError(
                "no OWON device %04x:%04x%s found"
                % (self.vid, self.pid, " serial %s" % self.serial if self.serial else "")
            )

        try:
            device.set_configuration()
        except Exception:
            pass  # already configured

        configuration = device.get_active_configuration()
        interface = configuration[(INTERFACE_HID, 0)]
        try:
            usb_util.claim_interface(device, INTERFACE_HID)
        except Exception:
            pass  # libusb0 does not always report an exclusive claim

        ep_out = usb_util.find_descriptor(
            interface, custom_match=lambda e: usb_util.endpoint_direction(e.bEndpointAddress) == usb_util.ENDPOINT_OUT
        )
        ep_in = usb_util.find_descriptor(
            interface, custom_match=lambda e: usb_util.endpoint_direction(e.bEndpointAddress) == usb_util.ENDPOINT_IN
        )
        if ep_in is None or ep_out is None:
            raise HdsUsbError("HID interface is missing an endpoint pair")

        self._device = device
        self._interface = interface
        self._ep_in = ep_in
        self._ep_out = ep_out
        try:
            self.description = "%04x:%04x %s" % (
                device.idVendor, device.idProduct, usb_util.get_string(device, device.iProduct))
        except Exception:
            self.description = "%04x:%04x" % (device.idVendor, device.idProduct)
        self._buffer.clear()
        self.is_open = True
        return True

    # ------------------------------------------------------ pyserial surface
    def write(self, data) -> int:
        if not self.is_open:
            raise HdsUsbError("transport is not open")
        payload = bytes(data)
        if not payload:
            return 0
        with self._lock:
            for offset in range(0, len(payload), self.report_size):
                chunk = payload[offset:offset + self.report_size]
                if len(chunk) < self.report_size:
                    chunk = chunk + b"\x00" * (self.report_size - len(chunk))
                self._ep_out.write(chunk, timeout=self._write_timeout_ms())
        return len(payload)

    def flush(self):
        return None

    def read(self, size=1) -> bytes:
        """Binary-safe read of exactly ``size bytes (fewer on timeout)."""
        if not size or size < 0:
            size = 1
        with self._lock:
            deadline = time.time() + self._seconds()
            while len(self._buffer) < size:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                if not self._pump(int(min(remaining, self._seconds()) * 1000)):
                    break
            data = bytes(self._buffer[:size])
            del self._buffer[:size]
            return data

    def readline(self) -> bytes:
        """One LF-terminated text line, with the report padding removed."""
        with self._lock:
            deadline = time.time() + self._seconds()
            while True:
                index = self._buffer.find(b"\n")
                if index >= 0:
                    line = bytes(self._buffer[:index + 1])
                    del self._buffer[:index + 1]
                    while self._buffer[:1] == b"\x00":
                        del self._buffer[:1]
                    return line
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                if not self._pump(int(min(remaining, self._seconds()) * 1000)):
                    break
            data = bytes(self._buffer).strip(b"\x00")
            self._buffer.clear()
            return data

    def read_reports(self, max_reports=16, gap_ms=500) -> bytes:
        """Everything currently offered, stopping after one silent gap."""
        collected = b""
        with self._lock:
            for _ in range(max_reports):
                if not self._pump(gap_ms):
                    break
            collected = bytes(self._buffer)
            self._buffer.clear()
        return collected

    def clear_input(self):
        """Drop anything buffered from an earlier exchange."""
        with self._lock:
            self._buffer.clear()

    def close(self):
        with self._lock:
            try:
                if self._device is not None:
                    usb_util = _usb_modules()[1]
                    usb_util.release_interface(self._device, INTERFACE_HID)
                    usb_util.dispose_resources(self._device)
            except Exception:
                pass
            self._device = None
            self._interface = None
            self._ep_in = None
            self._ep_out = None
            self._buffer.clear()
            self.is_open = False

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False

    # -------------------------------------------------------------- internals
    def _seconds(self) -> float:
        try:
            value = float(self.timeout)
        except Exception:
            value = 2.0
        return value if value > 0 else 2.0

    def _timeout_ms(self) -> int:
        return max(1, int(self._seconds() * 1000))

    def _write_timeout_ms(self) -> int:
        try:
            value = float(self.write_timeout)
        except Exception:
            value = 3.0
        if value <= 0:
            value = 3.0
        return max(1, int(value * 1000))

    def _pump(self, timeout_ms: Optional[int] = None) -> bool:
        """Read one report into the buffer.  False when nothing arrived."""
        if not self.is_open or self._ep_in is None:
            return False
        timeout = self._timeout_ms() if timeout_ms is None else timeout_ms
        try:
            report = bytes(self._ep_in.read(self.report_size, timeout=timeout))
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            return False
        if not report:
            return False
        self._buffer.extend(report)
        return True
