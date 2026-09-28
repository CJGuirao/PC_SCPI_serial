import struct
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modernlab.instrument.capture import waveform

from owon_controller import OWONScopeController
from modernlab.instrument.capture.waveform import (WaveformData, HDS_CODES_PER_DIVISION,
                            HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X)


def hds_header(scale, probe="10x", offset=0):
    """A minimal HDS capture header holding one channel."""
    return {
        "timebase": {"scale": "200us", "hoffset": 0},
        "sample": {"fullscreen": 4, "slowmove": -1, "datalen": 4,
                   "samplerate": "(500ks/s)", "type": "sample", "depmem": "4k"},
        "channel": [{"name": "ch1", "display": "on", "coupling": "dc",
                     "probe": probe, "scale": scale, "offset": offset,
                     "frequence": 0, "inverse": "off"}],
        "datatype": "screen",
        "runstatus": "auto",
        "trig": {"mode": "single", "type": "edge",
                 "items": {"channel": "ch1", "level": "32.0mv", "edge": "rise",
                           "coupling": "dc", "holdoff": "100ns"},
                 "sweep": "auto"},
    }


def hds_payload(codes):
    """Pack sample codes the way the instrument sends them.

    Each 8-bit sample rides in the high byte of a little-endian 16-bit word.
    """
    return struct.pack("<%dH" % len(codes), *[code << 8 for code in codes])


class FramingWatchTests(unittest.TestCase):
    """The live loop must see a framing change made on the instrument.

    The instrument has no volts/div knob: the CH1/CH2 key with the direction keys
    sets it, on the same cluster that otherwise moves the time base. So the
    volts/div can change with no write from this side at all, and the hours of
    "the volts/div control does nothing, the time base works" are what waiting for
    the 254 ms header looks like when the time base label is read every frame.
    """

    def scope_with(self, scale, probe="10X", timebase="1ms"):
        """A controller whose three framing reads give these values."""
        scope = OWONScopeController(family="hds")
        scope.get_channel_scale = lambda channel: scale
        scope.get_channel_probe = lambda channel: probe
        scope.get_timebase_scale = lambda: timebase
        return scope

    def test_the_signature_reads_the_three_framing_values(self):
        self.assertEqual(("500mv", "10X", "1ms"), self.scope_with("500mv").framing_signature())

    def test_a_changed_volts_div_shows_in_the_signature(self):
        self.assertNotEqual(self.scope_with("500mv").framing_signature(),
                            self.scope_with("5v").framing_signature())

    def test_a_changed_probe_or_timebase_shows_too(self):
        base = self.scope_with("500mv").framing_signature()
        self.assertNotEqual(base, self.scope_with("500mv", probe="1X").framing_signature())
        self.assertNotEqual(base, self.scope_with("500mv", timebase="2ms").framing_signature())

    def test_a_failed_read_is_a_difference_not_a_silence(self):
        # A dropped reply must re-read the header, not look like "nothing changed".
        scope = self.scope_with("500mv")

        def explode(channel):
            raise OSError("USB hiccup")

        scope.get_channel_probe = explode
        self.assertEqual(("500mv", None, "1ms"), scope.framing_signature())

    def test_the_cached_header_can_be_dropped(self):
        scope = OWONScopeController(family="hds")
        scope._cached_header = {"channel": []}
        scope.invalidate_capture_header()
        self.assertIsNone(scope._cached_header)


class HdsVerticalDecodeTests(unittest.TestCase):
    """Volts come from the codes plus a scale, and the scale never comes from a label.

    Measured on this unit. A volts/div written over SCPI does not reach the gain:
    with a 25 Vpp signal on screen, writing 10 V/div left the code span at 209 codes
    where a real change would have halved it, and writing 2 V/div left it at 121
    where a real change would have saturated it - while the instrument's own Vpp
    fell from 25.6 V to 17.76 V on an input that never moved.

    The instrument's READINGS, on the other hand, are real volts at the BNC: with a
    1X probe fitted it announced 10X and still read that 25 Vpp signal as 25.6 V. So
    the scale comes from those readings (or from the bench reference until the first
    calibration, or from HDS_MANUAL_VOLTS_PER_CODE when the generator is the
    reference), and never from a label.
    """

    codes = [113, 131, 149]
    payload = hds_payload(codes)
    #: A full set, which is what pin_extremes needs: min and max as well as pkpk.
    readings = {"Vmin": "-5.0", "Vmax": "5.0", "Vpp": "10.0"}

    def decode(self, scale, probe="10x", offset=0, calibrated=None):
        waveform = WaveformData()
        waveform.parse_hds_capture(hds_header(scale, probe=probe, offset=offset),
                                   {"CH1": self.payload},
                                   calibrated_volts_per_code=calibrated)
        return waveform.channels[0]

    def test_the_label_does_not_set_the_scale(self):
        # With a calibration in hand the same codes decode to the same volts
        # whatever the label says - which is what makes the reading survive a
        # label that has drifted from the gain (measured: a 10x label change left
        # the volts per code at ~0.145 V).
        fine = self.decode("500mv", calibrated=0.1455)
        coarse = self.decode("5v", calibrated=0.1455)
        self.assertNotEqual(fine["volts_per_div"], coarse["volts_per_div"])
        for a, b in zip(fine["waveform"], coarse["waveform"]):
            self.assertAlmostEqual(a, b, places=9)
        self.assertAlmostEqual(fine["true_volts_per_div"], coarse["true_volts_per_div"],
                               places=9)

    def test_a_calibration_sets_the_volts(self):
        entry = self.decode("5v", calibrated=0.1455)
        self.assertAlmostEqual(0.1455, entry["voltage_per_point"], places=9)
        self.assertAlmostEqual((self.codes[0] - 128) * 0.1455,
                               entry["waveform"][0], places=6)
        self.assertEqual("instrument measurements", entry["volts_per_code_source"])

    def test_the_true_division_is_the_decoded_scale(self):
        # This is what the grid's rows are worth: volts per code x codes/division.
        entry = self.decode("500mv", calibrated=0.1455)
        self.assertAlmostEqual(0.1455 * HDS_CODES_PER_DIVISION,
                               entry["true_volts_per_div"], places=9)

    def test_a_known_signal_decodes_to_its_amplitude(self):
        # The hardware case: 57..233 codes was a 25.6 Vpp signal. The middle sample
        # keeps every step inside the range the codes can resolve - a bare [57, 233]
        # is two extremes 176 codes apart, which is not a step any real pair of
        # adjacent samples can take and is left alone by unwrap_codes.
        waveform = WaveformData()
        waveform.parse_hds_capture(hds_header("5v", probe="10x"),
                                   {"CH1": hds_payload([57, 145, 233])},
                                   calibrated_volts_per_code=25.6 / 176.0)
        entry = waveform.channels[0]
        volts = entry["waveform"]
        self.assertAlmostEqual(25.6, max(volts) - min(volts), places=2)
        # And the grid reading - rows x volts per row - gives the same number.
        rows = (max(volts) - min(volts)) / entry["true_volts_per_div"]
        self.assertAlmostEqual(25.6, rows * entry["true_volts_per_div"], places=6)

    def test_without_a_calibration_the_bench_reference_is_used_and_named(self):
        # A fresh process has no calibration yet. The fallback is the measured bench
        # reference, not the reported label: on this firmware the label moves without
        # the gain moving, and using it read a 25.6 Vpp signal as 235.9 V. The
        # reference is scaled by the announced probe, which is the frame the
        # instrument's own readings use.
        entry = self.decode("500mv")
        self.assertEqual("reference (uncalibrated)", entry["volts_per_code_source"])
        self.assertAlmostEqual(HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X,
                               entry["voltage_per_point"], places=9)
        # ...and the label is reported as the instrument states it, a volts/div at
        # the input: "500mv" is 0.5 V/div, NOT 5 V/div, because it is not multiplied
        # by the probe the instrument announces (it announced 10X for a 1X probe on
        # this bench, which turned a truthful "5v" into a fictional "50 V/div").
        self.assertEqual(0.5, entry["volts_per_div"])

    def test_the_reference_follows_the_announced_probe(self):
        through_1x = self.decode("500mv", probe="1x")
        self.assertAlmostEqual(HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X / 10.0,
                               through_1x["voltage_per_point"], places=9)

    def test_a_missing_probe_leaves_the_reference_unscaled(self):
        # A missing probe is not a 1X probe: the reference is a tip figure at 10X,
        # so scaling it by an absent probe would be off by ten.
        entry = self.decode("500mv", probe=None)
        self.assertEqual("reference (uncalibrated)", entry["volts_per_code_source"])
        self.assertAlmostEqual(HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X,
                               entry["voltage_per_point"], places=9)

    def test_a_manual_trim_scales_the_reference_decode(self):
        # With no calibration the trim lands on the decoded slope itself.
        with patch.object(waveform, "HDS_MANUAL_CALIBRATION_TRIM", 0.5):
            entry = self.decode("500mv")
            self.assertAlmostEqual(HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X * 0.5,
                                   entry["voltage_per_point"], places=9)
            self.assertIn("trimmed x0.5", entry["volts_per_code_source"])

    def test_a_manual_trim_scales_the_instruments_own_readings(self):
        # Where the calibration pins the scale to the instrument's readings, the trim
        # has to be applied to the READINGS: pinning recomputes the slope from them,
        # so a trim applied to the slope instead would be cancelled out by the very
        # calibration it was meant to correct. Measured: with the pin left untrimmed
        # the app read 25.6-25.8 Vpp for a generator set to 25.0, trim and all.
        # Named `plain`: `waveform` is the decode MODULE, which is what the trim below
        # patches. A local of the same name shadows it and the patch lands on the
        # instance instead, which quietly turns the test into a no-op.
        plain = WaveformData()
        plain.parse_hds_capture(hds_header("500mv"), {"CH1": self.payload})
        untrimmed = plain.channels[0]["voltage_per_point"]
        plain.calibrate_from_measurements(self.readings, pin_extremes=True)
        pinned_untrimmed = plain.channels[0]["voltage_per_point"]

        trimmed_waveform = WaveformData()
        trimmed_waveform.parse_hds_capture(hds_header("500mv"), {"CH1": self.payload})
        with patch.object(waveform, "HDS_MANUAL_CALIBRATION_TRIM", 0.5):
            self.assertTrue(trimmed_waveform.calibrate_from_measurements(
                self.readings, pin_extremes=True))
        pinned_trimmed = trimmed_waveform.channels[0]["voltage_per_point"]

        self.assertAlmostEqual(pinned_untrimmed / 2, pinned_trimmed, places=9)
        self.assertNotAlmostEqual(untrimmed, pinned_untrimmed, places=9)

    def test_the_label_never_becomes_volts(self):
        # The whole bug: a decode that took its volts from the reported volts/div
        # read a measured 25.6 Vpp signal as 235.9 V when the label was 10x out.
        # Whatever the label says, only the calibration or the reference gives volts.
        for scale in ("500mv", "5v", "50v"):
            entry = self.decode(scale)
            self.assertNotIn("label", entry["volts_per_code_source"])
            self.assertAlmostEqual(HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X,
                                   entry["voltage_per_point"], places=9)

    def test_the_position_label_does_not_move_the_decode(self):
        # The position is a display shift: writing one division of it over the
        # interface moved the captured codes by 0, so it cannot move volts.
        centred = self.decode("500mv", offset=0, probe="10x")["waveform"]
        shifted = self.decode("500mv", offset=1, probe="10x")["waveform"]
        for base, moved in zip(centred, shifted):
            self.assertAlmostEqual(base, moved, places=9)

    def test_a_calibrated_decode_ignores_the_probe_label(self):
        # A calibration is measured at the tip, so the probe label adds nothing -
        # and with the label as a fallback it still scales, because the label is
        # the connector figure the instrument claims.
        calibrated = self.decode("500mv", probe="1x", calibrated=0.1455)
        through_10x = self.decode("500mv", probe="10x", calibrated=0.1455)
        for a, b in zip(calibrated["waveform"], through_10x["waveform"]):
            self.assertAlmostEqual(a, b, places=9)
        uncalibrated_1x = self.decode("500mv", probe="1x")
        uncalibrated_10x = self.decode("500mv", probe="10x")
        self.assertAlmostEqual(10.0, uncalibrated_10x["waveform"][0] /
                               uncalibrated_1x["waveform"][0], places=4)

    def test_a_capture_without_a_scale_still_reports_its_source(self):
        entry = self.decode(None)
        self.assertIsNone(entry["volts_per_div"])
        self.assertEqual("V", entry["units"])


class WrappedCodeTests(unittest.TestCase):
    """A signal across the 0/255 boundary must be made continuous.

    Measured on this unit with a 2.5 Vpp 1 kHz sine: the screen payload carried
    ..., 12, 4, 252, 245, ... - a step of -8 codes, not -248 - so the raw codes are a
    sawtooth-shaped jump every time the signal passes the boundary. Read as absolute
    values that draws a vertical break through the middle of every cycle and fits the
    calibration to the 248-code wrap instead of the signal, which is how a 2.60 V
    input came to be drawn and measured as 3.52 V.
    """

    SINE_CYCLES, SINE_SAMPLES, SINE_AMPLITUDE = 6, 50, 65

    def wrapped_sine(self):
        """A sine centred on the code boundary, as it arrives off the instrument."""
        import math
        return [int(round(self.SINE_AMPLITUDE * math.sin(2 * math.pi * i / self.SINE_SAMPLES)))
                % 256 for i in range(self.SINE_CYCLES * self.SINE_SAMPLES)]

    def decode(self, codes, volts_per_code):
        waveform = WaveformData()
        waveform.parse_hds_capture(hds_header("500mv"),
                                   {"CH1": hds_payload(codes)},
                                   calibrated_volts_per_code=volts_per_code)
        return waveform.channels[0]

    def test_a_step_across_the_boundary_is_read_as_a_small_step(self):
        self.assertEqual([12, 4, -4, -11], WaveformData.unwrap_codes([12, 4, 252, 245]))

    def test_a_sequence_that_does_not_wrap_is_left_alone(self):
        self.assertEqual([10, 30, 50, 70], WaveformData.unwrap_codes([10, 30, 50, 70]))

    def test_pinning_works_before_the_header_has_a_scale(self):
        # The first capture of a session can arrive with no volts/div in the header.
        # Requiring one skipped the calibration entirely, so the panel drew the bench
        # reference (a 2.6 V sine at 1.87 V) until a later capture carried it.
        waveform = WaveformData()
        header = hds_header("5v")
        for entry in header.get("CHANNEL", []):
            entry.pop("SCALE", None)
        waveform.parse_hds_capture(header, {"CH1": hds_payload([57, 145, 233])})
        self.assertEqual("reference (uncalibrated)", waveform.channels[0]["volts_per_code_source"])
        self.assertTrue(waveform.calibrate_from_measurements(
            {"Vmin": "-5.0", "Vmax": "5.0", "Vpp": "10.0"}, pin_extremes=True))
        self.assertAlmostEqual(10.0, max(waveform.channels[0]["waveform"])
                               - min(waveform.channels[0]["waveform"]), places=2)

    def test_the_decoded_span_is_the_signal_not_the_wrap(self):
        # 2 x 65 codes peak to peak. Without unwrapping the extremes are 65 and -65
        # plus a 256-code wrap, and the span comes out at about 320 codes.
        channel = self.decode(self.wrapped_sine(), volts_per_code=0.02)
        volts = channel["waveform"]
        self.assertAlmostEqual(2.60, max(volts) - min(volts), delta=0.06)

    def test_nothing_in_the_trace_jumps_the_range(self):
        # The symptom on screen: a full-scale break twice per period.
        volts = self.decode(self.wrapped_sine(), volts_per_code=0.02)["waveform"]
        steps = [abs(b - a) for a, b in zip(volts, volts[1:])]
        self.assertLess(max(steps), 0.5)          # the largest real step is about 8 codes

    def test_a_sample_at_the_ambiguity_limit_is_left_where_it_comes(self):
        # Half the range or more cannot be resolved at this width: do not invent a branch.
        self.assertEqual([0, 128], WaveformData.unwrap_codes([0, 128]))


class FramingSettleTests(unittest.TestCase):
    """A capture must not redraw the frame from before a framing write.

    Measured on hardware with a known 5.00 Vpp signal: a capture started 50 ms
    after a volts/div write returned the previous setting's frame (34 codes where
    the new setting gives 36), and 150 ms was clean. Captures wait it out, so a
    volts/div change cannot look ignored for one frame.
    """

    def scope(self):
        scope = OWONScopeController(family="hds")
        scope.is_connected = True
        scope.send_command = lambda command: ""
        return scope

    def test_a_capture_after_a_framing_write_waits_for_the_instrument(self):
        scope = self.scope()
        slept = []
        real_sleep = time.sleep
        try:
            time.sleep = lambda seconds: slept.append(seconds)
            scope.set_channel_scale(1, "500mv")
            self.assertGreater(scope._framing_settle_until, time.monotonic())
            scope.wait_for_framing_settle()
        finally:
            time.sleep = real_sleep
        self.assertTrue(slept, "a framing write must make the next capture wait")
        self.assertGreater(slept[0], 0.0)
        self.assertLessEqual(slept[0], OWONScopeController.FRAMING_SETTLE_SECONDS)

    def test_the_capture_itself_does_the_waiting(self):
        # The wait belongs to the capture, not to every caller of the write: the
        # live loop and a one-off download both go through it.
        scope = self.scope()
        scope._cached_header = {"head": 3}
        scope.set_channel_scale(1, "500mv")
        waited = []
        scope.wait_for_framing_settle = lambda: waited.append(True)
        scope.download_waveform_data(reuse_header=True)
        self.assertEqual([True], waited)

    def test_a_quiet_capture_does_not_wait(self):
        scope = self.scope()
        slept = []
        real_sleep = time.sleep
        try:
            time.sleep = lambda seconds: slept.append(seconds)
            scope.wait_for_framing_settle()
        finally:
            time.sleep = real_sleep
        self.assertEqual([], slept)

    def test_the_wait_covers_the_measured_window(self):
        # 50 ms was stale on hardware, 150 ms was clean; the constant has to
        # clear the slower of those.
        self.assertGreaterEqual(OWONScopeController.FRAMING_SETTLE_SECONDS, 0.2)


if __name__ == "__main__":
    unittest.main()
