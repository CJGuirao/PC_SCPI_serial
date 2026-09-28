"""The analysis layer: spectrum, maths, cursors, and the time axis.

Everything is checked against a signal whose answer is known in advance, because
the point of this layer is to be trusted when the instrument's own labels are not.
"""

import math
import unittest

import numpy as np

from modernlab import analysis
from owon_controller import OWONScopeController

SAMPLE_INTERVAL = 2e-05          # 20 us per point, what a SCREEN capture really is
POINTS = 300


def sine_channel(frequency=3000.0, amplitude=2.5, interval=SAMPLE_INTERVAL,
                 points=POINTS, offset=0.0):
    """A channel dict shaped like the decoder produces, with a known sine in it."""
    times = np.arange(points, dtype=float) * interval
    volts = offset + amplitude * np.sin(2.0 * math.pi * frequency * times)
    return {"name": "CH1", "waveform": list(volts),
            "point_interval": interval, "num_points": points}


class WindowTests(unittest.TestCase):
    def test_rectangle_is_a_plain_window(self):
        values = analysis.window_values("rectangle", 8)
        self.assertEqual(values.shape, (8,))
        self.assertTrue(np.allclose(values, 1.0))

    def test_the_named_windows_taper_the_ends(self):
        for name in ("hanning", "hamming", "blackman"):
            values = analysis.window_values(name, 64)
            # The middle is the peak and the ends are smaller, whichever window it is.
            self.assertLess(abs(values[0]), values[32] * 0.5, name)
        # Hanning and Blackman reach zero; Hamming stops at 0.08, by definition, so
        # a test that demanded zero of all three would be asserting arithmetic that
        # is not true.
        self.assertAlmostEqual(analysis.window_values("hanning", 64)[0], 0.0, places=9)
        self.assertAlmostEqual(analysis.window_values("blackman", 64)[0], 0.0, places=6)
        self.assertAlmostEqual(analysis.window_values("hamming", 64)[0], 0.08, places=6)

    def test_an_unknown_window_is_refused(self):
        with self.assertRaises(ValueError):
            analysis.window_values("gaussian", 16)


class SpectrumTests(unittest.TestCase):
    def test_a_sine_lands_on_its_own_frequency(self):
        spectrum = analysis.fft_spectrum(sine_channel(frequency=3000.0))
        peaks = analysis.spectrum_peaks(spectrum, count=1)
        self.assertTrue(peaks)
        self.assertAlmostEqual(peaks[0]["frequency"], 3000.0, delta=spectrum["bin_hz"] * 2)

    def test_a_sine_reads_its_own_amplitude(self):
        # A 2.5 V peak sine is 1.768 Vrms, and the window's gain is divided out so
        # a Blackman window does not make the same signal look smaller.
        for window in analysis.WINDOWS:
            spectrum = analysis.fft_spectrum(sine_channel(amplitude=2.5), window=window, fmt="Vrms")
            peaks = analysis.spectrum_peaks(spectrum, count=1)
            self.assertAlmostEqual(peaks[0]["value"], 1.7678, delta=0.06, msg=window)
            self.assertAlmostEqual(peaks[0]["amplitude"], 2.5, delta=0.09, msg=window)

    def test_one_volt_rms_is_zero_dBV(self):
        # 1 Vrms means a peak of sqrt(2).
        spectrum = analysis.fft_spectrum(sine_channel(amplitude=math.sqrt(2.0)))
        peak = analysis.spectrum_peaks(spectrum, count=1)[0]
        self.assertAlmostEqual(peak["value"], 0.0, delta=0.1)

    def test_the_axis_comes_from_the_capture_not_the_announcement(self):
        # The same samples, sent twice as fast, must read twice the frequency. This
        # is the mistake the instrument invites by announcing "1MSa/s" while
        # sending 300 points at 20 us.
        # The same record, declared at half the interval, is the same volts read
        # twice as fast - so the peak frequency doubles.
        record = sine_channel(frequency=3000.0, interval=2e-05)
        slower = dict(record, point_interval=2e-05)
        faster = dict(record, point_interval=1e-05)
        slow = analysis.fft_spectrum(slower, window="rectangle")
        fast = analysis.fft_spectrum(faster, window="rectangle")
        slow_peak = analysis.spectrum_peaks(slow, count=1)[0]["frequency"]
        fast_peak = analysis.spectrum_peaks(fast, count=1)[0]["frequency"]
        self.assertAlmostEqual(slow_peak, 3000.0, delta=200.0)
        self.assertAlmostEqual(fast_peak, 6000.0, delta=400.0)

    def test_the_limits_are_stated_with_the_spectrum(self):
        spectrum = analysis.fft_spectrum(sine_channel())
        self.assertAlmostEqual(spectrum["sample_rate"], 50000.0)
        self.assertAlmostEqual(spectrum["nyquist"], 25000.0)
        self.assertAlmostEqual(spectrum["bin_hz"], 50000.0 / POINTS)

    def test_a_capture_without_an_interval_has_no_spectrum(self):
        channel = sine_channel()
        channel["point_interval"] = 0
        self.assertIsNone(analysis.fft_spectrum(channel))

    def test_a_square_wave_reports_its_fundamental_first(self):
        times = np.arange(POINTS, dtype=float) * SAMPLE_INTERVAL
        volts = 12.5 * np.sign(np.sin(2.0 * math.pi * 1000.0 * times))
        channel = {"name": "CH1", "waveform": list(volts), "point_interval": SAMPLE_INTERVAL}
        peaks = analysis.spectrum_peaks(analysis.fft_spectrum(channel, window="rectangle"), count=3)
        self.assertAlmostEqual(peaks[0]["frequency"], 1000.0, delta=200.0)
        # A square wave's next component is its third harmonic.
        self.assertAlmostEqual(peaks[1]["frequency"], 3000.0, delta=400.0)

    def test_the_dBV_form_is_the_log_of_the_rms_volts(self):
        spectrum = analysis.fft_spectrum(sine_channel(amplitude=2.5))
        peak = analysis.spectrum_peaks(spectrum, count=1)[0]
        # 20*log10(2.5/sqrt(2)) = 4.95 dBV
        self.assertAlmostEqual(peak["value"], 4.95, delta=0.1)

    def test_thinning_keeps_the_peaks(self):
        long_channel = sine_channel(frequency=3000.0, points=20000)
        spectrum = analysis.fft_spectrum(long_channel)
        frequencies, values = analysis.display_spectrum(spectrum, max_points=500)
        self.assertLess(frequencies.size, spectrum["frequencies"].size)
        self.assertLessEqual(frequencies.size, 500 + 200)
        peak_index = int(np.argmax(values))
        self.assertAlmostEqual(frequencies[peak_index], 3000.0, delta=spectrum["bin_hz"] * 3)

    def test_the_summary_states_the_nyquist_limit(self):
        line = analysis.describe_spectrum(analysis.fft_spectrum(sine_channel()))
        self.assertIn("3 kHz", line)
        self.assertIn("Nyquist 25 kHz", line)
        self.assertIn("Hanning", line)

    def test_the_summary_says_so_when_there_is_nothing_to_describe(self):
        self.assertIn("no sample interval", analysis.describe_spectrum(None))


class MathsTests(unittest.TestCase):
    def test_the_two_trace_operations(self):
        first = [1.0, 2.0, 3.0]
        second = [0.5, 0.5, 0.5]
        self.assertEqual(list(analysis.math_trace(first, second, "add")), [1.5, 2.5, 3.5])
        self.assertEqual(list(analysis.math_trace(first, second, "subtract")), [0.5, 1.5, 2.5])
        self.assertEqual(list(analysis.math_trace(first, second, "multiply")), [0.5, 1.0, 1.5])

    def test_inverting_needs_one_trace(self):
        self.assertEqual(list(analysis.math_trace([1.0, -2.0], None, "invert")), [-1.0, 2.0])

    def test_mismatched_records_are_refused_not_padded(self):
        with self.assertRaises(ValueError):
            analysis.math_trace([1.0, 2.0], [1.0], "add")

    def test_an_unknown_operation_is_refused(self):
        with self.assertRaises(ValueError):
            analysis.math_trace([1.0], [1.0], "divide")

    def test_the_maths_trace_names_itself(self):
        self.assertEqual(analysis.math_label("subtract", "CH1", "CH2"), "CH1 − CH2")
        self.assertEqual(analysis.math_label("multiply", "CH1", "CH2"), "CH1 × CH2")
        self.assertEqual(analysis.math_label("invert", "CH2"), "CH2 inverted")


class XyTests(unittest.TestCase):
    def test_pairs_are_cut_to_the_shorter_record(self):
        xs, ys = analysis.xy_pairs([1, 2, 3], [4, 5])
        self.assertEqual(list(xs), [1, 2])
        self.assertEqual(list(ys), [4, 5])

    def test_nothing_in_gives_nothing_out(self):
        xs, ys = analysis.xy_pairs([], [])
        self.assertEqual(xs.size, 0)
        self.assertEqual(ys.size, 0)


class CursorTests(unittest.TestCase):
    def setUp(self):
        # A 25 Vpp, 1 kHz sine: 12.5 V above and below zero, one cycle per ms.
        interval = 1e-05
        points = 400
        times = np.arange(points, dtype=float) * interval
        volts = 12.5 * np.sin(2.0 * math.pi * 1000.0 * times)
        self.traces = {"CH1": (times, volts)}

    def test_the_peak_and_the_trough_read_the_amplitude(self):
        reading = analysis.cursor_readings(self.traces, vertical_times=(0.00025, 0.00075))
        self.assertAlmostEqual(reading["traces"]["CH1"]["at_t1"], 12.5, delta=0.05)
        self.assertAlmostEqual(reading["traces"]["CH1"]["at_t2"], -12.5, delta=0.05)
        self.assertAlmostEqual(reading["traces"]["CH1"]["dv"], -25.0, delta=0.1)

    def test_the_interval_reads_back_as_a_frequency(self):
        # Half a cycle apart: 0.5 ms, so 1/dT is 2 kHz.
        reading = analysis.cursor_readings(self.traces, vertical_times=(0.00025, 0.00075))
        self.assertAlmostEqual(reading["dt"], 0.0005, delta=1e-9)
        self.assertAlmostEqual(reading["frequency"], 2000.0, delta=1.0)

    def test_horizontal_cursors_give_a_voltage_difference(self):
        reading = analysis.cursor_readings(self.traces, horizontal_volts=(-6.0, 6.0))
        self.assertAlmostEqual(reading["dv"], 12.0, delta=1e-9)
        self.assertIsNone(reading["dt"])

    def test_a_marker_outside_the_record_reads_nothing(self):
        reading = analysis.cursor_readings(self.traces, vertical_times=(0.00025, 99.0))
        self.assertIsNone(reading["traces"]["CH1"]["at_t2"])
        self.assertIsNone(reading["traces"]["CH1"]["dv"])

    def test_the_line_says_what_was_read(self):
        reading = analysis.cursor_readings(self.traces, vertical_times=(0.00025, 0.00075),
                                          horizontal_volts=(-12.5, 12.5))
        line = analysis.describe_cursors(reading)
        self.assertIn("ΔT 500 us", line)
        self.assertIn("1/ΔT 2 kHz", line)
        self.assertIn("ΔV 25 V", line)

    def test_nothing_placed_says_nothing_is_placed(self):
        self.assertIn("none placed",
                      analysis.describe_cursors(analysis.cursor_readings(self.traces)))

    def test_one_marker_alone_says_so(self):
        line = analysis.describe_cursors(analysis.cursor_readings(self.traces, vertical_times=(0.00025, None)))
        self.assertIn("one marker", line)


class ScaleParsingTests(unittest.TestCase):
    def test_the_instruments_own_spellings(self):
        self.assertAlmostEqual(analysis.scale_to_float("500us"), 5e-4)
        self.assertAlmostEqual(analysis.scale_to_float("2mv"), 2e-3)
        self.assertAlmostEqual(analysis.scale_to_float("1v"), 1.0)
        self.assertAlmostEqual(analysis.scale_to_float("5ns"), 5e-9)
        self.assertAlmostEqual(analysis.scale_to_float("100s"), 100.0)
        self.assertAlmostEqual(analysis.scale_to_float("1ms"), 1e-3)
        self.assertAlmostEqual(analysis.scale_to_float(5e-4), 5e-4)

    def test_junk_is_refused_rather_than_guessed(self):
        for text in (None, "", "fast", "5 bananas"):
            self.assertIsNone(analysis.scale_to_float(text), text)

    def test_the_ladder_matches_the_controllers_own_list(self):
        # Two lists that must not drift: the controller writes these strings, the
        # analysis turns them into seconds.
        self.assertEqual(len(analysis.TIMEBASE_LADDER), len(OWONScopeController.TIMEBASE_SCALES))
        for step, text in zip(analysis.TIMEBASE_LADDER, OWONScopeController.TIMEBASE_SCALES):
            self.assertAlmostEqual(step, analysis.scale_to_float(text), msg=text)

    def test_a_timebase_for_a_frequency_lands_on_the_ladder(self):
        # Three cycles of 1 kHz across 12 divisions wants 250 us/div: the ladder
        # has 200 us and 500 us, and 200 us is closer in ratio.
        chosen = analysis.timebase_for(1000.0)
        self.assertAlmostEqual(chosen, 200e-6)
        self.assertIn(chosen, analysis.TIMEBASE_LADDER)

    def test_no_frequency_no_suggestion(self):
        self.assertIsNone(analysis.timebase_for(0))
        self.assertIsNone(analysis.timebase_for(None))

    def test_the_labels_a_person_reads(self):
        self.assertEqual(analysis.format_rate(50000.0), "50 kSa/s")
        self.assertEqual(analysis.format_rate(1000000.0), "1 MSa/s")
        self.assertEqual(analysis.format_rate(None), "—")
        self.assertEqual(analysis.format_hz(1000.0), "1 kHz")
        self.assertEqual(analysis.format_hz(2.5e6), "2.5 MHz")
        self.assertEqual(analysis.format_seconds(5e-4), "500 us")
        self.assertEqual(analysis.format_seconds(2e-3), "2 ms")
        self.assertEqual(analysis.format_volts(25.0), "25 V")
        self.assertEqual(analysis.format_volts(0.0125), "12.5 mV")
        self.assertEqual(analysis.format_hz(None), "—")


if __name__ == "__main__":
    unittest.main()
