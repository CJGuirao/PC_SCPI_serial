"""Controller for OWON HDS200/HDS300 oscilloscopes over USB HID.

``OWONScopeController`` is what the rest of the program talks to.
It owns the connection and the last capture.

The parts it is made of:

* :class:`~modernlab.instrument.dialect.hds.HdsDialect` -- HDS SCPI nodes
* :class:`~modernlab.instrument.framing.FramingSettle` / ``FramingWatch``
* :class:`~modernlab.instrument.measurements.Measurements`

Transport is always raw USB HID (pyusb + libusb).
"""

import json
import logging
import threading
import time
from typing import Optional

from modernlab.instrument.capture.waveform import (
    HDS_CODES_PER_DIVISION,
    HDS_HORIZONTAL_DIVISIONS,
    HDS_VERTICAL_DIVISIONS,
    WaveformData,
)
from modernlab.instrument.dialect.hds import HdsDialect
from modernlab.instrument.framing import FramingSettle, FramingWatch
from modernlab.instrument.measurements import Measurements

__all__ = ["OWONScopeController"]


class OWONScopeController(HdsDialect, FramingSettle, FramingWatch, Measurements):
    """Controller for OWON HDS200 / HDS300 oscilloscopes."""

    HDS_SERIES = ("hds200", "hds300")

    def __init__(self, family: str = "hds"):
        self.family = (family or "hds").lower()
        self.connection = None
        self.connection_type = None
        self.is_connected = False
        self.waveform = WaveformData()
        self.lock = threading.Lock()
        self.series = self.family
        self.model = ""
        self.serial_number = ""
        self.firmware = ""
        self._probed_nodes = {}
        # Volts per sample code, measured against the instrument's own readings.
        self._calibrated_volts_per_code = None
        self._calibrated_offset = None
        # A framing write needs time to land before the next capture.
        self._framing_settle_until = 0.0
        # Last capture header, reused by the live loop.
        self._cached_header = None
        # Probed once per connect.
        self._channel_count = None
        self._dmm_caps = None

    # ── is_hds property (always True — kept for test compatibility) ───────────

    @property
    def is_hds(self):
        """Always True: this controller only supports HDS series."""
        return True

    # ── Connection ────────────────────────────────────────────────────────────

    def find_hds_device(self):
        """Info for the first attached OWON HDS device, or None."""
        try:
            from modernlab.instrument.transport.usb_hid import list_owon_devices
            devices = list_owon_devices()
        except Exception as exc:
            logging.error("USB scan error: %s", exc)
            return None
        return devices[0] if devices else None

    def connect_usb_hid(self, serial=None, timeout=2.0):
        """Open the raw USB HID interface (VID 0x5345 / PID 0x1234)."""
        try:
            from modernlab.instrument.transport.usb_hid import HdsHidTransport
            transport = HdsHidTransport(serial=serial, timeout=timeout)
            transport.open()
            self.connection = transport
            self.connection_type = "usb"
            self.is_connected = True
            return True
        except Exception as exc:
            logging.error("USB HID connection error: %s", exc)
            self.disconnect()
            return False

    def connect_usb(self, port="auto", baudrate=115200):
        """Connect via USB HID."""
        if self.connect_usb_hid():
            self.identify_model()
            return True
        logging.warning("HDS USB HID link unavailable")
        return False

    def connect_hds(self, serial=None):
        """Connect using a specific USB serial number."""
        if self.connect_usb_hid(serial=serial):
            self.identify_model()
            return True
        return False

    def reconnect(self, serial=None):
        """Reopen the transport without changing settings.

        Called by the auto-reconnect watchdog. Returns (success, idn_string).
        """
        try:
            self.disconnect()
        except Exception as exc:
            logging.warning("reconnect: disconnect raised %s", exc)
        serial = serial or self.serial_number or None
        if not self.connect_usb_hid(serial=serial):
            return False, ""
        try:
            result = self.identify_model() or {}
            idn = result.get("idn", "")
        except Exception as exc:
            logging.warning("reconnect: identify_model raised %s", exc)
            idn = ""
        return True, idn

    def identify_model(self):
        """Read *IDN? and populate model/series/serial/firmware."""
        raw = (self.get_idn() or "").strip()
        parts = [p.strip() for p in raw.split(",")]
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
            "idn": raw, "model": self.model, "series": self.series,
            "serial": self.serial_number, "firmware": self.firmware,
        }

    # ── Transport ─────────────────────────────────────────────────────────────

    def _hds_transport(self):
        """The HID transport if connected, else None."""
        conn = self.connection
        return conn if conn is not None and hasattr(conn, "exchange_text") else None

    def query(self, command):
        """Send a command and return the reply.

        Over HID the reply must be read immediately — the scope pushes it
        into the interrupt endpoint and discards it if the host is slow.
        """
        transport = self._hds_transport()
        if transport is None:
            return None
        try:
            with self.lock:
                return transport.exchange_text(command)
        except Exception as exc:
            logging.error("HDS query error for %r: %s", command, exc)
            return None

    def query_binary(self, command, timeout=10):
        """Send a command and return the binary reply."""
        transport = self._hds_transport()
        if transport is None:
            return None
        try:
            with self.lock:
                return transport.exchange(command, payload_timeout=timeout)
        except Exception as exc:
            logging.error("HDS binary query error for %r: %s", command, exc)
            return None

    def send_command(self, command):
        """Write a command and invalidate the cached capture header."""
        transport = self._hds_transport()
        if transport is None:
            return None
        try:
            with self.lock:
                result = transport.exchange_text(command)
        except Exception as exc:
            logging.error("HDS send error for %r: %s", command, exc)
            return None
        if not str(command).strip().endswith("?"):
            self._cached_header = None
        return result

    # ── Channel commands ──────────────────────────────────────────────────────

    def set_channel_display(self, channel, state):
        return self.send_command(
            f"{self._channel_node(channel, 'DISPlay')} {'ON' if state else 'OFF'}"
        )

    def get_channel_display(self, channel):
        return self.query(self._channel_node(channel, "DISPlay?"))

    def set_channel_coupling(self, channel, coupling):
        return self.send_command(
            f"{self._channel_node(channel, 'COUPling')} {coupling}"
        )

    def get_channel_coupling(self, channel):
        reply = self.query(self._channel_node(channel, "COUPling?"))
        return self.match_choice(reply, self.COUPLING_MODES) or reply

    def set_channel_scale(self, channel, scale):
        result = self.send_command(f"{self._channel_node(channel, 'SCALe')} {scale}")
        self.mark_framing_settle()
        return result

    def get_channel_scale(self, channel):
        reply = self.query(self._channel_node(channel, "SCALe?"))
        return self.match_scale(reply, self.VOLTAGE_SCALES) or reply

    def set_channel_probe(self, channel, attenuation):
        result = self.send_command(
            f"{self._channel_node(channel, 'PROBe')} {attenuation}"
        )
        self.mark_framing_settle()
        return result

    def get_channel_probe(self, channel):
        return self.query(self._channel_node(channel, "PROBe?"))

    def set_channel_offset(self, channel, offset):
        result = self.send_command(
            f"{self._channel_node(channel, 'OFFSet')} {offset}"
        )
        self.mark_framing_settle()
        return result

    def get_channel_offset(self, channel):
        return self.query(self._channel_node(channel, "OFFSet?"))

    # ── Trigger ───────────────────────────────────────────────────────────────

    def set_trigger_slope(self, slope):
        return HdsDialect.set_trigger_slope(self, slope)

    def get_trigger_slope(self):
        return HdsDialect.get_trigger_slope(self)

    def set_trigger_coupling(self, coupling):
        return HdsDialect.set_trigger_coupling(self, coupling)

    def get_trigger_coupling(self):
        return HdsDialect.get_trigger_coupling(self)

    get_timebase_scale = HdsDialect.get_timebase_scale
    get_trigger_mode = HdsDialect.get_trigger_mode
    get_trigger_source = HdsDialect.get_trigger_source
    get_acquire_type = HdsDialect.get_acquire_type
    get_memory_depth = HdsDialect.get_memory_depth
    get_trigger_sweep = HdsDialect.get_trigger_sweep
    get_trigger_status = HdsDialect.get_trigger_status
    get_trigger_level_volts = HdsDialect.get_trigger_level_volts
    get_horizontal_position_seconds = HdsDialect.get_horizontal_position_seconds

    # ── Measurements ──────────────────────────────────────────────────────────

    def get_all_measurements(self, channel=1):
        results = {}
        for label, node in self.HDS_MEASUREMENT_ITEMS:
            value = self.query(f":MEASUrement:CH{channel}:{node}?")
            if value not in (None, ""):
                results[label] = value
        return results

    def measure_all(self, channel=1):
        payload = self.get_all_measurements(channel)
        return json.dumps(payload, ensure_ascii=False) if payload else None

    # ── Acquisition ───────────────────────────────────────────────────────────

    ACQUIRE_MODES = ("SAMPle", "PEAK")
    HDS_MEMORY_DEPTHS = ("4K", "8K")
    MEMORY_DEPTHS = HdsDialect.MEMORY_DEPTHS

    def acquire_mode_choices(self):
        return list(self.ACQUIRE_MODES)

    def memory_depth_choices(self):
        return list(self.HDS_MEMORY_DEPTHS)

    def get_acquire_mode(self):
        reply = self.query(":ACQuire:MODe?")
        return str(reply).strip() or None if reply else None

    def set_acquire_mode(self, mode):
        self.send_command(":ACQuire:MODe %s" % mode)
        return self.get_acquire_mode()

    def set_memory_depth(self, depth):
        """Set 4K or 8K memory depth (does not change the 600-byte screen capture)."""
        self.send_command(":ACQuire:DEPMem %s" % depth)
        return self.get_memory_depth()

    def get_measurement_display(self):
        reply = self.query(":MEASurement:DISPlay?")
        return None if reply in (None, "") else str(reply).strip().upper() == "ON"

    def set_measurement_display(self, enabled):
        self.send_command(":MEASurement:DISPlay %s" % ("ON" if enabled else "OFF"))
        return self.get_measurement_display()

    def run(self, enabled=True):
        """Start (RUN) or pause (STOP) acquisition."""
        return self.send_command(":RUNning %s" % ("RUN" if enabled else "STOP"))

    def get_run_state(self):
        """Return 'RUN', 'STOP', or None."""
        try:
            reply = (self.query(":RUNning?") or "").strip().upper()
        except Exception:
            return None
        return reply if reply in ("RUN", "STOP") else None

    def download_waveform_data(self, calibrate=False, reuse_header=False):
        """Capture the on-screen waveform into self.waveform.

        reuse_header=True skips the 254 ms header fetch when the settings
        have not changed. calibrate=True anchors the volts axis on the
        instrument's own Vmin + Vpp readings.
        """
        if not self.is_connected:
            logging.error("Not connected to oscilloscope")
            return False
        try:
            self.wait_for_framing_settle()
            transport = self._hds_transport()
            if transport is not None:
                transport.reset_input_buffer()
            header = self._cached_header if reuse_header else None
            if header is None:
                header = self._capture_header() or self.retry_capture_header()
                if header is None:
                    self._cached_header = None
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

            self.waveform = WaveformData()
            self.waveform.parse_hds_capture(
                header, channel_payloads,
                calibrated_volts_per_code=self._calibrated_volts_per_code,
                calibrated_offset=self._calibrated_offset)
            if not self.waveform.channels:
                return False
            if calibrate:
                self.calibrate_capture()
            return True
        except Exception as exc:
            logging.error("Error downloading waveform data: %s", exc)
            return False

    def retry_capture_header(self):
        """Retry the capture header once (transient misses are expected)."""
        logging.warning("Capture header missing; retrying once")
        return self._capture_header()

    def _capture_header(self):
        """Fetch the capture header JSON and fix stale scale/probe fields."""
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
        """Replace stale header scale/probe with fresh SCPI queries."""
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
        """Channel names to fetch, derived from the header's CHANNEL list."""
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

    def calibrate_capture(self, channel=None, pin_extremes=True):
        """Anchor the volts axis on the instrument's own Vmin + Vpp readings.

        pin_extremes=True (default): 2 queries (64 ms).
        pin_extremes=False: refines offset from AVERage only.
        """
        if pin_extremes:
            measurements = {}
            for entry in self.waveform.channels:
                name = str(entry.get("name", "CH1")).upper()
                ch = name[2:] if name[:2].upper() == "CH" else name
                for label, node in (("Vmin", "MIN"), ("Vpp", "PKPK")):
                    raw = self.query(":MEASUrement:CH%s:%s?" % (ch, node))
                    value = self.parse_measurement(raw, node)
                    if value is not None:
                        measurements[label] = value
        else:
            measurements = {}
            for entry in self.waveform.channels:
                name = str(entry.get("name", "CH1")).upper()
                ch = name[2:] if name[:2].upper() == "CH" else name
                measurements.update(self.get_measurements_numeric(ch))
        calibrated = self.waveform.calibrate_from_measurements(
            measurements, channel=channel, pin_extremes=pin_extremes)
        if calibrated:
            for entry in self.waveform.channels:
                slope = entry.get("voltage_per_point")
                cal = entry.get("calibration", {})
                offset = cal.get("offset_volts")
                if slope:
                    self._calibrated_volts_per_code = slope
                    self._calibrated_offset = offset
                    entry["true_volts_per_div"] = slope * HDS_CODES_PER_DIVISION
                    entry["volts_per_code_source"] = "instrument measurements"
                    break
        return calibrated

    # ── Instrument shape ──────────────────────────────────────────────────────

    def forget_channel_count(self):
        """Reset the cached channel count (call after reconnect)."""
        self._channel_count = None

    def get_channel_count(self):
        """How many channels the instrument has (probed, not assumed).

        HDS271 is silent for :CH2:SCALe? — absence of reply means single channel.
        """
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

    def get_capabilities(self):
        """Probe which optional capture paths this instrument answers."""
        capabilities = {
            "screen": True, "deep_memory": False, "bitmap": False,
            "measurements": [], "series": self.series, "model": self.model,
            "channels": self.get_channel_count(),
        }
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

    @staticmethod
    def format_level(volts):
        """Format volts as the instrument spells a level: '100mV', '1.00V'."""
        if volts is None:
            return None
        if abs(volts) < 1.0:
            return "%dmV" % int(round(volts * 1000))
        return "%.2fV" % volts

    def frame_from_trace(self, channel=1):
        """Measure frequency/Vpp/Vmean from the decoded samples.

        Used instead of :MEASurement? which returns range-flagged values or
        refuses to answer frequency at the wrong scale.
        """
        entry = None
        for item in self.waveform.channels or []:
            if str(item.get("name") or "").upper().endswith(str(channel)):
                entry = item
                break
        if entry is None and self.waveform.channels:
            entry = self.waveform.channels[0]
        if not entry:
            return {}

        data = [v for v in entry.get("waveform") or [] if v is not None]
        interval = entry.get("point_interval")
        if len(data) < 4 or not interval or interval <= 0:
            return {}

        high = max(data)
        low = min(data)
        result = {
            "vpp": high - low, "vmax": high, "vmin": low,
            "vmean": sum(data) / len(data),
            "span": interval * (len(data) - 1),
        }
        midpoint = (high + low) / 2.0
        edges = [i for i in range(1, len(data))
                 if data[i - 1] <= midpoint < data[i]]
        if len(edges) > 1:
            result["frequency"] = (len(edges) - 1) / ((edges[-1] - edges[0]) * interval)
            result["cycles"] = len(edges)
        return result

    def auto_frame(self, target_cycles=3.0, channel=1, frame_vertically=True):
        """Frame the waveform — tries hardware AUTO first, then software.

        Tries :AUTOSet / :AUTO hardware candidates (silently ignored on
        HDS271 V1.3.0). Then computes the timebase from the measured
        frequency and aims the trigger at the signal midpoint.
        """
        import time as _time
        hw_candidates = (":AUTOSet", ":AUTOset EXECute", ":AUTO")
        hw_tried = []
        timebase_before = None
        try:
            timebase_before = self.get_timebase_scale()
        except Exception:
            pass
        for cmd in hw_candidates:
            try:
                self.send_command(cmd)
                hw_tried.append(cmd)
            except Exception:
                pass
        if hw_tried:
            _time.sleep(1.5)

        report = {
            "channel": channel, "timebase": None, "timebase_from": None,
            "trigger_level": None, "vertical_scale": None,
            "timebase_readback": None, "trigger_level_readback": None,
            "vertical_scale_readback": None, "applied": [], "notes": [],
            "measured_from": None, "hw_auto_tried": hw_tried,
        }

        if hw_tried:
            try:
                timebase_after = self.get_timebase_scale()
            except Exception:
                timebase_after = None
            if timebase_before and timebase_after and timebase_before != timebase_after:
                report["notes"].append(
                    "hardware AUTO responded: timebase %s -> %s"
                    % (timebase_before, timebase_after))
                report["applied"].append("hardware_auto")
            else:
                report["notes"].append(
                    "hardware AUTO (%s) sent but timebase unchanged"
                    % ", ".join(hw_tried))

        self.download_waveform_data(reuse_header=False, calibrate=True)
        trace = self.frame_from_trace(channel)
        measured = self.get_measurements_numeric(channel)

        frequency = trace.get("frequency") or measured.get("Frequency")
        vpp = trace.get("vpp") or measured.get("Vpp") or measured.get("Vamp")
        vmean = trace.get("vmean")
        if vmean is None:
            vmean = measured.get("Vmean")
        report["measured_from"] = "decoded trace" if trace else "instrument measurements"

        reported_vpp = measured.get("Vpp")
        amplitude = trace.get("vpp") or reported_vpp
        level = vmean if vmean is not None else measured.get("Vmean")
        level_source = "decoded trace" if trace.get("vpp") else "instrument measurements"

        disputed = False
        if trace.get("vpp") and reported_vpp:
            ratio = trace["vpp"] / reported_vpp
            if ratio > 3.0 or ratio < 0.33:
                report["notes"].append(
                    "decoded trace spans %.4g V while the instrument measures %.4g V (%.1fx); disagree"
                    % (trace["vpp"], reported_vpp, ratio if ratio >= 1 else 1 / ratio))
                disputed = True

        if disputed:
            level = None
            report["vertical_scale"] = None

        if frequency and frequency > 0:
            report["timebase_from"] = self.get_timebase_scale()
            wanted = target_cycles / (frequency * HDS_HORIZONTAL_DIVISIONS)
            choice = self.nearest_timebase(wanted)
            if choice:
                report["timebase"] = choice
                self.set_timebase_scale(choice)
                report["timebase_readback"] = self.get_timebase_scale()
                if self.match_scale(report["timebase_readback"],
                                    self.TIMEBASE_SCALES) == choice:
                    report["applied"].append("timebase")
        else:
            report["notes"].append("no frequency measured; time/div left alone")

        limit = abs(amplitude) if amplitude else None
        if reported_vpp:
            limit = min(limit, abs(reported_vpp)) if limit else abs(reported_vpp)
        if level is not None and limit and abs(level) > limit / 2.0 + 1e-12:
            report["notes"].append(
                "level to aim at (%s) is outside the measured span of %s; trigger left alone"
                % (self.format_level(level), self.format_level(limit / 2.0)))
            level = None

        if level is not None:
            requested = self.format_level(level)
            report["trigger_level"] = requested
            self.set_trigger_level(requested)
            reply = self.query(self.trigger_node("level") + "?")
            report["trigger_level_readback"] = reply
            wanted_v = self.parse_scale(requested)
            got_v = self.parse_scale(reply) if reply else None
            if None not in (wanted_v, got_v) and \
               abs(got_v - wanted_v) <= max(abs(wanted_v), 1e-9) * 0.05:
                report["applied"].append("trigger level")
            else:
                report["notes"].append(
                    "trigger level written as %s but read back %r"
                    % (requested, reply))
        else:
            report["notes"].append("no usable level; trigger left alone")

        if frame_vertically and amplitude and not disputed:
            probe = WaveformData._probe_factor(self.get_channel_probe(channel))
            wanted = (amplitude / (0.7 * HDS_VERTICAL_DIVISIONS)) / (probe or 1.0)
            choice = self.nearest_voltage_scale(wanted)
            if choice:
                report["vertical_scale"] = choice
                report["vertical_scale_readback"] = self.get_channel_scale(channel)
                report["notes"].append(
                    "suggested volts/div: %s — not written (cosmetic-only on this firmware)"
                    % choice)
        self.download_waveform_data()
        return report

    # ── Multimeter ────────────────────────────────────────────────────────────

    DMM_NODES = {
        "VOLTAGE": "VOLTage", "VOLT": "VOLTage", "V": "VOLTage",
        "CURRENT": "CURRent", "CURR": "CURRent", "A": "CURRent", "AMP": "CURRent",
        "RESISTANCE": "RESistance", "RES": "RESistance", "OHM": "RESistance",
        "DIODE": "DIODe", "CONTINUITY": "CONTinuity", "CAPACITANCE": "CAPacitance",
    }
    DMM_TYPED = ("VOLTage", "CURRent")

    def get_dmm_reading(self):
        return self.parse_scale(self.query(":DMM:MEAS?"))

    def get_dmm_function(self):
        """Return (function, subtype) or (None, None)."""
        for function in self.DMM_TYPED:
            reply = self.query(":DMM:CONFigure:%s?" % function)
            token = str(reply or "").strip().upper()
            if token in ("AC", "DC"):
                return function, token
        return None, None

    def set_dmm_function(self, function, subtype=None):
        node = self.DMM_NODES.get(str(function or "").strip().upper())
        if node is None:
            return self.get_dmm_function()
        if node in self.DMM_TYPED and subtype:
            self.send_command(":DMM:CONFigure:%s %s" % (node, subtype))
        else:
            self.send_command(":DMM:CONFigure %s" % node)
        return self.get_dmm_function()

    def get_dmm_relative(self):
        reply = self.query(":DMM:REL?")
        token = str(reply or "").strip()
        if not token:
            return None
        return False if token.upper() == "OFF" else self.parse_scale(token)

    def set_dmm_relative(self, enabled=True):
        self.send_command(":DMM:REL %s" % ("ON" if enabled else "OFF"))
        return self.get_dmm_relative()

    def dmm_capabilities(self, refresh=False):
        """Probe what this instrument's multimeter actually answers."""
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
                caps["typed"][function] = None
            else:
                caps["silent"].append(function)
        caps["relative"] = self.query(":DMM:REL?") not in (None, "")
        self._dmm_caps = caps
        return caps
