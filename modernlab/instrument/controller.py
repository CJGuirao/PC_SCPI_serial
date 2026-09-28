"""The controller: one instrument, both dialects, over whichever link is there.

`OWONScopeController` is what the rest of the program talks to.  It owns the
connection and the last capture, and it is the only place that knows which family
it is on: everything whose node form differs between an SDS and an HDS instrument
is branched here, once, rather than being redefined over itself.

The parts it is made of:

* :class:`~modernlab.instrument.dialect.sds.SdsDialect` -- the legacy serial/LAN
  command set, with the ladder and choice lists the panel builds from
* :class:`~modernlab.instrument.dialect.hds.HdsDialect` -- the HDS nodes and the
  reply-normalising helpers
* :class:`~modernlab.instrument.framing.FramingSettle` and
  :class:`~modernlab.instrument.framing.FramingWatch` -- letting a framing write
  land, and noticing one made on the instrument
* :class:`~modernlab.instrument.measurements.Measurements` -- readings as
  numbers, qualifiers and all

Transports are untouched: the USB HID link (:mod:`...transport.usb_hid`), the
serial object and the LAN socket are all still just ``self.connection``.

This used to be one flat class in which the HDS definitions appeared *after* the
SDS ones under the same names, so each HDS method replaced its SDS namesake
outright and a fallback written as ``OWONScopeController.query(self, ...)``
resolved back to the HDS override and recursed until the stack died.  Two
separate bases cannot do that to each other, so the aliases that papered over it
are gone.
"""

import json
import logging
import struct
import threading
import time
from typing import Optional

from modernlab.instrument.capture.waveform import (HDS_CODES_PER_DIVISION, HDS_HORIZONTAL_DIVISIONS,
                           HDS_VERTICAL_DIVISIONS, WaveformData)
from modernlab.instrument.dialect.hds import HdsDialect
from modernlab.instrument.dialect.sds import SdsDialect
from modernlab.instrument.framing import FramingSettle, FramingWatch
from modernlab.instrument.measurements import Measurements

__all__ = ["OWONScopeController"]


class OWONScopeController(SdsDialect, HdsDialect, FramingSettle, FramingWatch, Measurements):
    """Controller for OWON SDS / HDS oscilloscopes."""

    def __init__(self, family: str = "hds"):
        self.family = (family or "hds").lower()
        self.connection = None
        self.connection_type = None
        self.is_connected = False
        self.waveform = WaveformData()
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
        # instrument needs a moment to re-acquire.  The window itself is
        # FramingSettle.FRAMING_SETTLE_SECONDS; see that class.
        self._framing_settle_until = 0.0
        # Last capture header, reused by the live loop; see download_waveform_data.
        self._cached_header = None
        # Probed once per connect by get_channel_count().
        self._channel_count = None
        # Probed once by dmm_capabilities().
        self._dmm_caps = None

    @property
    def is_hds(self):
        return self.family.startswith("hds")

    def _cmd(self, hds: str, legacy: Optional[str] = None):
        return hds if self.is_hds or not legacy else legacy

    # ---- HDS USB (raw HID) support ----------------------------------------
    # Verified on an HDS271, firmware V1.3.0: the scope carries ASCII SCPI in
    # 64-byte HID reports on interface 0 (interrupt OUT 0x01 / IN 0x81).  There
    # is no serial interface, so the transport is pyusb rather than pyserial.


    HDS_SERIES = ("hds200", "hds300")

    def find_hds_device(self):
        """Info for the first OWON HDS device on USB, or None."""
        try:
            from modernlab.instrument.transport.usb_hid import list_owon_devices
            devices = list_owon_devices()
        except Exception as exc:
            logging.error("USB scan error: %s", exc)
            return None
        return devices[0] if devices else None

    def connect_usb_hid(self, serial=None, timeout=2.0):
        """Connect over the raw USB HID interface.  Works with any driver."""
        try:
            from modernlab.instrument.transport.usb_hid import HdsHidTransport
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

    #: The legacy USB entry point.  It is looked up on the instance by
    #: ``connect_usb`` above (which used to be written ``self.connect_usb_serial``
    #: in the flat class), so it stays a plain assignment here.
    connect_usb_serial = SdsDialect.connect_usb

    def connect_hds(self, serial=None):
        """USB HID first, then the serial and LAN paths as fallback."""
        if self.connect_usb_hid(serial=serial):
            self.identify_model()
            return True
        port = self.detect_usb_port()
        if port:
            return self.connect_usb(port)
        return False

    def reconnect(self, serial=None):
        """Close the current transport and reopen it without changing any settings.

        Called by the auto-reconnect watchdog when live captures start failing.
        Returns ``(success, idn_string)`` — the same shape ``open_transport``
        returns so the caller can route both paths through one handler.

        Only the HDS USB HID path is attempted here: that is the one transport
        that can drop mid-session (the endpoint stalls, the firmware resets, or
        the cable is briefly unplugged).  LAN and serial transports are not
        retried automatically because their failure modes are different.
        """
        try:
            self.disconnect()
        except Exception as exc:
            logging.warning("reconnect: disconnect raised %s", exc)
        if not self.is_hds:
            return False, ""
        serial = serial or self.serial_number or None
        opened = self.connect_usb_hid(serial=serial)
        if not opened:
            return False, ""
        try:
            result = self.identify_model() or {}
            idn = result.get("idn", "")
        except Exception as exc:
            logging.warning("reconnect: identify_model raised %s", exc)
            idn = ""
        return True, idn

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

    # ================= HDS command dialect ====================================
    # Everything below is the HDS instrument's own.  Where the SDS dialect above
    # uses a different node, the family is checked here and the right base called
    # - never a same-named definition replacing another.

    # The SDS implementations of these are on ``SdsDialect`` under the same
    # names, so the family is routed here rather than the method being
    # redefined: an SDS write must reach ``:CHANnel1:SCALe`` and, crucially,
    # must reach a definition that cannot call back into its own HDS override.
    #
    # The HDS forms are written out in full because the routing has to happen
    # per call, and ``_channel_node`` is the one node form that differs.

    def set_channel_display(self, channel, state):
        if not self.is_hds:
            return SdsDialect.set_channel_display(self, channel, state)
        return self.send_command(f"{self._channel_node(channel, 'DISPlay')} "
                                 f"{'ON' if state else 'OFF'}")

    def get_channel_display(self, channel):
        return self.query(self._channel_node(channel, "DISPlay?"))

    def set_channel_coupling(self, channel, coupling):
        if not self.is_hds:
            return SdsDialect.set_channel_coupling(self, channel, coupling)
        return self.send_command(f"{self._channel_node(channel, 'COUPling')} {coupling}")

    def get_channel_coupling(self, channel):
        reply = self.query(self._channel_node(channel, "COUPling?"))
        return self.match_choice(reply, self.COUPLING_MODES) or reply

    def set_channel_scale(self, channel, scale):
        if not self.is_hds:
            return SdsDialect.set_channel_scale(self, channel, scale)
        result = self.send_command(f"{self._channel_node(channel, 'SCALe')} {scale}")
        self.mark_framing_settle()
        return result

    def get_channel_scale(self, channel):
        if not self.is_hds:
            return SdsDialect.get_channel_scale(self, channel)
        reply = self.query(self._channel_node(channel, "SCALe?"))
        return self.match_scale(reply, self.VOLTAGE_SCALES) or reply

    def set_channel_probe(self, channel, attenuation):
        if not self.is_hds:
            return SdsDialect.set_channel_probe(self, channel, attenuation)
        result = self.send_command(f"{self._channel_node(channel, 'PROBe')} {attenuation}")
        self.mark_framing_settle()
        return result

    def get_channel_probe(self, channel):
        if not self.is_hds:
            return SdsDialect.get_channel_probe(self, channel)
        return self.query(self._channel_node(channel, "PROBe?"))

    def set_channel_offset(self, channel, offset):
        if not self.is_hds:
            return SdsDialect.set_channel_offset(self, channel, offset)
        result = self.send_command(f"{self._channel_node(channel, 'OFFSet')} {offset}")
        self.mark_framing_settle()
        return result

    def get_channel_offset(self, channel):
        return self.query(self._channel_node(channel, "OFFSet?"))

    def set_channel_scale_sds(self, channel, scale):
        return SdsDialect.set_channel_scale(self, channel, scale)

    def get_channel_scale_sds(self, channel):
        return SdsDialect.get_channel_scale(self, channel)

    def set_channel_probe_sds(self, channel, attenuation):
        return SdsDialect.set_channel_probe(self, channel, attenuation)

    def get_channel_probe_sds(self, channel):
        return SdsDialect.get_channel_probe(self, channel)

    def set_channel_offset_sds(self, channel, offset):
        return SdsDialect.set_channel_offset(self, channel, offset)

    # -- method resolution ---------------------------------------------------
    #: The two families answer these under different nodes, so each name belongs
    #: to exactly one base here (``HdsDialect``), and the ``legacy_*`` names below
    #: are the hooks its non-HDS branch calls.  They are bound here, where both
    #: dialects are in scope, so neither base has to import the other.
    #:
    #: These names existed before the split too, for the same job.  With the
    #: dialects in separate bases nothing shadows anything any more, so they are
    #: no longer load-bearing for correctness -- but they are still the documented
    #: way to reach the legacy implementation of a name that HDS overrides, and
    #: ``send_command`` / ``query`` / ``query_serial`` / ``query_binary`` /
    #: ``read_response`` / ``_read_binary_packet`` / ``connect_usb`` /
    #: ``download_waveform_data`` all still resolve through them, so a caller that
    #: replaced ``scope.legacy_send_command`` for a test or a stub keeps working.
    legacy_send_command = SdsDialect.send_command
    legacy_query = SdsDialect.query
    legacy_query_binary = SdsDialect.query_binary
    legacy_read_binary_packet = SdsDialect._read_binary_packet
    legacy_download_waveform_data = SdsDialect.download_waveform_data
    legacy_set_trigger_slope = SdsDialect.set_trigger_slope_legacy
    legacy_get_trigger_slope = SdsDialect.get_trigger_slope_legacy
    legacy_set_trigger_coupling = SdsDialect.set_trigger_coupling_legacy
    legacy_get_trigger_coupling = SdsDialect.get_trigger_coupling_legacy

    #: These four names are answered by the HDS dialect for an HDS instrument and
    #: by the legacy one for everything else.  The bodies are written out rather
    #: than aliased to ``HdsDialect.set_trigger_slope`` because ``SdsDialect``
    #: carries a definition under the same name, and which base wins is decided by
    #: the MRO - an alias would leave the bare name pointing at whichever base
    #: came first, which is what the flat class's shadowing used to do silently.
    def set_trigger_slope(self, slope):
        if self.is_hds:
            return HdsDialect.set_trigger_slope(self, slope)
        return self.legacy_set_trigger_slope(slope)

    def get_trigger_slope(self):
        if self.is_hds:
            return HdsDialect.get_trigger_slope(self)
        return self.legacy_get_trigger_slope()

    def set_trigger_coupling(self, coupling):
        if self.is_hds:
            return HdsDialect.set_trigger_coupling(self, coupling)
        return self.legacy_set_trigger_coupling(coupling)

    def get_trigger_coupling(self):
        if self.is_hds:
            return HdsDialect.get_trigger_coupling(self)
        return self.legacy_get_trigger_coupling()
    get_timebase_scale = HdsDialect.get_timebase_scale
    get_trigger_mode = HdsDialect.get_trigger_mode
    get_trigger_source = HdsDialect.get_trigger_source
    get_acquire_type = HdsDialect.get_acquire_type
    get_memory_depth = HdsDialect.get_memory_depth
    get_trigger_sweep = HdsDialect.get_trigger_sweep
    get_trigger_status = HdsDialect.get_trigger_status
    get_trigger_level_volts = HdsDialect.get_trigger_level_volts
    get_horizontal_position_seconds = HdsDialect.get_horizontal_position_seconds

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

    #: The legacy capture path, reachable by name the way the old module said it
    #: was.  Nothing in the program calls them; ``SdsDialect`` is the real home.
    measure_all_sds = SdsDialect.measure_all
    get_all_measurements_sds = SdsDialect.get_all_measurements

    def measure_all(self, channel=1):
        if not self.is_hds:
            return self.measure_all_sds(channel)
        payload = self.get_all_measurements(channel)
        return json.dumps(payload, ensure_ascii=False) if payload else None

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

    # -- acquisition --------------------------------------------------------
    def download_waveform_data(self, calibrate=False, reuse_header=False):
        """Capture the on-screen waveform into ``self.waveform``.

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
                calibrated_volts_per_code=self._calibrated_volts_per_code)
            if not self.waveform.channels:
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
        for entry in self.waveform.channels:
            name = str(entry.get("name", "CH1")).upper()
            channel_number = name[2:] if name[:2].upper() == "CH" else name
            measurements.update(self.get_measurements_numeric(channel_number))
        calibrated = self.waveform.calibrate_from_measurements(
            measurements, channel=channel, pin_extremes=pin_extremes)
        if calibrated:
            for entry in self.waveform.channels:
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
        for item in self.waveform.channels or []:
            if str(item.get("name") or "").upper().endswith(str(channel)):
                entry = item
                break
        if entry is None and self.waveform.channels:
            entry = self.waveform.channels[0]
        if not entry:
            return {}

        data = [value for value in entry.get("waveform") or [] if value is not None]
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
        if not self.waveform.channels:
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
    #: It was a class attribute before the split, and stays one: a subclass or a
    #: stub that never runs ``__init__`` still reads and writes it.
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

    #: The depth ladder the flat class ended up exposing, because the HDS block
    #: redefined it lower in the same class body.  ``SdsDialect`` has one under the
    #: same name and ``HdsDialect`` has another; this is the HDS271's, which is
    #: what a caller of this class has always seen.
    MEMORY_DEPTHS = HdsDialect.MEMORY_DEPTHS

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
