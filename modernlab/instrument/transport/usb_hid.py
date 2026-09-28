"""Raw USB HID transport for OWON HDS200 / HDS300 oscilloscopes.

The scope enumerates as a two-interface composite device, VID 0x5345 /
PID 0x1234: interface 0 is a vendor-defined HID interface (its report
descriptor uses vendor usage page 0xFF00 and declares only 1-byte reports, so
the 64-byte interrupt transfers are a raw vendor channel rather than HID
semantics), and interface 1 is mass storage.  ASCII SCPI travels in 64-byte
interrupt reports on interface 0 (OUT 0x01, IN 0x81, wMaxPacketSize 64,
bInterval 32 ms).  Verified on an HDS271, firmware V1.3.0: writing b"*IDN?" to
0x01 and reading 0x81 returns b"OWON,HDS271,25520161,V1.3.0\n".

pyusb reaches the device through whatever driver Windows has bound -- on this
host libusb0.sys, installed by Zadig -- so no COM port and no HID class driver
are needed.  This instrument has no serial interface at all.

Three transport rules, each learned the hard way on hardware:

1. Writes need their own generous timeout.  A 500 ms interrupt OUT transfer
   times out; 3 s succeeds.

2. A reply is one 64-byte report per transfer, and a read must ask for exactly
   64 bytes.  Requesting 128 or 512 bytes returns nothing at all: the endpoint
   rejects any transfer larger than wMaxPacketSize.

3. THE READ WINDOW IS TIGHT.  For a multi-report reply (a :DATA: payload) the
   firmware abandons the rest of the transfer if the host has not started
   reading within roughly 100 ms of the write.  Measured on hardware by
   delaying between the write and the first read:

       delay 0.00 - 0.10 s  -> 12/12 captures complete (475- and 600-byte payloads)
       delay 0.15 s         -> sometimes one 64-byte report only
       delay 0.20 s and up  -> always one 64-byte report, never the rest

   So nothing may sleep between sending a command and reading it.  ``exchange()``
   and ``read_payload()`` are the supported way to do that: they write and read
   in one uninterrupted operation.

   Do NOT "recover" by clearing endpoint halts while a reply is in flight.
   CLEAR_FEATURE(HALT) during a read discards the queued reports of the reply
   being read, which is what previously made every capture look truncated to a
   single 64-byte report and led to the wrong conclusion that the firmware could
   not download waveforms at all.  ``recover()`` therefore runs only on a
   *write* failure, never spontaneously during reads.

HdsHidTransport implements the subset of the pyserial Serial API that
OWONScopeController uses (write, read, readline, flush, close and a settable
``timeout``), so it can stand in for a Serial object unchanged.
"""

from __future__ import annotations

import struct
import threading
import time
from typing import List, Optional

REPORT_SIZE = 64
OWON_VID = 0x5345
HDS_PID = 0x1234
INTERFACE_HID = 0

#: How long the firmware waits for the host to start reading a multi-report
#: reply before dropping it.  Measured margin on an HDS271 is ~0.10 s; see
#: rule 3 in the module docstring.
READ_DEADLINE = 0.10

#: Refuse to trust a declared payload length beyond this (sanity bound).
MAX_PAYLOAD = 64 * 1024 * 1024

#: How many times reopen() re-finds an instrument that dropped off the bus.
REOPEN_ATTEMPTS = 4


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
                text = usb_util.get_string(dev, getattr(dev, attr))
            except Exception:
                continue
            # Some descriptors arrive NUL-padded ("MSC+HID\x00\x00\x00"), which reads
            # as a different string to anything comparing it - including the serial
            # match that decides which scope to open.
            info[key] = (text or "").replace("\x00", "").strip() or None
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
        # clear_halt repairs a stalled OUT pipe; it is never part of reading.
        self.recover_on_write_failure = True

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

    def reopen(self, attempts: int = REOPEN_ATTEMPTS, delay: float = 0.5) -> bool:
        """Re-find the instrument after it drops off and re-enumerates.

        Driving the OUT endpoint with commands the firmware does not implement
        can reset it off the bus; it comes back a second or two later, so a
        bounded retry loop is the right response rather than declaring failure.
        """
        for _ in range(max(1, attempts)):
            try:
                self.close()
            except Exception:
                pass
            time.sleep(delay)
            try:
                self.open()
                return True
            except Exception as exc:
                self.last_error = "reopen: %s" % exc
        return False

    # ------------------------------------------------------ pyserial surface
    def write(self, data) -> int:
        if not self.is_open:
            raise HdsUsbError("transport is not open")
        payload = bytes(data)
        if not payload:
            return 0
        with self._lock:
            for attempt in range(2):
                try:
                    self._write_reports(payload)
                    return len(payload)
                except Exception as exc:
                    self.last_error = "%s: %s" % (type(exc).__name__, exc)
                    if attempt == 0 and self.recover_on_write_failure:
                        self.recover()  # a stalled OUT pipe is where clear_halt helps
                        time.sleep(0.05)
                        continue
                    raise HdsUsbError("USB write failed: %s" % exc)
        return len(payload)

    def _write_reports(self, payload):
        for offset in range(0, len(payload), self.report_size):
            chunk = payload[offset:offset + self.report_size]
            if len(chunk) < self.report_size:
                chunk = chunk + b"\x00" * (self.report_size - len(chunk))
            self._ep_out.write(chunk, timeout=self._write_timeout_ms())

    def flush(self):
        return None

    def reset_input_buffer(self):
        """Drop anything already in the buffer.

        A waveform capture has to begin reading immediately: the firmware pushes
        the reply into the interrupt endpoint and abandons it if the host is slow
        to collect it, so a stale buffered line would be mistaken for the new
        answer.  Clearing here is also what keeps the pre-read sleep out of the
        capture path.
        """
        with self._lock:
            self._buffer.clear()
        return None

    def read(self, size=1) -> bytes:
        """Binary-safe read of up to ``size`` bytes (fewer on timeout)."""
        if not size or size < 0:
            size = 1
        with self._lock:
            return self._read_exact_locked(size, time.time() + self._seconds())

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
                if not self._pump_until(deadline):
                    break
            data = bytes(self._buffer).strip(b"\x00")
            self._buffer.clear()
            return data

    def read_payload(self, timeout: Optional[float] = None) -> bytes:
        """One ``:DATA:`` reply: a 4-byte little-endian length then the payload.

        The whole transfer is read under a single deadline with no pause, so the
        firmware never sees the host stop polling part-way through a reply.
        Returns b"" when nothing arrived.
        """
        window = self._seconds() if timeout is None else max(0.0, float(timeout))
        with self._lock:
            deadline = time.time() + window
            head = self._read_exact_locked(4, deadline)
            if len(head) < 4:
                return b""
            size = struct.unpack("<I", head)[0]
            if size <= 0:
                return b""
            if size > MAX_PAYLOAD:
                self.last_error = "declared payload %d exceeds sanity bound" % size
                return b""
            return self._read_exact_locked(size, deadline)

    def exchange(self, command: str, payload_timeout: Optional[float] = None,
                 wait: float = 0.0) -> bytes:
        """Send one command and read its reply with no pause in between.

        ``wait`` exists only for callers that genuinely need a delay; leaving it
        at 0 is what keeps multi-report replies intact (rule 3).
        """
        if not command.endswith("\n"):
            command += "\n"
        self.write(command.encode("ascii", "ignore"))
        if wait > 0:
            time.sleep(wait)
        return self.read_payload(payload_timeout)

    def exchange_text(self, command: str, wait: float = 0.0) -> str:
        """Send a scalar query and return the textual reply."""
        if not command.endswith("\n"):
            command += "\n"
        self.clear_input()
        self.write(command.encode("ascii", "ignore"))
        if wait > 0:
            time.sleep(wait)
        return self.readline().decode("utf-8", "replace").strip()

    def read_reports(self, max_reports=16, gap_ms=500) -> bytes:
        """Everything currently offered, stopping after one silent gap."""
        deadline = time.time() + (max(1, max_reports) * gap_ms / 1000.0)
        with self._lock:
            for _ in range(max_reports):
                if not self._pump_until(deadline, gap_ms):
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
    def _clear_halt(self, endpoint_address) -> bool:
        """CLEAR_FEATURE(HALT) on one endpoint.

        Only for repairing a stalled OUT pipe.  Issuing this while a reply is
        being read throws that reply away, so it is never done automatically
        from the read path.
        """
        if self._device is None:
            return False
        try:
            self._device.clear_halt(endpoint_address)
            return True
        except Exception as exc:
            self.last_error = "clear_halt 0x%02x: %s" % (endpoint_address, exc)
            return False

    def recover(self) -> bool:
        """Clear the halts on both HID endpoints.  Safe to call at any time."""
        if self._device is None:
            return False
        addresses = [endpoint.bEndpointAddress for endpoint in (self._ep_in, self._ep_out) if endpoint is not None]
        if not addresses:
            return False
        self._buffer.clear()
        return all([self._clear_halt(address) for address in addresses])

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

    def _read_exact_locked(self, size: int, deadline: float) -> bytes:
        """Take ``size`` bytes from the buffer, pumping reports until the deadline."""
        while len(self._buffer) < size and time.time() < deadline:
            self._pump_until(deadline)
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def _pump_until(self, deadline: float, slice_ms: Optional[int] = None) -> bool:
        """Pump one report while there is time left on the deadline."""
        remaining = deadline - time.time()
        if remaining <= 0:
            return False
        if slice_ms is None:
            slice_ms = max(1, int(min(remaining, self._seconds()) * 1000))
        return self._pump(slice_ms)

    def _pump(self, timeout_ms: Optional[int] = None) -> bool:
        """Read one report into the buffer.  False when nothing arrived.

        No halt clearing happens here: a timeout in the middle of a multi-report
        reply is normal end-of-stream padding, not a fault.
        """
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
