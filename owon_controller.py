import json
import logging
import math
import socket
import struct
import threading
import time
from typing import Optional

import serial
from serial.tools import list_ports

from waveform_data import (WaveformData, HDS_HORIZONTAL_DIVISIONS, HDS_VERTICAL_DIVISIONS,
                           HDS_CODES_PER_DIVISION)


class OWONScopeController:
    """Controller for OWON SDS / HDS oscilloscopes."""

    VOLTAGE_SCALES = ["2mv", "5mv", "10mv", "20mv", "50mv", "100mv",
                      "200mv", "500mv", "1v", "2v", "5v", "10v"]
    TIMEBASE_SCALES = ["5ns", "10ns", "20ns", "50ns", "100ns", "200ns", "500ns",
                       "1us", "2us", "5us", "10us", "20us", "50us", "100us",
                       "200us", "500us", "1ms", "2ms", "5ms", "10ms", "20ms",
                       "50ms", "100ms", "200ms", "500ms", "1s", "2s", "5s",
                       "10s", "20s", "50s", "100s"]
    ACQ_TYPES = ["SAMPle", "AVERage", "PEAK"]
    AVG_COUNTS = ["4", "16", "64", "128"]
    MEMORY_DEPTHS = ["1K", "10K", "100K", "1M", "10M"]
    COUPLING_MODES = ["AC", "DC", "GND"]
    PROBE_ATTEN = ["X1", "X10", "X100", "X1000", "X10000"]
    TRIGGER_TYPES = ["SINGle", "ALT", "Logic", "BUS"]
    TRIGGER_MODES = ["AUTO", "NORMal", "SINGle"]
    TRIGGER_SOURCES = ["CH1", "CH2", "EXT", "EXT/5", "ACLine"]
    TRIGGER_SLOPES = ["RISE", "FALL"]
    TRIGGER_COUPLING = ["DC", "AC", "HF", "LF"]

    def __init__(self, family: str = "hds"):
        self.family = (family or "hds").lower()
        self.connection = None
        self.connection_type = None
        self.is_connected = False
        self.waveform_data = WaveformData()
        self.lock = threading.Lock()
        # Filled in by identify_model() once the instrument answers *IDN?.
        self.series = self.family
        self.model = ""
        self.serial_number = ""
        self.firmware = ""
        self._probed_nodes = {}
        # Absolute volts per sample code, measured against the instrument's own
        # readings. The header's volts/div is a label this firmware does not keep
        # in step with the gain, so this is what later captures decode with.
        self._calibrated_volts_per_code = None
        # A write that changes the framing is not in the next capture yet: the
        # instrument needs a moment to re-acquire. Measured on an HDS271 with a
        # known 5.00 Vpp signal, a capture started 50 ms after a volts/div write
        # still returned the PREVIOUS setting's frame (34 codes where the new
        # setting gives 36); from 150 ms it was correct. Captures wait this out.
        self._framing_settle_until = 0.0

    #: Seconds to let a framing write take effect before capturing. See above.
    FRAMING_SETTLE_SECONDS = 0.25

    def mark_framing_settle(self):
        """Note that the instrument's screen is about to change.

        Called by the writes that move it (volts/div, probe, offset, timebase).
        Without this a capture can redraw the old frame right after a change,
        which looks exactly like the control not working.
        """
        self._framing_settle_until = time.monotonic() + self.FRAMING_SETTLE_SECONDS

    def wait_for_framing_settle(self):
        """Sleep out whatever is left of a pending framing change."""
        remaining = self._framing_settle_until - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    @property
    def is_hds(self):
        return self.family.startswith("hds")

    def _cmd(self, hds: str, legacy: Optional[str] = None):
        return hds if self.is_hds or not legacy else legacy

    def _scpi_mode_command(self):
        return ":SDSLSCPIH" if self.connection_type == "usb" else ":SDSLSCPI#"

    def _normalize_command(self, command):
        command = str(command).strip()
        if not command:
            return ""
        if not command.endswith("\n"):
            command += "\n"
        return command

    def _read_exact(self, size):
        data = b""
        while len(data) < size:
            chunk = self.connection.read(size - len(data)) if self.connection_type == "usb" else self.connection.recv(size - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def detect_usb_port(self):
        candidates = []
        for port in list_ports.comports():
            label = f"{port.device} {port.description or ''} {port.manufacturer or ''}".lower()
            if (port.vid, port.pid) == (0x5345, 0x1234):
                return port.device
            if any(token in label for token in ("owon", "hds", "oscilloscope")):
                candidates.append(port.device)
        return candidates[0] if candidates else None

    def connect_lan(self, ip='10.1.1.131', port=3000):
        try:
            self.connection = socket.create_connection((ip, port), timeout=3)
            self.connection.settimeout(5)
            self.connection_type = 'lan'
            self.send_command(self._scpi_mode_command())
            self.is_connected = True
            return True
        except Exception as exc:
            logging.error("LAN Connection error: %s", exc)
            self.disconnect()
            return False

    def connect_usb(self, port='COM3', baudrate=115200):
        try:
            if not port or str(port).strip().lower() in {"auto", "", "none"}:
                port = self.detect_usb_port()
            if not port:
                raise RuntimeError("No OWON USB serial port detected")
            self.connection = serial.Serial(
                port=port,
                baudrate=baudrate,
                timeout=2,
                write_timeout=2,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
            )
            self.connection_type = 'usb'
            self.send_command(self._scpi_mode_command())
            self.is_connected = True
            return True
        except Exception as exc:
            logging.error("USB Connection error: %s", exc)
            self.disconnect()
            return False

    def send_command(self, command):
        if not self.connection or not self.is_connected and self.connection_type is None:
            return False
        command = self._normalize_command(command)
        if not command:
            return True
        try:
            payload = command.encode("ascii", errors="ignore")
            with self.lock:
                if self.connection_type == 'usb':
                    self.connection.write(payload)
                    self.connection.flush()
                else:
                    self.connection.sendall(payload)
            return True
        except Exception as exc:
            logging.error("Send command error: %s", exc)
            return False

    def read_response(self, timeout=2):
        if not self.connection:
            return None
        try:
            with self.lock:
                if self.connection_type == 'usb':
                    self.connection.timeout = timeout
                    response = self.connection.readline()
                else:
                    self.connection.settimeout(timeout)
                    response = self.connection.recv(4096)
            if not response:
                return ""
            return response.decode('utf-8', errors='ignore').strip()
        except socket.timeout:
            return ""
        except Exception as exc:
            logging.error("Read response error: %s", exc)
            return None

    def _read_binary_packet(self, timeout=10):
        if not self.connection:
            return None
        try:
            with self.lock:
                if self.connection_type == 'usb':
                    self.connection.timeout = timeout
                    first = self.connection.read(4)
                else:
                    self.connection.settimeout(timeout)
                    first = self.connection.recv(4)
                if not first:
                    return None
                if first[:1] == b'#' and len(first) >= 2 and first[1:2].isdigit():
                    digits = int(first[1:2].decode('ascii'))
                    size_bytes = self._read_exact(digits)
                    if len(size_bytes) < digits:
                        return None
                    size = int(size_bytes.decode('ascii'))
                    return self._read_exact(size)
                size = struct.unpack('<I', first)[0]
                if size <= 0:
                    return b''
                payload = self._read_exact(size - 4)
                return payload
        except Exception as exc:
            logging.error("Read binary data error: %s", exc)
            return None

    def query(self, command):
        if self.send_command(command):
            time.sleep(0.05)
            return self.read_response()
        return None

    def query_binary(self, command, timeout=10):
        if self.send_command(command):
            time.sleep(0.05)
            return self._read_binary_packet(timeout=timeout)
        return None

    def get_idn(self):
        return self.query("*IDN?")

    def reset(self):
        return self.send_command("*RST")

    def clear_status(self):
        return self.send_command("*CLS")

    def operation_complete(self):
        return self.query("*OPC?")

    # Channel commands
    def set_channel_display(self, channel, state):
        return self.send_command(f":CHANnel{channel}:DISPlay {'ON' if state else 'OFF'}")

    def get_channel_display(self, channel):
        return self.query(f":CHANnel{channel}:DISPlay?")

    def set_channel_coupling(self, channel, coupling):
        return self.send_command(f":CHANnel{channel}:COUPling {coupling}")

    def get_channel_coupling(self, channel):
        return self.query(f":CHANnel{channel}:COUPling?")

    def set_channel_probe(self, channel, attenuation):
        return self.send_command(f":CHANnel{channel}:PROBe {attenuation}")

    def get_channel_probe(self, channel):
        return self.query(f":CHANnel{channel}:PROBe?")

    def set_channel_scale(self, channel, scale):
        return self.send_command(f":CHANnel{channel}:SCALe {scale}")

    def get_channel_scale(self, channel):
        return self.query(f":CHANnel{channel}:SCALe?")

    def set_channel_offset(self, channel, offset):
        return self.send_command(f":CHANnel{channel}:OFFSet {offset}")

    def get_channel_offset(self, channel):
        return self.query(f":CHANnel{channel}:OFFSet?")

    # Timebase commands
    def set_timebase_scale(self, scale):
        result = self.send_command(f"{self._cmd(':HORIzontal:SCALe', ':TIMebase:SCALe')} {scale}")
        self.mark_framing_settle()
        return result

    def get_timebase_scale(self):
        return self.query(f"{self._cmd(':HORIzontal:SCALe?', ':TIMebase:SCALe?')}")

    def set_timebase_offset(self, offset):
        result = self.send_command(f"{self._cmd(':HORIzontal:OFFset', ':TIMebase:HOFFset')} {offset}")
        self.mark_framing_settle()
        return result

    def get_timebase_offset(self):
        return self.query(f"{self._cmd(':HORIzontal:OFFset?', ':TIMebase:HOFFset?')}")

    # Acquisition commands
    def set_acquire_type(self, acq_type):
        return self.send_command(f"{self._cmd(':ACQuire:MODE', ':ACQuire:TYPE')} {acq_type}")

    def get_acquire_type(self):
        return self.query(f"{self._cmd(':ACQuire:MODE?', ':ACQuire:TYPE?')}")

    def set_acquire_average(self, count):
        return self.send_command(f"{self._cmd(':ACQuire:AVERage:NUM', ':ACQuire:AVERage')} {count}")

    def get_acquire_average(self):
        return self.query(f"{self._cmd(':ACQuire:AVERage:NUM?', ':ACQuire:AVERage?')}")

    def set_memory_depth(self, depth):
        return self.send_command(f"{self._cmd(':ACQuire:DEPMem', ':ACQuire:MDEPth')} {depth}")

    def get_memory_depth(self):
        return self.query(f"{self._cmd(':ACQuire:DEPMem?', ':ACQuire:MDEPth?')}")

    # Trigger commands
    def set_trigger_mode(self, mode):
        if self.is_hds:
            return self.send_command(f":TRIGger:SINGle:SWEEp {mode}")
        return self.send_command(f":TRIGger:MODE {mode}")

    def get_trigger_mode(self):
        return self.query(f"{':TRIGger:SINGle:SWEEp?' if self.is_hds else ':TRIGger:MODE?'}")

    def set_trigger_source(self, source):
        if self.is_hds:
            return self.send_command(f":TRIGger:SINGle:EDGE:SOURce {source}")
        return self.send_command(f":TRIGger:SOURce {source}")

    def get_trigger_source(self):
        return self.query(f"{':TRIGger:SINGle:EDGE:SOURce?' if self.is_hds else ':TRIGger:SOURce?'}")

    def set_trigger_slope(self, slope):
        if self.is_hds:
            return self.send_command(f":TRIGger:SINGle:EDGE:SLOPe {slope}")
        return self.send_command(f":TRIGger:SLOPe {slope}")

    def get_trigger_slope(self):
        return self.query(f"{':TRIGger:SINGle:EDGE:SLOPe?' if self.is_hds else ':TRIGger:SLOPe?'}")

    def set_trigger_level(self, level):
        if self.is_hds:
            return self.send_command(f":TRIGger:SINGle:EDGE:LEVel {level}")
        return self.send_command(f":TRIGger:LEVel {level}")

    def get_trigger_level(self):
        return self.query(f"{':TRIGger:SINGle:EDGE:LEVel?' if self.is_hds else ':TRIGger:LEVel?'}")

    def set_trigger_coupling(self, coupling):
        if self.is_hds:
            return self.send_command(f":TRIGger:SINGle:EDGE:COUPling {coupling}")
        return self.send_command(f":TRIGger:COUPling {coupling}")

    def get_trigger_coupling(self):
        return self.query(f"{':TRIGger:SINGle:EDGE:COUPling?' if self.is_hds else ':TRIGger:COUPling?'}")

    def set_edge_trigger_source(self, source):
        return self.set_trigger_source(source)

    def get_edge_trigger_source(self):
        return self.get_trigger_source()

    def set_edge_trigger_slope(self, slope):
        return self.set_trigger_slope(slope)

    def get_edge_trigger_slope(self):
        return self.get_trigger_slope()

    def set_edge_trigger_level(self, level):
        return self.set_trigger_level(level)

    def get_edge_trigger_level(self):
        return self.get_trigger_level()

    def set_edge_trigger_coupling(self, coupling):
        return self.set_trigger_coupling(coupling)

    def get_edge_trigger_coupling(self):
        return self.get_trigger_coupling()

    def get_trigger_status(self):
        return self.query(":TRIGger:STATUS?")

    def set_trigger_holdoff(self, holdoff):
        return self.send_command(f":TRIGger:SINGle:HOLDoff {holdoff}") if self.is_hds else self.send_command(f":TRIGger:HOLDoff {holdoff}")

    def auto_set(self):
        return self.send_command(":AUTOset ON")

    def auto_scale(self, enable=True):
        return self.send_command(f":AUTOscale {'ON' if enable else 'OFF'}")

    def run(self, enabled=True):
        return self.send_command(f":RUNning {'RUN' if enabled else 'STOP'}")

    def single_trigger(self):
        if self.is_hds:
            return self.send_command(":TRIGger:TYPE SINGle")
        return self.send_command(":TRIGger:MODE SINGle")

    # Measurements
    def set_measure_source(self, source):
        return self.send_command(f":MEASUrement:SOURce {source}")

    def measure_frequency(self, channel=1):
        return self.query(f":MEASUrement:CH{channel}:FREQuency?") if self.is_hds else self.query(":MEASure:FREQuency?")

    def measure_period(self, channel=1):
        return self.query(f":MEASUrement:CH{channel}:PERiod?") if self.is_hds else self.query(":MEASure:PERiod?")

    def measure_vpp(self, channel=1):
        return self.query(f":MEASUrement:CH{channel}:PKPK?") if self.is_hds else self.query(":MEASure:PKPK?")

    def measure_vmax(self, channel=1):
        return self.query(f":MEASUrement:CH{channel}:MAX?") if self.is_hds else self.query(":MEASure:MAX?")

    def measure_vmin(self, channel=1):
        return self.query(f":MEASUrement:CH{channel}:MIN?") if self.is_hds else self.query(":MEASure:MIN?")

    def measure_vavg(self, channel=1):
        return self.query(f":MEASUrement:CH{channel}:AVERage?") if self.is_hds else self.query(":MEASure:AVERage?")

    def measure_vrms(self, channel=1):
        return self.query(f":MEASUrement:CH{channel}:CYCRms?") if self.is_hds else self.query(":MEASure:CYCRms?")

    def measure_all(self, channel=1):
        if self.is_hds:
            return self.query(f":MEASUrement:CH{channel}?")
        return self.query(f":MEASure:CH{channel}?")

    def get_all_measurements(self, channel=1):
        raw = self.measure_all(channel)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        except Exception:
            return {"value": raw}

    # Waveform acquisition
    def _read_hds_waveform(self, channel=1):
        header_raw = self.query(":DATA:WAVE:SCREen:HEAD?")
        if not header_raw:
            return False
        try:
            header = json.loads(header_raw)
        except Exception:
            header = {}

        payload = self.query_binary(f":DATA:WAVE:SCREen:CH{channel}?", timeout=15)
        if not payload:
            return False

        self.waveform_data.parse_hds_capture(header, {f"CH{channel}": payload})
        return True

    def download_waveform_data(self):
        if not self.is_connected:
            logging.error("Not connected to oscilloscope")
            return False

        try:
            self.clear_status()
            time.sleep(0.05)
            if self.is_hds:
                channel_payloads = {}
                header_raw = self.query(":DATA:WAVE:SCREen:HEAD?")
                if not header_raw:
                    return False
                try:
                    header = json.loads(header_raw)
                except Exception:
                    header = {}
                channels = [item.get("name", "") for item in header.get("channel", []) if str(item.get("name", "")).lower().startswith("ch")]
                if not channels:
                    channels = ["CH1", "CH2"]
                for channel_name in channels:
                    payload = self.query_binary(f":DATA:WAVE:SCREen:{channel_name.upper()}?", timeout=15)
                    if payload:
                        channel_payloads[channel_name.upper()] = payload
                if not channel_payloads:
                    payload = self.query_binary(":DATA:WAVE:DEPMem:All?", timeout=15)
                    if payload:
                        channel_payloads["CH1"] = payload
                if not channel_payloads:
                    return False
                self.waveform_data.parse_hds_capture(header, channel_payloads)
                return bool(self.waveform_data.channels)

            # Legacy OWON SDS-compatible capture path
            self.send_command("STARTMEMDEPTH")
            time.sleep(0.1)
            header_data = self._read_binary_packet(timeout=5)
            if not header_data or len(header_data) < 12:
                logging.error("Failed to read response header")
                return False

            file_length = struct.unpack('<I', header_data[0:4])[0]
            if file_length <= 0:
                return False
            waveform_data = self._read_binary_packet(timeout=15)
            if waveform_data and len(waveform_data) >= file_length:
                self.waveform_data.parse_bin_data(waveform_data, file_length)
                return True
        except Exception as exc:
            logging.error("Error downloading waveform data: %s", exc)
        return False

    def disconnect(self):
        self.is_connected = False
        if self.connection:
            try:
                self.connection.close()
            except Exception:
                pass
        self.connection = None
        self.connection_type = None

    # ---- HDS USB (raw HID) support ----------------------------------------
    # Verified on an HDS271, firmware V1.3.0: the scope carries ASCII SCPI in
    # 64-byte HID reports on interface 0 (interrupt OUT 0x01 / IN 0x81).  There
    # is no serial interface, so the transport is pyusb rather than pyserial.

    HDS_SERIES = ("hds200", "hds300")

    def find_hds_device(self):
        """Info for the first OWON HDS device on USB, or None."""
        try:
            from hds_usb import list_owon_devices
            devices = list_owon_devices()
        except Exception as exc:
            logging.error("USB scan error: %s", exc)
            return None
        return devices[0] if devices else None

    def connect_usb_hid(self, serial=None, timeout=2.0):
        """Connect over the raw USB HID interface.  Works with any driver."""
        try:
            from hds_usb import HdsHidTransport
            transport = HdsHidTransport(serial=serial, timeout=timeout)
            transport.open()
            self.connection = transport
            # the transport mirrors the pyserial surface, so send_command,
            # read_response, _read_exact and _read_binary_packet work unchanged
            self.connection_type = "usb"
            self.is_connected = True
            return True
        except Exception as exc:
            logging.error("USB HID connection error: %s", exc)
            self.disconnect()
            return False

    def connect_hds(self, serial=None):
        """USB HID first, then the serial and LAN paths as fallback."""
        if self.connect_usb_hid(serial=serial):
            self.identify_model()
            return True
        port = self.detect_usb_port()
        if port:
            return self.connect_usb(port)
        return False

    def identify_model(self):
        """Read *IDN? and derive the HDS series (200 or 300)."""
        raw = (self.get_idn() or "").strip()
        parts = [part.strip() for part in raw.split(",")]
        model = parts[1] if len(parts) > 1 else ""
        if model.upper().startswith("HDS3"):
            series = "hds300"
        elif model.upper().startswith("HDS2"):
            series = "hds200"
        else:
            series = self.family
        self.model = model
        self.series = series
        self.serial_number = parts[2] if len(parts) > 2 else ""
        self.firmware = parts[3] if len(parts) > 3 else ""
        if series in self.HDS_SERIES:
            self.family = series
        return {
            "idn": raw,
            "model": self.model,
            "series": self.series,
            "serial": self.serial_number,
            "firmware": self.firmware,
        }

    # ================= HDS overrides ==========================================
    # Everything below shadows the SDS-oriented definitions above.  Verified
    # against an HDS271, firmware V1.3.0:
    #   * channels are addressed as ":CH1:SCALe?" -- never ":CHANnel1:SCALe?"
    #   * measurements answer per item only (":MEASUrement:CH1:FREQuency?"),
    #     ":MEASUrement:CH1?" itself is silent
    #   * replies come back in engineering notation ("5e-04", "5e+00") and with
    #     loose enum spelling ("AUTo"), which the UI choice lists do not match,
    #   so replies are mapped back onto the lists the front panel uses.

    connect_usb_serial = connect_usb
    measure_all_sds = measure_all
    get_all_measurements_sds = get_all_measurements

    MEMORY_DEPTHS = ["4K", "1K", "10K", "100K", "1M", "10M"]  # HDS reports 4K

    HDS_MEASUREMENT_ITEMS = (
        ("Frequency", "FREQuency"),
        ("Period", "PERiod"),
        ("Vpp", "PKPK"),
        ("Vmax", "MAX"),
        ("Vmin", "MIN"),
        ("Vavg", "AVERage"),
    )

    SI_FACTORS = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6}

    @classmethod
    def parse_scale(cls, text):
        """'500us', '5v', '1ms', '5e-04' -> float in base units."""
        import re
        if text is None:
            return None
        match = re.match(r"^\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*([A-Za-z]*)\s*$", str(text))
        if not match:
            return None
        try:
            number = float(match.group(1))
        except ValueError:
            return None
        unit = match.group(2)
        if len(unit) > 1 and unit[0] in cls.SI_FACTORS:
            number *= cls.SI_FACTORS[unit[0]]
        return number

    @classmethod
    def match_scale(cls, reply, choices):
        """'5e-04' -> '500us': the choice list entry the scope's reply equals."""
        wanted = cls.parse_scale(reply)
        if wanted is None:
            return None
        for choice in choices:
            value = cls.parse_scale(choice)
            if value is None:
                continue
            if abs(value - wanted) <= max(abs(wanted), 1e-12) * 1e-6:
                return choice
        return None

    @staticmethod
    def match_choice(reply, choices):
        """'AUTo' -> 'AUTO': case-insensitive match against the choice list."""
        if reply in (None, ""):
            return None
        token = str(reply).strip().lower()
        for choice in choices:
            if str(choice).strip().lower() == token:
                return choice
        return None

    def _channel_node(self, channel, node):
        """HDS uses :CH1:..., the SDS dialect uses :CHANnel1:..."""
        return f":CH{channel}:{node}" if self.is_hds else f":CHANnel{channel}:{node}"

    # -- HDS dialect overrides ---------------------------------------------
    # Everything above this point is the original implementation. The methods
    # from here on reuse those names and, living in the same class, they replace
    # them outright - so a fallback written as
    # ``OWONScopeController.query(self, ...)`` resolves back to the override and
    # recurses until the stack dies. The originals are kept under aliases for
    # the non-HDS path to call instead.
    legacy_send_command = send_command
    legacy_query = query
    legacy_query_binary = query_binary
    legacy_read_binary_packet = _read_binary_packet
    legacy_download_waveform_data = download_waveform_data
    legacy_set_trigger_slope = set_trigger_slope
    legacy_get_trigger_slope = get_trigger_slope
    legacy_set_trigger_coupling = set_trigger_coupling
    legacy_get_trigger_coupling = get_trigger_coupling

    # -- connection ---------------------------------------------------------
    def connect_usb(self, port='auto', baudrate=115200):
        """Auto USB goes to the raw HID link first: HDS has no COM port."""
        target = str(port or "").strip()
        automatic = target.lower() in {"", "auto", "none", "usb"}
        if self.is_hds and automatic:
            if self.connect_usb_hid():
                self.identify_model()
                return True
            logging.warning("HDS USB HID link unavailable, falling back to serial")
        return self.connect_usb_serial(target or "auto", baudrate)

    # -- channel commands ---------------------------------------------------
    def set_channel_display(self, channel, state):
        return self.send_command(f"{self._channel_node(channel, 'DISPlay')} {'ON' if state else 'OFF'}")

    def get_channel_display(self, channel):
        # the HDS271 does not answer this; None leaves the widget untouched
        return self.query(self._channel_node(channel, "DISPlay?"))

    def set_channel_coupling(self, channel, coupling):
        return self.send_command(f"{self._channel_node(channel, 'COUPling')} {coupling}")

    def get_channel_coupling(self, channel):
        reply = self.query(self._channel_node(channel, "COUPling?"))
        return self.match_choice(reply, self.COUPLING_MODES) or reply

    def set_channel_probe(self, channel, attenuation):
        result = self.send_command(f"{self._channel_node(channel, 'PROBe')} {attenuation}")
        self.mark_framing_settle()
        return result

    def get_channel_probe(self, channel):
        return self.query(self._channel_node(channel, "PROBe?"))

    def set_channel_scale(self, channel, scale):
        result = self.send_command(f"{self._channel_node(channel, 'SCALe')} {scale}")
        self.mark_framing_settle()
        return result

    def get_channel_scale(self, channel):
        reply = self.query(self._channel_node(channel, "SCALe?"))
        return self.match_scale(reply, self.VOLTAGE_SCALES) or reply

    def set_channel_offset(self, channel, offset):
        result = self.send_command(f"{self._channel_node(channel, 'OFFSet')} {offset}")
        self.mark_framing_settle()
        return result

    def get_channel_offset(self, channel):
        return self.query(self._channel_node(channel, "OFFSet?"))

    # -- timebase / acquisition --------------------------------------------
    def get_timebase_scale(self):
        reply = self.query(f"{self._cmd(':HORIzontal:SCALe?', ':TIMebase:SCALe?')}")
        return self.match_scale(reply, self.TIMEBASE_SCALES) or reply

    def invalidate_capture_header(self):
        """Drop the cached capture header, so the next capture re-reads the framing.

        The header carries the volts/div, probe and timebase the instrument is
        using. Any change of those - a write here, or a menu setting on the
        instrument - must not be decoded against the old copy.
        """
        self._cached_header = None

    #: The framing as three cheap reads, watched on a schedule.
    #:
    #: The capture header carries copies of these values but costs 479 bytes
    #: (~254 ms) against ~32 ms each, so the live loop watches these and only
    #: re-reads the header when one of them moves. Without that watch, a
    #: volts/div set from the instrument's own menu reaches the plot only on the
    #: header schedule - which is ~8 s at the live rate, and reads exactly like
    #: "the volts/div control is not working" while the time base, whose label the
    #: plot follows every frame, looks fine.
    #:
    #: They are read through the panel's own accessors, which apply the family's
    #: dialect and match the reply to the ladder: the raw ':CHANnel1:SCALe?' nodes
    #: answer nothing at all on an HDS instrument and would have made every poll
    #: look like a change.
    FRAMING_NAMES = ("scale", "probe", "timebase")

    def framing_signature(self):
        """What the instrument says its framing is right now, as a tuple.

        Returns None for any value that could not be read, so a dropped reply is
        reported as a difference and triggers a header re-read rather than being
        mistaken for "nothing changed".
        """
        readers = ((self.get_channel_scale, 1), (self.get_channel_probe, 1),
                   (self.get_timebase_scale, None))
        values = []
        for name, (reader, argument) in zip(self.FRAMING_NAMES, readers):
            try:
                reply = reader(argument) if argument is not None else reader()
                values.append(str(reply or "").strip())
            except Exception as exc:                              # noqa: BLE001
                logging.warning("Framing poll failed on %s: %s", name, exc)
                values.append(None)
        return tuple(values)

    def get_trigger_mode(self):
        reply = self.query(":TRIGger:SINGle:SWEEp?" if self.is_hds else ":TRIGger:MODE?")
        return self.match_choice(reply, self.TRIGGER_MODES) or reply

    def get_trigger_source(self):
        reply = self.query(":TRIGger:SINGle:EDGE:SOURce?" if self.is_hds else ":TRIGger:SOURce?")
        return self.match_choice(reply, self.TRIGGER_SOURCES) or reply

    def get_acquire_type(self):
        reply = self.query(f"{self._cmd(':ACQuire:MODE?', ':ACQuire:TYPE?')}")
        return self.match_choice(reply, self.ACQ_TYPES) or reply

    def get_memory_depth(self):
        reply = self.query(f"{self._cmd(':ACQuire:DEPMem?', ':ACQuire:MDEPth?')}")
        return self.match_choice(reply, self.MEMORY_DEPTHS) or reply

    # -- measurements -------------------------------------------------------
    def get_all_measurements(self, channel=1):
        if not self.is_hds:
            return self.get_all_measurements_sds(channel)
        results = {}
        for label, node in self.HDS_MEASUREMENT_ITEMS:
            value = self.query(f":MEASUrement:CH{channel}:{node}?")
            if value not in (None, ""):
                results[label] = value
        return results

    def measure_all(self, channel=1):
        if not self.is_hds:
            return self.measure_all_sds(channel)
        payload = self.get_all_measurements(channel)
        return json.dumps(payload, ensure_ascii=False) if payload else None

    # ================= HDS transport and command dialect ======================
    # The node forms below were established by write-then-read-back against a
    # live instrument, NOT taken from the manual.  Two manual nodes do not exist
    # on HDS271 firmware V1.3.0 and are actively harmful because the scope
    # answers nothing and the setting silently does not change:
    #
    #   manual says :TRIGger:SINGle:EDGE:SLOPe     real node :TRIGger:SINGle:EDGe
    #   manual says :TRIGger:SINGle:EDGE:COUPling  real node :TRIGger:SINGle:COUPling
    #
    # Everything else in the manual held up.  Each entry is a tuple of
    # candidates in preference order; :meth:`probe_dialect` can confirm the
    # first one that actually answers on an unknown model.

    HDS_DIALECT_HDS200 = {
        "source": (":TRIGger:SINGle:EDGE:SOURce", ":TRIGger:SINGle:SOURce"),
        "slope": (":TRIGger:SINGle:EDGe", ":TRIGger:SINGle:SLOPe", ":TRIGger:SINGle:EDGE:SLOPe"),
        "coupling": (":TRIGger:SINGle:COUPling", ":TRIGger:SINGle:EDGE:COUPling"),
        "level": (":TRIGger:SINGle:EDGE:LEVel", ":TRIGger:SINGle:EDGe:LEVel", ":TRIGger:SINGle:LEVel"),
        "sweep": (":TRIGger:SINGle:SWEEp", ":TRIGger:SINGle:MODE"),
        "holdoff": (":TRIGger:SINGle:HOLDoff",),
    }

    # No HDS300-series instrument was available to test against, and OWON
    # publishes no SCPI manual for it.  The HDS300 shares the HDS200 command
    # core in OWON's own PC software, so the verified HDS200 nodes are the
    # default and the deeper forms are kept as fallbacks.  Treat any HDS300
    # result as unverified until probe_dialect() has run on real hardware.
    HDS_DIALECT_HDS300 = dict(HDS_DIALECT_HDS200)
    HDS_DIALECT_VERIFIED = {"hds200": "HDS271 / V1.3.0", "hds300": None}

    HDS_MEASUREMENT_ITEMS = (
        ("Frequency", "FREQuency"),
        ("Period", "PERiod"),
        ("Vpp", "PKPK"),
        ("Vmax", "MAX"),
        ("Vmin", "MIN"),
        ("Vmean", "AVERage"),
        ("Vrms", "RMS"),
        ("Vamp", "VAMP"),
        ("Pwidth", "PWIDth"),
        ("Nwidth", "NWIDth"),
    )

    @property
    def dialect(self):
        """Trigger node table for the connected family."""
        if self.series == "hds300":
            return self.HDS_DIALECT_HDS300
        return self.HDS_DIALECT_HDS200

    def trigger_node(self, name):
        """Preferred node for a trigger setting, honouring a probed override."""
        probed = getattr(self, "_probed_nodes", {}).get(name)
        if probed:
            return probed
        return self.dialect[name][0]

    def probe_dialect(self, names=None):
        """Ask the instrument which candidate node each setting really uses.

        Sends one query per candidate and keeps the first that answers.  This is
        the supported way to bring up an untested HDS200/HDS300 model; run it
        deliberately rather than on every connect, because a burst of commands
        the firmware does not implement can reset it off the USB bus.
        """
        if not self.is_hds:
            return {}
        probed = dict(getattr(self, "_probed_nodes", {}))
        for name in (names or list(self.dialect)):
            for node in self.dialect[name]:
                try:
                    reply = self.query(node + "?")
                except Exception:
                    reply = None
                if reply:
                    probed[name] = node
                    break
        self._probed_nodes = probed
        return probed

    # -- transport ---------------------------------------------------------
    def _hds_transport(self):
        """The HID transport when talking to an HDS over raw USB, else None."""
        connection = self.connection
        if connection is not None and hasattr(connection, "exchange_text"):
            return connection
        return None

    def query(self, command):
        """One command, one reply.

        Over the HDS HID link this must not wait before reading: the scope
        pushes the whole reply into the interrupt endpoint and abandons it if
        the host is slow to collect it, so a sleep between write and read turns
        a full answer into a truncated one.
        """
        transport = self._hds_transport()
        if transport is not None:
            try:
                with self.lock:
                    return transport.exchange_text(command)
            except Exception as exc:
                logging.error("HDS query error for %r: %s", command, exc)
                return None
        return self.query_serial(command)

    def query_serial(self, command):
        return self.legacy_query(command)

    def query_binary(self, command, timeout=10):
        """Binary capture: the payload is length-prefixed by the transport."""
        transport = self._hds_transport()
        if transport is not None:
            try:
                with self.lock:
                    return transport.exchange(command, payload_timeout=timeout)
            except Exception as exc:
                logging.error("HDS binary query error for %r: %s", command, exc)
                return None
        return self.legacy_query_binary(command, timeout=timeout)

    # -- trigger settings ---------------------------------------------------
    def set_trigger_slope(self, slope):
        if not self.is_hds:
            return self.legacy_set_trigger_slope(slope)
        # The scope echoes "RISe"; spell it the way the front panel accepts it.
        token = {"RISE": "RISe", "RISING": "RISe", "FALL": "FALL", "FALLING": "FALL"}.get(
            str(slope).strip().upper(), slope)
        return self.send_command(f"{self.trigger_node('slope')} {token}")

    def get_trigger_slope(self):
        if not self.is_hds:
            return self.legacy_get_trigger_slope()
        reply = self.query(self.trigger_node("slope") + "?")
        return self.match_choice(reply, self.TRIGGER_SLOPES) or reply

    def set_trigger_coupling(self, coupling):
        if not self.is_hds:
            return self.legacy_set_trigger_coupling(coupling)
        return self.send_command(f"{self.trigger_node('coupling')} {coupling}")

    def get_trigger_coupling(self):
        if not self.is_hds:
            return self.legacy_get_trigger_coupling()
        reply = self.query(self.trigger_node("coupling") + "?")
        return self.match_choice(reply, self.TRIGGER_COUPLING) or reply

    def get_trigger_sweep(self):
        """AUTO, NORMal or SINGle: 32 ms, cheaper than reading a whole header."""
        reply = self.query(":TRIGger:SINGle:SWEEp?")
        return self.match_choice(reply, self.TRIGGER_MODES) or reply

    def get_trigger_status(self):
        """What the trigger is doing - RUN, TRIG, STOP or WAIT - or None.

        Documented as :TRIGger:STATus? and answered on the HDS271, so it can be
        asked without capturing a frame; the same word also rides in the capture
        header as RUNSTATUS.
        """
        reply = self.query(":TRIGger:STATus?")
        return str(reply).strip() if reply else None

    # -- acquisition --------------------------------------------------------
    def download_waveform_data(self, calibrate=False, reuse_header=False):
        """Capture the on-screen waveform into ``self.waveform_data``.

        ``reuse_header=True`` re-parses against the last header instead of
        asking for it again. The header is 479 bytes and the payload 604, over
        an endpoint that hands over 64 bytes every 32 ms, so the header is a
        measured 254 ms of every frame - 31% - and it only changes when a
        setting does. Any write invalidates it, and the live loop re-reads it
        on a schedule, so a stale one cannot outlive a front-panel change by
        more than that schedule.

        ``calibrate=True`` also asks for the instrument's own MIN/MAX/PKPK/Average
        and uses them to anchor the volts axis (see
        :meth:`WaveformData.calibrate_from_measurements`); that costs a few extra
        round trips but makes the plot agree with the readout.
        """
        if not self.is_hds:
            return self.legacy_download_waveform_data()
        if not self.is_connected:
            logging.error("Not connected to oscilloscope")
            return False
        try:
            # A framing write is not in the next capture yet - at 50 ms the
            # instrument still returns the previous setting's frame - so a
            # capture asked for right after one would redraw what was on the
            # screen before the change, and the change would look ignored.
            self.wait_for_framing_settle()
            transport = self._hds_transport()
            if transport is not None:
                # A capture begins by reading straight away, so clear this
                # transport's buffer rather than issuing a command.
                transport.reset_input_buffer()
            header = self._cached_header if reuse_header else None
            if header is None:
                header = self._capture_header() or self.retry_capture_header()
                if header is None:
                    return False
                self._cached_header = header

            channel_payloads = {}
            for name in self._capture_channels(header):
                payload = self.query_binary(":DATA:WAVE:SCREen:%s?" % name, timeout=5)
                if payload:
                    channel_payloads[name] = payload
            if not channel_payloads:
                logging.error("Capture returned no channel payloads")
                return False

            self.waveform_data = WaveformData()
            self.waveform_data.parse_hds_capture(
                header, channel_payloads,
                calibrated_volts_per_code=self._calibrated_volts_per_code)
            if not self.waveform_data.channels:
                return False
            if calibrate:
                self.calibrate_capture()
            return True
        except Exception as exc:
            logging.error("Error downloading waveform data: %s", exc)
            return False

    def retry_capture_header(self):
        """One more try at the capture header.

        The firmware abandons a payload that is not polled within about 100 ms,
        so a missed header is expected now and then - and two programs polling
        the one HID endpoint make it more likely. Without a retry a transient
        miss pauses the live refresh and reports a failure that is not real.
        """
        logging.warning("Capture header missing; retrying once")
        return self._capture_header()

    def _capture_header(self):
        """The capture header as a dict, with trustworthy channel scales.

        Two instrument quirks are handled here:

        * the header arrives as a length-prefixed binary payload, not as a text
          line, so it must be read with the binary path or the JSON parse fails
          and every field silently comes back empty;
        * the header's per-channel SCALE goes stale: after the scale is written
          externally it reports a value a factor of the probe away from what
          ``:CHn:SCALe?`` says.  The queried value is the one that reproduces the
          instrument's own measurements, so it overwrites the header's copy.
        """
        try:
            raw = self.query_binary(":DATA:WAVE:SCREen:HEAD?", timeout=5)
        except Exception as exc:
            logging.error("Capture header error: %s", exc)
            return None
        if not raw:
            logging.error("No capture header returned")
            return None
        try:
            header = json.loads(raw.decode("utf-8", errors="ignore"))
        except Exception as exc:
            logging.error("Capture header was not JSON: %s", exc)
            return None
        self._override_header_scales(header)
        return header

    def _override_header_scales(self, header):
        """Replace stale header scale/probe fields with freshly queried ones."""
        entries = header.get("CHANNEL") or header.get("channel") or []
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            return
        for item in entries:
            if not isinstance(item, dict):
                continue
            name = str(item.get("NAME") or item.get("name") or "").upper()
            if not name.startswith("CH"):
                continue
            number = name[2:]
            scale = self.query(":CH%s:SCALe?" % number)
            probe = self.query(":CH%s:PROBe?" % number)
            if scale:
                item["SCALE"] = scale
                item.pop("scale", None)
            if probe:
                item["PROBE"] = probe
                item.pop("probe", None)

    def _capture_channels(self, header):
        """Channel names to pull, from the header when it names them.

        The header JSON uses UPPERCASE keys on this firmware, so the lookup has
        to be case-insensitive or the enabled channels are missed.
        """
        declared = header.get("CHANNEL") or header.get("channel") or []
        names = []
        if isinstance(declared, list):
            for item in declared:
                if isinstance(item, dict):
                    name = str(item.get("NAME") or item.get("name") or "").strip()
                    display = item.get("DISPLAY") if "DISPLAY" in item else item.get("display")
                    if name.lower().startswith("ch") and display not in (0, "0", False):
                        names.append(name.upper())
        return names or ["CH1"]

    #: Measurement nodes whose replies are in picoseconds, not seconds, held in
    #: upper case because they are matched against an upper-cased node name.
    #: A 1 kHz square wave answers PERiod? with "1.0000e+09", i.e. 1 ms in ps;
    #: the same convention applies to the width measurements.
    HDS_PICOSECOND_ITEMS = ("PERIOD", "PWIDTH", "NWIDTH", "RISETIME", "FALLTIME")

    @classmethod
    def parse_measurement(cls, text, node=None):
        """' >4.4600e+00' -> 4.46, and '1.0000e+09' on a time node -> 1e-3 s.

        The instrument prefixes a value with '>' or '<' when it falls outside
        the range it guarantees.  The number is still the best answer it has, so
        the qualifier is stripped rather than the reading thrown away.  A
        non-numeric reply returns None; callers must not assume a number.
        """
        if text is None:
            return None
        token = str(text).strip().lstrip("><= ")
        try:
            value = float(token)
        except ValueError:
            return None
        if node and str(node).upper() in cls.HDS_PICOSECOND_ITEMS:
            value /= 1e12
        return value

    def get_measurements_numeric(self, channel=1):
        """Measurements as floats in volts / hertz / seconds.

        Use this rather than :meth:`get_all_measurements` when the values are to
        be computed with: the display-oriented method returns the instrument's
        raw strings, which carry qualifiers and picosecond time units.
        """
        if not self.is_hds:
            return {}
        return {label: value for label, value in
                ((label, self.parse_measurement(self.query(":MEASUrement:CH%s:%s?" % (channel, node)), node))
                 for label, node in self.HDS_MEASUREMENT_ITEMS)
                if value is not None}

    @staticmethod
    def is_qualified(text):
        """True when the instrument flagged a reading as outside its range.

        Such a number is a bound, not a value: '>4.46' means 'at least 4.46'.
        Callers comparing it against a decoded waveform must treat the result as
        inconclusive rather than as a mismatch.
        """
        return str(text).strip()[:1] in (">", "<") if text is not None else False

    def verify_scale(self, channel=1, tolerance=0.05):
        """Check the decoded volts axis against the instrument's own reading.

        The vertical scale reported by ``:CHn:SCALe?`` can disagree with the
        scale the acquisition is actually using: on HDS271 firmware V1.3.0 a
        scale WRITE is accepted and echoed without changing the captured gain,
        which would silently mis-scale the decoded volts.  Comparing the decoded
        RMS against the instrument's own Vrms catches that, and the reading's
        range qualifier decides whether the comparison means anything.

        Returns a dict with the numbers used and a verdict, never a bare bool,
        because "could not tell" is a real and common answer.
        """
        verdict = {"channel": channel, "volts_per_div": None, "decoded_vrms": None,
                   "instrument_vrms": None, "relative_error": None,
                   "conclusive": False, "consistent": False, "reason": ""}
        if not self.is_hds or not self.waveform_data.channels:
            verdict["reason"] = "no capture available; call download_waveform_data() first"
            return verdict

        entry = None
        for candidate in self.waveform_data.channels:
            if str(candidate.get("name", "")).upper() == "CH%s" % channel:
                entry = candidate
                break
        if entry is None:
            verdict["reason"] = "channel CH%s is not in the capture" % channel
            return verdict

        verdict["volts_per_div"] = entry.get("volts_per_div")
        volts = entry.get("waveform_data") or []
        if not volts:
            verdict["reason"] = "capture holds no samples for CH%s" % channel
            return verdict
        decoded = math.sqrt(sum(value * value for value in volts) / len(volts))
        verdict["decoded_vrms"] = decoded

        raw = self.query(":MEASUrement:CH%s:RMS?" % channel)
        instrument = self.parse_measurement(raw, "RMS")
        verdict["instrument_vrms"] = instrument
        if instrument is None:
            verdict["reason"] = "the instrument did not report Vrms"
            return verdict
        if self.is_qualified(raw):
            verdict["reason"] = ("the instrument flagged its Vrms as %s, so it is a bound "
                                 "rather than a value; comparison inconclusive" % str(raw).strip())
            return verdict
        if not decoded:
            verdict["reason"] = "decoded RMS is zero; nothing to compare"
            return verdict

        error = abs(decoded - instrument) / abs(instrument)
        verdict["relative_error"] = error
        verdict["conclusive"] = True
        verdict["consistent"] = error <= tolerance
        verdict["reason"] = "decoded RMS within %.1f%% of the instrument" % (tolerance * 100) \
            if verdict["consistent"] else \
            "decoded RMS differs from the instrument by %.1f%%" % (error * 100)
        return verdict

    def calibrate_capture(self, channel=None, pin_extremes=True):
        """Anchor the capture's volts axis on the instrument's own readings.

        ``pin_extremes=True`` by default here: it is what fixes the slope from the
        instrument's MIN/MAX/PKPK, and the slope is the part that needs fixing.
        The header's volts/div is a label this firmware does not keep in step with
        the gain - a 10x label change was measured leaving the volts per code at
        ~0.145 V - while the instrument's own readings come from the real gain and
        describe the input. Pinning the extremes onto those readings therefore
        recovers the true scale, and the result is remembered so later captures
        decode correctly without paying for the measurement block again.
        """
        measurements = {}
        for entry in self.waveform_data.channels:
            name = str(entry.get("name", "CH1")).upper()
            channel_number = name[2:] if name[:2].upper() == "CH" else name
            measurements.update(self.get_measurements_numeric(channel_number))
        calibrated = self.waveform_data.calibrate_from_measurements(
            measurements, channel=channel, pin_extremes=pin_extremes)
        if calibrated:
            for entry in self.waveform_data.channels:
                slope = entry.get("voltage_per_point")
                if slope:
                    self._calibrated_volts_per_code = slope
                    entry["true_volts_per_div"] = slope * HDS_CODES_PER_DIVISION
                    entry["volts_per_code_source"] = "instrument measurements"
                    break
        return calibrated

    def get_capabilities(self):
        """Which optional capture paths this instrument actually answers.

        Deep-memory and bitmap downloads are silent on some firmware, so probe
        them once instead of assuming: a command that never replies is reported
        as unavailable rather than guessed at.
        """
        capabilities = {"screen": True, "deep_memory": False, "bitmap": False,
                        "measurements": [], "series": self.series, "model": self.model,
                        "channels": self.get_channel_count()}
        if not self.is_hds:
            return capabilities
        try:
            if self.query(":DATA:WAVE:DEPMem:HEAD?"):
                capabilities["deep_memory"] = True
            if self.query(":DATA:WAVE:SCREen:BMP?"):
                capabilities["bitmap"] = True
            supported = []
            for label, node in self.HDS_MEASUREMENT_ITEMS:
                if self.query(":MEASUrement:CH1:%s?" % node):
                    supported.append(label)
            capabilities["measurements"] = supported
        except Exception as exc:
            logging.error("Capability probe error: %s", exc)
        return capabilities

    # -- instrument shape ---------------------------------------------------
    def forget_channel_count(self):
        """Drop the cached channel count.

        The count belongs to one instrument, so a fresh connect must re-probe
        rather than trust an answer gathered from whatever was attached before.
        """
        self._channel_count = None

    def get_channel_count(self):
        """How many channels this instrument really has, probed not assumed.

        A single-channel HDS271 answers nothing at all to ``:CH2:SCALe?``, so an
        empty reply means the channel is absent.  The reply must also parse as a
        number: an unrecognised command can make the instrument hand back a stale
        queued payload (``:CH2:PROBe?`` returns the previous capture header), and
        taking any non-empty answer as proof would invent a second channel.
        """
        if not self.is_hds:
            return 2
        cached = getattr(self, "_channel_count", None)
        if cached:
            return cached
        count = 1
        for number in (2, 3, 4):
            try:
                reply = self.query(":CH%d:SCALe?" % number)
            except Exception:
                reply = None
            if reply and self.parse_scale(reply) is not None:
                count = number
        self._channel_count = count
        return count

    def get_trigger_level_volts(self):
        """Trigger level in volts for the Y-axis cursor, or None."""
        if not self.is_hds:
            return None
        reply = self.query(self.trigger_node("level") + "?")
        if reply in (None, ""):
            return None
        return self.parse_scale(reply)

    def get_horizontal_position_seconds(self):
        """Horizontal position as reported by the instrument, in seconds."""
        reply = self.query(self._cmd(":HORIzontal:OFFset?", ":TIMebase:HOFFset?"))
        if reply in (None, ""):
            return None
        return self.parse_scale(reply)

    def nearest_timebase(self, seconds_per_div):
        """The available time/div closest to a target, compared by ratio."""
        if not seconds_per_div or seconds_per_div <= 0:
            return None
        best = None
        for choice in self.TIMEBASE_SCALES:
            value = self.parse_scale(choice)
            if not value or value <= 0:
                continue
            if best is None or abs(math.log(value / seconds_per_div)) < abs(math.log(best[0] / seconds_per_div)):
                best = (value, choice)
        return best[1] if best else None

    def nearest_voltage_scale(self, volts_per_div):
        """The available volts/div closest to a target, compared by ratio."""
        if not volts_per_div or volts_per_div <= 0:
            return None
        best = None
        for choice in self.VOLTAGE_SCALES:
            value = self.parse_scale(choice)
            if not value or value <= 0:
                continue
            if best is None or abs(math.log(value / volts_per_div)) < abs(math.log(best[0] / volts_per_div)):
                best = (value, choice)
        return best[1] if best else None

    @staticmethod
    def format_level(volts):
        """Volts as the instrument spells a level: '100mV', '1.00V', '0V'."""
        if volts is None:
            return None
        if abs(volts) < 1.0:
            return "%dmV" % int(round(volts * 1000))
        return "%.2fV" % volts

    def frame_from_trace(self, channel=1):
        """Frequency, Vpp and Vmean measured from the decoded trace itself.

        The instrument's own ``:MEASuurement?`` readings come back range-flagged
        whenever the signal leaves the current scale -- which is exactly when
        AUTO is wanted -- and on the HDS271 they simply answer nothing for
        frequency. The captured samples are already decoded to volts and were
        validated against the instrument's own RMS to 0.74%, so the framing is
        derived from them instead.
        """
        entry = None
        for item in self.waveform_data.channels or []:
            if str(item.get("name") or "").upper().endswith(str(channel)):
                entry = item
                break
        if entry is None and self.waveform_data.channels:
            entry = self.waveform_data.channels[0]
        if not entry:
            return {}

        data = [value for value in entry.get("waveform_data") or [] if value is not None]
        interval = entry.get("point_interval")
        if len(data) < 4 or not interval or interval <= 0:
            return {}

        high = max(data)
        low = min(data)
        result = {"vpp": high - low, "vmax": high, "vmin": low,
                  "vmean": sum(data) / len(data),
                  "span": interval * (len(data) - 1)}

        midpoint = (high + low) / 2.0
        edges = [index for index in range(1, len(data))
                 if data[index - 1] <= midpoint < data[index]]
        if len(edges) > 1:
            # N rising edges span N-1 periods. Using N here doubles the figure
            # whenever only two edges are on screen, which is the framed case.
            result["frequency"] = (len(edges) - 1) / ((edges[-1] - edges[0]) * interval)
            result["cycles"] = len(edges)
        return result

    def auto_frame(self, target_cycles=3.0, channel=1, frame_vertically=True):
        """Frame the waveform the way the front panel AUTO key does.

        This firmware exposes no autoset over SCPI -- the manual documents
        ``:AUTO`` only for the DMM, and ``:AUTOset``/``:AUTOSet``/``:AUTO``/
        ``:AUTOscale`` were each tried against a scrambled instrument and left
        every setting unchanged.  So the framing is computed here from the
        instrument's own measurements instead of delegated to the front panel.

        What takes effect is verified elsewhere: the horizontal scale (the number
        of captured cycles tracks it exactly) and the trigger level.  The
        vertical scale is computed and written, but this firmware accepts a
        vertical write without changing the acquisition, so the report says what
        was read back rather than claiming the framing happened.

        Returns a report dict; never raises for a merely disappointing result.
        """
        report = {
            "channel": channel, "timebase": None, "timebase_from": None,
            "trigger_level": None, "vertical_scale": None,
            "timebase_readback": None, "trigger_level_readback": None,
            "vertical_scale_readback": None, "applied": [], "notes": [],
            "measured_from": None,
        }
        if not self.is_hds:
            report["notes"].append("auto framing is implemented for HDS models only")
            return report
        if not self.waveform_data.channels:
            if not self.download_waveform_data():
                report["notes"].append("no capture available to measure")
                return report

        trace = self.frame_from_trace(channel)
        measured = self.get_measurements_numeric(channel)
        # The decoded trace is preferred: it is not affected by the range-flagging
        # that makes the instrument's own readings useless exactly when AUTO is
        # wanted, and it is the same data the plot shows.
        frequency = trace.get("frequency") or measured.get("Frequency")
        vpp = trace.get("vpp") or measured.get("Vpp") or measured.get("Vamp")
        vmean = trace.get("vmean")
        if vmean is None:
            vmean = measured.get("Vmean")
        report["measured_from"] = "decoded trace" if trace else "instrument measurements"

        # The decoded trace is what the plot and the grid show, and it is decoded
        # from the same codes and the same volts/div label the instrument's own
        # figures use, so the two agree by construction. Prefer the trace, and note
        # only the case where they still differ: a qualified (`>`) or stale reading,
        # or a capture that does not describe the signal. The one disagreement this
        # cannot see is the dangerous one - a label that has come apart from the
        # FRONT PANEL's gain moves both sides together, and no amount of comparing
        # them can reveal it on this firmware, where the panel is not readable.
        reported_vpp = measured.get("Vpp")
        amplitude = trace.get("vpp") or reported_vpp
        level = vmean if vmean is not None else measured.get("Vmean")
        level_source = "decoded trace" if trace.get("vpp") else "instrument measurements"
        if trace.get("vpp") and reported_vpp:
            ratio = trace["vpp"] / reported_vpp
            if ratio > 3.0 or ratio < 0.33:
                report["notes"].append(
                    "the decoded trace spans %.4g V while the instrument measures %.4g V "
                    "(%.1fx); both are drawn from the same volts/div label, so a gap this "
                    "wide means a qualified or stale reading rather than a decode fault - "
                    "check the volts/div on the instrument's front panel"
                    % (trace["vpp"], reported_vpp, ratio if ratio >= 1 else 1 / ratio))
                # No level from a source that cannot be reconciled: leave the
                # trigger where it is rather than aiming it with a number that one
                # of the two readings contradicts.
                disputed = True
            else:
                disputed = False
        else:
            disputed = False
        if disputed:
            level = None
            report["notes"].append("trigger level left alone: the two amplitude readings "
                                   "disagree too widely to aim it")
            # And withhold the vertical suggestion for the same reason: recommending
            # a volts/div from a number the instrument contradicts is a recommendation
            # the user cannot check.
            report["vertical_scale"] = None

        if frequency and frequency > 0:
            report["timebase_from"] = self.get_timebase_scale()
            wanted = target_cycles / (frequency * HDS_HORIZONTAL_DIVISIONS)
            choice = self.nearest_timebase(wanted)
            if choice:
                report["timebase"] = choice
                self.set_timebase_scale(choice)
                report["timebase_readback"] = self.get_timebase_scale()
                if self.match_scale(report["timebase_readback"], self.TIMEBASE_SCALES) == choice:
                    report["applied"].append("timebase")
        else:
            report["notes"].append("the instrument reported no frequency; time/div left alone")

        # A level outside the span that was measured would be a trigger point that
        # cannot be met, and writing one is not a framing success. When the two
        # sources disagree, the NARROWER span governs: a level that only fits the
        # wider one is exactly the case that once aimed a trigger at -2.26 V, which
        # the instrument read back as 4293 V.
        limit = abs(amplitude) if amplitude else None
        if reported_vpp:
            limit = min(limit, abs(reported_vpp)) if limit else abs(reported_vpp)
        if level is not None and limit and abs(level) > limit / 2.0 + 1e-12:
            report["notes"].append(
                "the level to aim at (%s from the %s) is outside the measured span of "
                "%s, so the trigger was left alone"
                % (self.format_level(level), level_source, self.format_level(limit / 2.0)))
            level = None
        if level is not None:
            requested = self.format_level(level)
            report["trigger_level"] = requested
            self.set_trigger_level(requested)
            reply = self.query(self.trigger_node("level") + "?")
            report["trigger_level_readback"] = reply
            # Confirmed, not assumed: on HDS271 V1.3.0 this node accepts writes
            # and never reflects them, so a write alone proves nothing.
            wanted = self.parse_scale(requested)
            got = self.parse_scale(reply) if reply else None
            if None not in (wanted, got) and abs(got - wanted) <= max(abs(wanted), 1e-9) * 0.05:
                report["applied"].append("trigger level")
            else:
                report["notes"].append(
                    "trigger level was written as %s but the instrument read back %r, "
                    "so the write could not be confirmed" % (requested, reply))
        else:
            report["notes"].append("no usable average level; trigger level left alone")

        if frame_vertically and amplitude and not disputed:
            probe = WaveformData._probe_factor(self.get_channel_probe(channel))
            # Aim the signal at about 70% of the vertical range, and REPORT it: the
            # volts/div is a front-panel setting on this firmware, since a write over
            # the interface changes the label and nothing else (writing a position
            # shift of one label-division moved the samples by 0 codes). Writing it
            # would leave the instrument reporting the signal at the label's ratio of
            # its real size, so the suggestion is handed to the user instead.
            wanted = (amplitude / (0.7 * HDS_VERTICAL_DIVISIONS)) / (probe or 1.0)
            choice = self.nearest_voltage_scale(wanted)
            if choice:
                report["vertical_scale"] = choice
                report["vertical_scale_readback"] = self.get_channel_scale(channel)
                report["notes"].append(
                    "volts/div for this signal would be about %s, but it is a front-panel "
                    "setting here: a write over this interface is accepted and changes "
                    "only the label, which scales the instrument's own readings and the "
                    "grid together, so it was not written"
                    % choice)
        self.download_waveform_data()
        return report

    # -- capture header cache ----------------------------------------------
    #: Last capture header, reused by the live loop; see download_waveform_data.
    _cached_header = None

    def send_command(self, command):
        """Write a setting, and drop any cached capture header.

        The cached header carries the scales the plot is decoded with, so a
        setting that changes must not leave a stale one behind.
        """
        result = self.legacy_send_command(command)
        if result and not str(command).strip().endswith("?"):
            self._cached_header = None
        return result

    # -- acquisition mode / memory depth / on-screen measurements ----------
    #: The HDS manual documents {SAMPle|PEAK} and {4K|8K}; the SDS constants
    #: above are a different instrument's and must not be overwritten, since
    #: this class serves both.
    ACQUIRE_MODES = ("SAMPle", "PEAK")
    HDS_MEMORY_DEPTHS = ("4K", "8K")

    def acquire_mode_choices(self):
        """Acquisition modes this instrument accepts."""
        return list(self.ACQUIRE_MODES) if self.is_hds else list(self.ACQ_TYPES)

    def memory_depth_choices(self):
        """Capture depths this instrument accepts."""
        return list(self.HDS_MEMORY_DEPTHS) if self.is_hds else list(self.MEMORY_DEPTHS)

    def get_acquire_mode(self):
        """'SAMPle' or 'PEAK' as reported, else None."""
        reply = self.query(":ACQuire:MODe?")
        return str(reply).strip() or None if reply else None

    def set_acquire_mode(self, mode):
        """Select the acquisition mode; returns what the instrument reports after."""
        self.send_command(":ACQuire:MODe %s" % mode)
        return self.get_acquire_mode()

    def get_memory_depth(self):
        """Points per trigger sample, as the header reports it ('4K'/'8K')."""
        reply = self.query(":ACQuire:DEPMem?")
        return str(reply).strip() or None if reply else None

    def set_memory_depth(self, depth):
        """Set 4K or 8K deep memory; returns the readback.

        Worth knowing: changing it does not change the screen capture at all -
        FULLSCREEN stays 600 bytes at either setting - so it costs nothing and
        saves nothing on a live capture.
        """
        self.send_command(":ACQuire:DEPMem %s" % depth)
        return self.get_memory_depth()

    def get_measurement_display(self):
        """Whether the instrument draws its own measurement readouts."""
        reply = self.query(":MEASurement:DISPlay?")
        return None if reply in (None, "") else str(reply).strip().upper() == "ON"

    def set_measurement_display(self, enabled):
        self.send_command(":MEASurement:DISPlay %s" % ("ON" if enabled else "OFF"))
        return self.get_measurement_display()

    # -- multimeter ---------------------------------------------------------
    #: Mnemonics the manual documents, mapped from what a UI might call them.
    DMM_NODES = {
        "VOLTAGE": "VOLTage", "VOLT": "VOLTage", "V": "VOLTage",
        "CURRENT": "CURRent", "CURR": "CURRent", "A": "CURRent", "AMP": "CURRent",
        "RESISTANCE": "RESistance", "RES": "RESistance", "OHM": "RESistance",
        "DIODE": "DIODe", "CONTINUITY": "CONTinuity", "CAPACITANCE": "CAPacitance",
    }
    #: The two the sub-type (AC/DC) query exists for.
    DMM_TYPED = ("VOLTage", "CURRent")

    def get_dmm_reading(self):
        """The value the multimeter is displaying, or None if it answers nothing."""
        return self.parse_scale(self.query(":DMM:MEAS?"))

    def get_dmm_function(self):
        """(function, subtype) in use, e.g. ('VOLTage', 'DC'), else (None, None).

        ``:DMM:CONFigure?`` answers the literal text "error" on this firmware,
        so the active function is found by asking each typed sub-node in turn:
        the selected one answers AC or DC, the other answers "error".
        """
        for function in self.DMM_TYPED:
            reply = self.query(":DMM:CONFigure:%s?" % function)
            token = str(reply or "").strip().upper()
            if token in ("AC", "DC"):
                return function, token
        return None, None

    def set_dmm_function(self, function, subtype=None):
        """Select the multimeter function; returns (function, subtype) confirmed.

        Not every write in this subsystem is reflected: REL takes effect, while
        switching voltage to current was accepted and changed nothing in
        testing. The caller therefore gets the instrument's own answer back and
        can say whether the change actually happened.
        """
        node = self.DMM_NODES.get(str(function or "").strip().upper())
        if node is None:
            return self.get_dmm_function()
        if node in self.DMM_TYPED and subtype:
            self.send_command(":DMM:CONFigure:%s %s" % (node, subtype))
        else:
            self.send_command(":DMM:CONFigure %s" % node)
        return self.get_dmm_function()

    def get_dmm_relative(self):
        """False when relative mode is off, the stored offset when it is on.

        The readback is the offset value rather than "ON", which is what the
        query actually answers once relative mode is engaged.
        """
        reply = self.query(":DMM:REL?")
        token = str(reply or "").strip()
        if not token:
            return None
        if token.upper() == "OFF":
            return False
        return self.parse_scale(token)

    def set_dmm_relative(self, enabled=True):
        """Relative mode on/off; returns the readback (False, or the offset)."""
        self.send_command(":DMM:REL %s" % ("ON" if enabled else "OFF"))
        return self.get_dmm_relative()

    def dmm_capabilities(self, refresh=False):
        """What this instrument's multimeter actually answers.

        Probed, not assumed: the manual documents resistance, diode, continuity
        and capacitance for the series, and this HDS271 is silent for all four,
        so the panel only offers what is really there.
        """
        cached = getattr(self, "_dmm_caps", None)
        if cached is not None and not refresh:
            return cached
        caps = {"reading": False, "typed": {}, "silent": [], "relative": False}
        caps["reading"] = self.query(":DMM:MEAS?") not in (None, "")
        for function in ("VOLTage", "CURRent", "RESistance", "DIODe",
                         "CONTinuity", "CAPacitance"):
            reply = self.query(":DMM:CONFigure:%s?" % function)
            token = str(reply or "").strip().upper()
            if token in ("AC", "DC"):
                caps["typed"][function] = token
            elif token == "ERROR":
                # The node exists; it just is not the selected function.
                caps["typed"][function] = None
            else:
                caps["silent"].append(function)
        caps["relative"] = self.query(":DMM:REL?") not in (None, "")
        self._dmm_caps = caps
        return caps
