import json
import logging
import re
import struct
from datetime import datetime

import numpy as np

# --- HDS200 / HDS300 capture geometry -------------------------------------
# Verified on an HDS271 (firmware V1.3.0) against the instrument's own
# measurements: a 1 kHz signal on a 500us/div timebase spans 6 ms of capture
# holding 6 complete cycles, so the screen is 12 divisions wide (6 ms /
# 500us). OWON's own PC-software help document also describes 12 voltage
# scales. The vertical axis is 256 (8-bit) sample codes tall; see
# HDS_CODES_PER_DIVISION for how many of them make a division.
#: Horizontal divisions on the graticule. Confirmed on hardware: a 1 kHz square
#: wave captured at 500us/div spans exactly 6 ms of data (6 cycles over 12
#: divisions), and the cycle count tracks the timebase exactly.
HDS_HORIZONTAL_DIVISIONS = 12.0

#: Sample codes per division: the whole 0..255 code range covers the 8 divisions
#: the capture is drawn over, so a division is 256 / 8 = 32 codes. Geometry, not a
#: fit - it is what turns a decoded volts-per-code into a volts-per-division, i.e.
#: what a row of the grid is worth.
#:
#: The volts per CODE is the part that has to be calibrated, not this. It comes
#: from the instrument's own readings (see calibrate_from_measurements), because
#: the volts/div that :CHANn:SCALe? reports is cosmetic on this firmware: measured
#: on one unchanged signal, a 10x change of it (500mV/div -> 5V/div) left the volts
#: per code at ~0.145 V while the code span tracked the signal (4.5 -> 25.6 Vpp),
#: and the instrument's own Vpp followed the real gain and not the label.
#:
#: History, because three models were published here before this one. 26.0 was
#: fitted by scoring candidates on RMS against the instrument's own readings, so it
#: agreed with them by construction and read a 5.00 Vpp signal as 6.5 V. 34.5
#: anchored on a signal of known amplitude while treating the codes as a fixed
#: input scale. 32.0 as "screen codes, decode from the live volts/div" then followed
#: from reading identical codes across volts/div writes as proof that the writes are
#: inert - which they may be, since the label moving without the gain moving gives
#: the same observation - and it made the grid depend on that label, which put a
#: measured 25.6 Vpp signal at 275 V. The evidence that separated them is the pair
#: of captures above: the label moved 10x and the volts per code did not move at all.
#:
#: Consequence worth stating: the instrument's readings and the grid are right
#: while the calibration is in hand, and the label is only ever shown as the
#: instrument's claim.
HDS_CODES_PER_DIVISION = 32.0

#: The volts one sample code is worth, measured on this instrument at the reference
#: setting: a 5.00 Vpp signal spanned 34.5 codes -> 0.1449 V per code in the frame
#: the instrument itself reads in (it reported that signal as 5.000 V through a 10X
#: probe). This is the FALLBACK, used only until a calibration lands; the app
#: calibrates against the instrument's own readings on every capture
#: (calibrate_from_measurements with pin_extremes=True). It exists because the
#: alternative fallback - the volts/div the instrument reports - is cosmetic on this
#: firmware and read the same 25.6 Vpp signal as 235.9 V.
HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X = 0.1449

def _manual_trim():
    """The hand-set calibration factor, as a float, 1.0 when unset."""
    if not HDS_MANUAL_CALIBRATION_TRIM:
        return 1.0
    try:
        return float(HDS_MANUAL_CALIBRATION_TRIM)
    except (TypeError, ValueError):
        logging.warning("HDS_MANUAL_CALIBRATION_TRIM is not a number (%r); ignoring it",
                        HDS_MANUAL_CALIBRATION_TRIM)
        return 1.0


#: ===========================================================================
#: MANUAL CALIBRATION - the one line to change by hand.
#: ===========================================================================
#:
#: A factor applied to whatever scale the decode arrived at, automatic calibration
#: included. ``None`` (or 1.0) leaves it alone.
#:
#: Why a factor and not a volts-per-code. A fixed volts-per-code looks like the
#: more direct statement, but the code span the instrument returns for one signal
#: moves by a few percent from capture to capture (144, 148, 151 codes for the same
#: 25 Vpp square wave), so a fixed value inherits that movement and wanders: derived
#: from one capture it read 26.4 Vpp on the next. A factor is applied to a scale
#: measured on the SAME capture, so the span cancels and the reading stays put - and
#: it still says what it should: "the instrument reads a few percent high, correct
#: it".
#:
#: HOW TO GET THE NUMBER
#:
#:   1. Put a signal on the input whose amplitude you trust.
#:   2. Let the panel capture it, and read the amplitude it shows.
#:   3. trim = true amplitude / amplitude shown.
#:
#:      Worked example from this bench: the generator set to 25.0 Vpp, the app
#:      showing 25.80 Vpp, so trim = 25.0 / 25.80 = 0.968992. At that value the
#:      grid draws the signal as 5.00 divisions of 5 V/div, which is what 25.0 V
#:      over 5 V/div is.
#:
#:   Or let the tool do it from a live capture:
#:
#:          .venv\Scripts\python.exe calibrate_manual.py 25.0
#:
#: WHAT THIS TRADES. Untrimmed, the app agrees with the INSTRUMENT: the scale is
#: pinned to the instrument's own reading, so it reads 25.6-25.8 Vpp for this
#: generator's 25.0. A trim says the instrument is the one that is off and the
#: generator is the reference. Re-derive it if the probe or the instrument's
#: volts/div changes, since it corrects a gain, not a fixed offset.
#: SET FOR THIS BENCH. Derived with ``calibrate_manual.py 25.0`` - the generator's
#: 25 Vpp square wave read 25.80 Vpp, so the factor is 25.0/25.80. Verified live at
#: this value: 25.000 Vpp and 24.806 Vpp on consecutive captures, drawing 4.96 and
#: 5.00 divisions of 5 V/div. Back to None lets the app agree with the instrument
#: again; re-derive if the probe or the instrument's volts/div changes. The test
#: suite neutralises this (tests/__init__.py), so a number here is fine to keep.
HDS_MANUAL_CALIBRATION_TRIM = 0.968992

#: Vertical divisions on the graticule. The screen is 12 x 8, as the instrument
#: draws it, so the visible window is 8 rows of the selected volts/div.
HDS_VERTICAL_DIVISIONS = 8.0

#: Divisions the whole 0..255 sample-code range spans. It equals the screen height
#: because the codes are screen positions; kept as its own constant because a code
#: range and a screen height are different statements that happen to coincide here.
HDS_CODE_RANGE_DIVISIONS = 256.0 / HDS_CODES_PER_DIVISION

#: Sample code that sits at the vertical centre (zero volts at offset 0).
HDS_SAMPLE_MIDPOINT = 128


def _calibrate_codes(codes, anchors):
    """Least-squares slope/intercept mapping sample codes onto volts.

    ``anchors`` is a sequence of (code, volts) pairs. Two are enough for a
    straight line; more are fitted and the residual reported.
    Returns (slope, offset, max_residual) or None when it cannot be fitted.
    """
    pairs = [(c, v) for c, v in anchors if c is not None and v is not None]
    if len(pairs) < 2:
        return None
    xs = np.asarray([c for c, _ in pairs], dtype=float)
    ys = np.asarray([v for _, v in pairs], dtype=float)
    if np.ptp(xs) == 0:
        return None
    slope, offset = np.polyfit(xs, ys, 1)
    residual = float(np.max(np.abs(slope * xs + offset - ys)))
    return float(slope), float(offset), residual


def _ci(source, key, default=None):
    """Case-insensitive lookup in a nested header dict.

    The instrument answers :DATA:WAVE:SCREen:HEAD? with UPPERCASE JSON keys
    ("TIMEBASE", "SAMPLE", "CHANNEL"), while OWON's published SCPI manual shows
    the same document in lowercase. Both spellings have to work.
    """
    if not isinstance(source, dict):
        return default
    wanted = str(key).lower()
    for name, value in source.items():
        if str(name).lower() == wanted:
            return value
    return default


class WaveformData:
    """Parse and store waveform data from oscilloscope captures."""

    def __init__(self):
        self.header = ""
        self.file_length = 0
        self.channels = []
        self.raw_data = b""
        # HDS capture context, filled in by parse_hds_capture().
        self.timebase_scale = None
        self.timebase_offset = 0
        self.model = ""
        self.datatype = ""
        self.run_status = ""
        self.sample_rate = ""

    @staticmethod
    def _scale_to_float(value):
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip().lower()
        # The exponent must be part of the number, not treated as a unit: the
        # instrument answers ":CH1:SCALe?" with "5e-01" and its measurements with
        # values like "1.0000e+09", and a regex that stops at the digit silently
        # fails those, which collapses the volts axis onto raw sample codes.
        match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)(?:\s*([a-z/]+))?", text)
        if not match:
            return None
        number = float(match.group(1))
        unit = (match.group(2) or "").strip()
        multipliers = {
            "ns": 1e-9,
            "us": 1e-6,
            "µs": 1e-6,
            "ms": 1e-3,
            "s": 1.0,
            "mv": 1e-3,
            "v": 1.0,
        }
        return number * multipliers.get(unit, 1.0)

    @staticmethod
    def _to_float(value):
        """Numeric coercion for header fields that may be text or numbers."""
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return WaveformData._scale_to_float(value)

    @staticmethod
    def _probe_factor(text):
        """'10X' -> 10.0, '1x' -> 1.0, missing -> 1.0."""
        if text in (None, ""):
            return 1.0
        match = re.search(r"(\d+(?:\.\d+)?)\s*[xX]", str(text))
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return 1.0
        value = WaveformData._scale_to_float(text)
        return value if value else 1.0

    def parse_bin_data(self, data, file_length):
        """Parse legacy OWON waveform data."""
        try:
            logging.debug("Starting parse_bin_data, data length: %s", len(data))
            self.raw_data = data
            self.header = data[:6].decode("ascii", errors="ignore")
            logging.info("Waveform header: %s", self.header)
            self.file_length = file_length
            self.channels = []
            pos = 54

            while pos < len(data):
                if pos + 3 > len(data):
                    break
                channel_name = data[pos:pos + 3].decode("ascii", errors="ignore")
                if not channel_name.startswith("CH"):
                    break
                pos += 3

                if pos + 4 > len(data):
                    break
                block_length = struct.unpack("<i", data[pos:pos + 4])[0]
                normal_wave = block_length >= 0
                block_length = abs(block_length)
                pos += 4

                if normal_wave:
                    channel_data = self._parse_legacy_channel(data, pos, channel_name, block_length, point_width=1)
                    if channel_data:
                        self.channels.append(channel_data)
                    pos += block_length
                else:
                    pos += 8
                    actual_block_length = block_length + 8
                    channel_data = self._parse_legacy_channel(data, pos, channel_name, actual_block_length, point_width=1)
                    if channel_data:
                        self.channels.append(channel_data)
                    pos += actual_block_length - 20
        except Exception as exc:
            logging.error("Error parsing legacy waveform data: %s", exc)

    def _parse_legacy_channel(self, data, pos, channel_name, block_length, point_width=1):
        try:
            channel = {
                "name": channel_name,
                "whole_screen_points": 0,
                "num_points": 0,
                "slow_move": 0,
                "timebase_index": 0,
                "zero_point": 0,
                "voltage_index": 0,
                "attenuation": 1,
                "point_interval": 0.0,
                "frequency": 0,
                "cycle": 0,
                "voltage_per_point": 0.0,
                "waveform_data": [],
            }

            if pos + 44 <= len(data):
                channel["whole_screen_points"] = struct.unpack("<i", data[pos:pos + 4])[0]
                channel["num_points"] = struct.unpack("<i", data[pos + 4:pos + 8])[0]
                channel["slow_move"] = struct.unpack("<i", data[pos + 8:pos + 12])[0]
                channel["timebase_index"] = struct.unpack("<i", data[pos + 12:pos + 16])[0]
                channel["zero_point"] = struct.unpack("<i", data[pos + 16:pos + 20])[0]
                channel["voltage_index"] = struct.unpack("<i", data[pos + 20:pos + 24])[0]
                channel["attenuation"] = struct.unpack("<i", data[pos + 24:pos + 28])[0]
                channel["point_interval"] = struct.unpack("<f", data[pos + 28:pos + 32])[0]
                channel["frequency"] = struct.unpack("<i", data[pos + 32:pos + 36])[0]
                channel["cycle"] = struct.unpack("<i", data[pos + 36:pos + 40])[0]
                channel["voltage_per_point"] = struct.unpack("<f", data[pos + 40:pos + 44])[0]

                data_start = pos + 44
                data_end = data_start + (channel["num_points"] * point_width)
                if data_end <= len(data):
                    waveform = []
                    for i in range(channel["num_points"]):
                        data_pos = data_start + (i * point_width)
                        value = struct.unpack("<b", data[data_pos:data_pos + point_width])[0]
                        waveform.append(value)
                    channel["waveform_data"] = waveform
                    logging.info("Channel %s: %s points parsed", channel_name, len(waveform))
                return channel
        except Exception as exc:
            logging.error("Error parsing channel %s: %s", channel_name, exc)
        return None

    def parse_hds_capture(self, header, channel_payloads, calibrated_volts_per_code=None):
        """Parse an HDS200/HDS300 HEAD? document plus channel payloads.

        ``header`` is the JSON object from :DATA:WAVE:SCREen:HEAD? and
        ``channel_payloads`` maps channel names ("CH1") to the bytes returned by
        :DATA:WAVE:SCREen:CH<n>?.  Both the uppercase keys this firmware emits
        and the lowercase keys of OWON's published manual are accepted.

        ``calibrated_volts_per_code`` is the absolute volts-per-code measured
        against the instrument's own readings (an earlier capture calibrated with
        :meth:`calibrate_from_measurements`).  Pass it whenever it is known: the
        volts/div in the header is a label this firmware does not keep in step
        with the gain, so decoding from it puts the signal out by that ratio.

        Each channel dict keeps the keys the GUI already plots with
        ("waveform_data" in volts, "point_interval" in seconds per point) and
        adds the raw 8-bit codes for callers that want them.
        """
        self.raw_data = b""
        self.file_length = sum(len(payload) for payload in channel_payloads.values())
        self.header = json.dumps(header, ensure_ascii=False, sort_keys=True)

        timebase = _ci(header, "timebase", {})
        self.timebase_scale = self._scale_to_float(_ci(timebase, "scale"))
        self.timebase_offset = _ci(timebase, "hoffset", 0)
        sample = _ci(header, "sample", {})
        self.model = _ci(header, "model", "")
        self.datatype = _ci(header, "datatype", "")
        self.run_status = _ci(header, "runstatus", "")
        self.sample_rate = _ci(sample, "samplerate", "")

        self.channels = []
        for channel_name, payload in channel_payloads.items():
            channel = self._parse_hds_channel(header, channel_name, payload,
                                              calibrated_volts_per_code)
            if channel:
                self.channels.append(channel)
        return self.channels

    @staticmethod
    def _strip_declared_length(payload):
        """Drop a length prefix, whether it is ``#<n><len>`` or 4-byte little-endian.

        The HDS ``:DATA`` commands prefix every reply with a 4-byte
        little-endian length ("there are 4 bytes in the returned data to
        indicate the size of the returned data" per the HDS200 manual); the
        transport strips it, but callers may hand over the raw exchange.
        """
        if len(payload) >= 2 and payload[:1] == b"#" and payload[1:2].isdigit():
            digits = int(payload[1:2].decode("ascii"))
            if len(payload) >= 2 + digits:
                size = int(payload[2:2 + digits].decode("ascii"))
                return payload[2 + digits:2 + digits + size]
        if len(payload) >= 4:
            declared = struct.unpack("<I", payload[:4])[0]
            if 0 < declared <= len(payload) - 4:
                return payload[4:4 + declared]
        return payload

    # Kept under the original name for callers that used it.
    _strip_definite_length = _strip_declared_length

    @staticmethod
    def _hds_codes(payload):
        """Decode a channel payload into 8-bit sample codes.

        The instrument stores each 8-bit sample in the HIGH byte of a
        little-endian 16-bit word; the low byte carries dither (adjacent codes
        differ by about 1 while quantisation steps are exactly 256), so taking
        the whole word would invent resolution the hardware does not have.
        Verified on an HDS271: the decoded codes step by 256, and the high byte
        alone reproduces the instrument's own MAX reading to 0.1%.
        """
        payload = WaveformData._strip_declared_length(payload)
        usable = len(payload) - (len(payload) % 2)
        if usable < 2:
            return []
        words = np.frombuffer(payload[:usable], dtype="<u2")
        return (words >> 8).astype(np.int32).tolist()

    @staticmethod
    def unwrap_codes(codes):
        """Turn wrapped 8-bit screen codes into a continuous series.

        The payload carries each sample as one byte, and the acquisition's own offset
        can sit the signal across the 0/255 boundary. A 2.5 Vpp 1 kHz sine arrives as
        ..., 12, 4, 252, 245, ... - a step of -8 codes, not -248. Read as raw codes
        that is a signal jumping the full range twice per period: the trace is drawn
        with a vertical break through the middle of every cycle, and the span the
        calibration is fitted to is the 248-code wrap rather than the signal, so a
        2.60 V input is drawn and measured as 3.52 V.

        Adjacent samples of anything this instrument can put on screen move far less
        than half the range - 50 samples to a 1 kHz cycle at 20 us a point is about
        8 codes a step on a full-screen signal - so the smallest step is the right
        branch. A step at or beyond half the range is genuinely ambiguous at this
        width, and such a sample is left where it comes.
        """
        unwrapped, offset, previous = [], 0, None
        for code in codes:
            if previous is not None:
                step = ((code - previous + 128) % 256) - 128
                if abs(step) < 128:          # half the range or more is not a step to guess at
                    offset += step - (code - previous)
            previous = code
            unwrapped.append(code + offset)
        return unwrapped

    def _parse_hds_channel(self, header, channel_name, payload,
                           calibrated_volts_per_code=None):
        # Wrapped codes are made continuous before anything else reads them: the
        # volts, the calibration's extremes and the plotted trace all depend on
        # samples being in order, and a signal across the 0/255 boundary is not.
        codes = self.unwrap_codes(self._hds_codes(payload))
        if not codes:
            return None

        ch_header = {}
        for item in _ci(header, "channel", []) or []:
            if str(_ci(item, "name", "")).strip().lower() == channel_name.lower():
                ch_header = item
                break

        scale_per_div = self._scale_to_float(_ci(ch_header, "scale"))
        probe = self._probe_factor(_ci(ch_header, "probe"))
        offset_div = self._to_float(_ci(ch_header, "offset", 0)) or 0.0

        # Volts per sample code.  The codes are the acquisition, referred to the
        # connector: the same signal returns proportionally more codes at a finer
        # volts/div, and the volts per code does NOT follow the volts/div LABEL.
        # Measured on this unit: moving the label from 500mV/div to 5V/div (10x)
        # left the volts per code at ~0.145 V, while the trace's span tracked the
        # signal itself (4.5 Vpp -> 25.6 Vpp).  On this firmware the label is
        # cosmetic, and the instrument's own readings come from the real gain - it
        # called that 25.6 Vpp signal 25.6 V while its own label claimed 50 V/div.
        #
        # So a code cannot be turned into volts from the label.  The mapping comes
        # from, in order of preference:
        #   1. a calibration against the instrument's own readings (see
        #      calibrate_from_measurements with pin_extremes=True), remembered
        #      across captures - the only path that stays right whatever the label
        #      says, and the one the app uses;
        #   2. the bench reference (0.1449 V per code at 10X), whose error is the
        #      spread of the gain between bench sittings - tens of percent at worst -
        #      and which is replaced within one capture anyway.
        # The reported label is NOT in this list: it never becomes volts. It is
        # carried in the entry as the instrument's claim, so a disagreement can be
        # shown to the user, and that is all it is used for.
        volts_per_div = scale_per_div * probe if scale_per_div else None
        if calibrated_volts_per_code:
            volts_per_code = float(calibrated_volts_per_code)
            source = "instrument measurements"
        else:
            # Uncalibrated: the measured bench reference, in the frame the
            # instrument's own readings use. Scaled by the probe it ANNOUNCES - the
            # reference is a tip figure at 10X - and left unscaled when it announces
            # none, since a missing probe is not the same thing as a 1X one.
            announced = _ci(ch_header, "probe")
            volts_per_code = HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X
            if announced:
                volts_per_code *= probe / 10.0
            source = "reference (uncalibrated)"
            logging.info(
                "HDS channel %s: no calibration yet; using the bench reference of "
                "%.4g V per code at 10X%s. The app calibrates on capture, so this "
                "is the first guess only.",
                channel_name, HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X,
                "" if announced else " (no probe announced, left unscaled)")

        # A hand-set trim, for the paths where nothing re-pins the scale afterwards.
        # A CALIBRATED scale is not trimmed here: the calibration trims the
        # instrument's readings instead, and doing it in both places would apply the
        # factor twice to anything remembered across captures.
        if source.startswith("reference"):
            trim = _manual_trim()
            if trim != 1.0:
                volts_per_code *= trim
                source = "%s, trimmed x%.6g" % (source, trim)

        # The zero of the input.  The position control is a display shift: writing
        # one division of it moved the captured codes by 0, so the acquisition's
        # zero is the sample midpoint, and a calibration refines it from the
        # instrument's average.
        zero_code = HDS_SAMPLE_MIDPOINT
        volts = [round((code - zero_code) * volts_per_code, 6) for code in codes]

        # Time axis: the capture spans the model's full horizontal width, and
        # the samples tile it evenly.
        point_interval = 1.0
        if self.timebase_scale:
            point_interval = (self.timebase_scale * HDS_HORIZONTAL_DIVISIONS) / max(len(codes), 1)

        return {
            "name": channel_name.upper(),
            "whole_screen_points": len(codes),
            "num_points": len(codes),
            "slow_move": int(self._to_float(_ci(_ci(header, "sample", {}), "slowmove", 0)) or 0),
            "timebase_index": 0,
            "zero_point": zero_code,
            "voltage_index": 0,
            "attenuation": _ci(ch_header, "probe", "") if ch_header else "",
            "point_interval": point_interval,
            "frequency": self._to_float(_ci(ch_header, "frequence", 0)) or 0,
            "cycle": 0,
            # Volts per code at the tip: what a row of the grid is worth once the
            # selected volts/div is divided into the 32 codes a division holds.
            "voltage_per_point": volts_per_code,
            # The GUI plots these two directly.
            "waveform_data": volts,
            "raw_codes": codes,
            "units": "V",
            # What the instrument CLAIMS its vertical scale is. Cosmetic on this
            # firmware: it moves without the gain moving, so it is kept for
            # display and comparison, not for measuring.
            # The instrument's claim, as it states it: a volts/div at the input.
            # Deliberately NOT multiplied by the probe it announces - on this bench it
            # announces 10X for a 1X probe, which turned a truthful "5v" into a
            # fictional "50 V/div" and made the plot report a disagreement that was
            # entirely the panel's own doing.
            "volts_per_div": scale_per_div,
            # What a row of the grid is actually worth: the decoded volts per
            # code times the codes a division is drawn across. This is what the
            # grid is labelled with, and it stays right when the label does not.
            "true_volts_per_div": (volts_per_code * HDS_CODES_PER_DIVISION
                                   if volts_per_code else None),
            "volts_per_code_source": source,
            "coupling": _ci(ch_header, "coupling", ""),
            "display": _ci(ch_header, "display", ""),
            "vertical_offset_div": offset_div,
        }

    # ------------------------------------------------------------------
    # Vertical calibration
    def calibrate_from_measurements(self, measurements, channel=None, pin_extremes=False):
        """Refine the sample-code -> volts mapping from the instrument's readings.

        ``measurements`` is a mapping of measurement label to volts as returned
        by ``OWONScopeController.get_all_measurements()`` for one channel: the
        labels "Vmin", "Vmax", "Vpp", "Vmean" are recognised.

        By default only the ZERO point is refined, using the instrument's
        average reading, and the slope stays at the decoded absolute value
        (``voltage_per_point``). That keeps the waveform shape and its RMS
        correct, which is the property verified on hardware: refining the zero
        reproduces the instrument's RMS to about 0.06 V.

        ``pin_extremes=True`` instead forces the decoded extremes onto the
        instrument's MIN/MAX/PKPK so the plotted peak-to-peak matches the
        readout exactly. That distorts the interior of the trace whenever the
        screen undersamples the record's peaks, so it is opt-in and the result
        is recorded with ``method`` set accordingly.

        What the instrument's readings mean on this hardware, measured rather than
        assumed: they are real volts at the BNC. With a 1X probe fitted it announced
        10X and still read a 25 Vpp signal as 25.6 V, and its own volts/div LABEL
        drifts from its gain (a volts/div written over SCPI is accepted without the
        gain following - writing 10 V/div left the code span at 209 codes where a
        real change would have halved it). So do not fold either label into the
        readings: pinning to them is the right thing to do, and it is what makes the
        app agree with the instrument. If you would rather the app agreed with the
        SIGNAL instead - the generator is the reference and the scope is the one that
        is off - set HDS_MANUAL_CALIBRATION_TRIM and this calibration is scaled by it.

        Returns True when at least one channel was calibrated.
        """
        labels = {
            "min": ("vmin", "min"),
            "max": ("vmax", "max"),
            "pkpk": ("vpp", "pkpk"),
            "mean": ("vmean", "average", "vavg"),
        }

        def pick(names):
            """The instrument's reading for the first name that has one.

            Some readings come back range-qualified (">4.4600e+00"), which is a
            statement about the instrument's range rather than a value. The
            qualifier is stripped rather than skipping the reading: a skipped
            reading silently drops the calibration, and the decode then falls back
            to the bench reference, which is only a first guess - so the trace keeps
            its shape but the volts drift by tens of percent.
            """
            for name in names:
                for key, value in (measurements or {}).items():
                    if str(key).strip().lower() == name:
                        try:
                            text = str(value).strip().lstrip("<>=~≈ ")
                            reading = float(text)
                        except (TypeError, ValueError):
                            return None
                        # The hand-set trim is applied to the READINGS, not to the
                        # slope this method computes: pinning the extremes recomputes
                        # the slope from these numbers, so a trim applied downstream
                        # would be cancelled out by its own calibration.
                        return reading * _manual_trim()
            return None

        wanted = {group: pick(names) for group, names in labels.items()}
        vmin, vmax, vpkpk, vmean = wanted["min"], wanted["max"], wanted["pkpk"], wanted["mean"]

        calibrated = False
        for entry in self.channels:
            if channel is not None and str(entry.get("name", "")).upper() != str(channel).upper():
                continue
            codes = entry.get("raw_codes") or []
            if not codes:
                continue
            lo, hi = min(codes), max(codes)
            # The header's volts/div is only a starting point for the slope, and the
            # first capture of a session can arrive before the header is complete. It
            # must not block the pinning, which recomputes the slope from the anchors
            # and needs neither: requiring it here silently skipped the calibration on
            # that first capture, and the panel then drew on the bench reference - a
            # 2.6 V sine at 1.87 V - until a later capture happened to carry it.
            volts_per_div = entry.get("volts_per_div")
            slope = entry.get("voltage_per_point") or (
                (volts_per_div / HDS_CODES_PER_DIVISION) if volts_per_div else None)
            mean_code = sum(codes) / len(codes)
            anchors = []
            method = "scale + zero from instrument average"

            if pin_extremes:
                top = vmin + vpkpk if (vpkpk is not None and vmin is not None) else vmax
                anchors = [(lo, vmin)] if vmin is not None else []
                if top is not None:
                    anchors.append((hi, top))
                fit = _calibrate_codes(codes, anchors)
                if not fit:
                    continue
                slope, offset, residual = fit
                method = "extremes pinned to instrument MIN/MAX/PKPK"
            else:
                if vmean is None or not slope:
                    continue
                # volts(code) = (code - zero) * slope, with the mean landing on
                # the instrument's average: zero = mean_code - Vmean / slope.
                zero_code = mean_code - (vmean / slope)
                offset = -zero_code * slope
                residual = 0.0
                anchors = [(mean_code, vmean)]

            volts = [round(slope * code + offset, 6) for code in codes]
            entry["waveform_data"] = volts
            entry["voltage_per_point"] = slope
            entry["zero_point"] = (-offset / slope) if slope else entry.get("zero_point")
            entry["units"] = "V"
            entry["calibration"] = {
                "method": method,
                "volts_per_code": slope,
                "offset_volts": offset,
                "codes_per_division": (volts_per_div / slope) if (volts_per_div and slope) else None,
                "anchors": anchors,
                "residual_volts": residual,
                "source": "instrument measurements",
            }
            # Recorded for the caller's judgement; not all of these were used.
            checks = {}
            if vmean is not None:
                checks["vmean"] = {"scope": vmean, "decoded": sum(volts) / len(volts)}
            if vpkpk is not None:
                checks["vpp"] = {"scope": vpkpk, "decoded": max(volts) - min(volts)}
            if vmax is not None:
                checks["vmax"] = {"scope": vmax, "decoded": max(volts)}
            if vmin is not None:
                checks["vmin"] = {"scope": vmin, "decoded": min(volts)}
            entry["calibration"]["checks"] = checks
            calibrated = True
        return calibrated
