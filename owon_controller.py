import json
import logging
import socket
import struct
import threading
import time
from typing import Optional

import serial
from serial.tools import list_ports

from waveform_data import WaveformData


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
        return self.send_command(f"{self._cmd(':HORIzontal:SCALe', ':TIMebase:SCALe')} {scale}")

    def get_timebase_scale(self):
        return self.query(f"{self._cmd(':HORIzontal:SCALe?', ':TIMebase:SCALe?')}")

    def set_timebase_offset(self, offset):
        return self.send_command(f"{self._cmd(':HORIzontal:OFFset', ':TIMebase:HOFFset')} {offset}")

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
        return self.send_command(f"{self._channel_node(channel, 'PROBe')} {attenuation}")

    def get_channel_probe(self, channel):
        return self.query(self._channel_node(channel, "PROBe?"))

    def set_channel_scale(self, channel, scale):
        return self.send_command(f"{self._channel_node(channel, 'SCALe')} {scale}")

    def get_channel_scale(self, channel):
        reply = self.query(self._channel_node(channel, "SCALe?"))
        return self.match_scale(reply, self.VOLTAGE_SCALES) or reply

    def set_channel_offset(self, channel, offset):
        return self.send_command(f"{self._channel_node(channel, 'OFFSet')} {offset}")

    def get_channel_offset(self, channel):
        return self.query(self._channel_node(channel, "OFFSet?"))

    # -- timebase / acquisition --------------------------------------------
    def get_timebase_scale(self):
        reply = self.query(f"{self._cmd(':HORIzontal:SCALe?', ':TIMebase:SCALe?')}")
        return self.match_scale(reply, self.TIMEBASE_SCALES) or reply

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
