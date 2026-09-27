"""The capture as four views, the cursors, the table, the export and the player.

All of it hardware-free: every view is a reading of the same samples, so a
synthetic capture with a known signal in it is enough to say whether the maths,
the axes and the readouts are right.
"""

import math
import os
import tempfile
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

import analysis
import modern_lab
import waveform_export
from main import App
from modern_lab import SetupDialog
from owon_controller import OWONScopeController
from scope_setup import ScopeSetup

INTERVAL = 2e-05                      # 20 us per point: a real SCREEN capture
POINTS = 400


def sine_channel(frequency=1000.0, amplitude=12.5, name="CH1", points=POINTS, interval=INTERVAL):
    """A 25 Vpp sine by default: the signal that has been on the bench."""
    times = np.arange(points, dtype=float) * interval
    volts = amplitude * np.sin(2.0 * math.pi * frequency * times)
    return {"name": name, "waveform_data": list(volts), "point_interval": interval,
            "num_points": points, "whole_screen_points": points}


SETTINGS = {"model": "HDS271", "serial": "25520161", "firmware": "V1.3.0",
            "timebase_scale_s": 5e-4, "sample_rate": 50000.0, "point_interval_s": INTERVAL,
            "points": POINTS, "calibration_trim": 0.968992}


class ViewTestCase(unittest.TestCase):
    """An app with a synthetic capture in it, and no instrument anywhere."""

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root)
        self.dir = tempfile.TemporaryDirectory(prefix="views-")
        self.addCleanup(self.dir.cleanup)
        self.scope = self.fake_scope()
        self.app.scope = self.scope

    def tearDown(self):
        try:
            if self.root.winfo_exists():
                self.app.close_panel()
        except tk.TclError:
            pass

    def fake_scope(self, channels=None):
        scope = Mock(spec=OWONScopeController)
        scope.is_connected = True
        scope.model = "HDS271"
        scope.serial_number = "25520161"
        scope.firmware = "V1.3.0"
        for name in ("VOLTAGE_SCALES", "TIMEBASE_SCALES", "FRAMING_NAMES", "AVG_COUNTS",
                     "HDS_MEASUREMENT_ITEMS"):
            setattr(scope, name, getattr(OWONScopeController, name, ()))
        scope.waveform_data = waveform_export.LoadedCapture(
            channels if channels is not None else [sine_channel()], SETTINGS, source="test")
        return scope

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def texts(self):
        return [artist.get_text() for artist in self.app.ax.texts]


class ViewTests(ViewTestCase):
    def test_every_view_draws_something(self):
        for view in modern_lab.VIEWS:
            self.app.set_view(view)
            self.assertEqual(self.app._view, view)
            self.assertTrue(self.app.ax.lines or self.app.ax.texts, view)

    def test_the_active_view_button_is_the_one_lit(self):
        self.app.set_view("fft")
        self.assertEqual(self.app._view_buttons["fft"].cget("bg"), "#f1f3ed")
        self.assertEqual(self.app._view_buttons["time"].cget("bg"), "#444c4e")

    def test_an_unknown_view_falls_back_to_time(self):
        self.app.set_view("hologram")
        self.assertEqual(self.app._view, "time")

    def test_the_time_view_still_frames_from_the_capture(self):
        self.app.set_view("time")
        # 25 Vpp across 12 divisions at 5 V/div is about five divisions tall.
        low, high = self.app.ax.get_ylim()
        self.assertLess(low, -10.0)
        self.assertGreater(high, 10.0)

    def test_the_fft_view_puts_the_peak_on_its_own_frequency(self):
        self.app.scope = self.fake_scope([sine_channel(frequency=3000.0)])
        self.app.set_view("fft")
        self.assertTrue(any("3 kHz" in text for text in self.texts()), self.texts())

    def test_the_fft_view_states_the_limits_it_was_computed_under(self):
        self.app.set_view("fft")
        title = self.app.ax.get_title(loc="left")
        self.assertIn("Nyquist 25 kHz", title)
        self.assertIn("50 kSa/s", title)

    def test_the_fft_view_says_so_when_there_is_no_sample_interval(self):
        channel = sine_channel()
        channel["point_interval"] = 0
        self.app.scope = self.fake_scope([channel])
        self.app.set_view("fft")
        self.assertTrue(any("no sample interval" in text for text in self.texts()), self.texts())

    def test_the_maths_view_subtracts_one_channel_from_the_other(self):
        # CH2 is the same sine inverted, so CH1 - CH2 is twice the sine.
        first = sine_channel(amplitude=12.5)
        second = sine_channel(amplitude=-12.5, name="CH2")
        self.app.scope = self.fake_scope([first, second])
        self.app._math_options["op"] = "subtract"
        self.app._math_options["second"] = "CH2"
        self.app.set_view("math")
        line = self.app.ax.lines[0]
        self.assertAlmostEqual(float(np.max(np.abs(line.get_ydata()))), 25.0, delta=0.4)
        self.assertIn("CH1 − CH2", line.get_label())

    def test_the_maths_view_refuses_records_of_different_lengths(self):
        self.app.scope = self.fake_scope([sine_channel(points=400), sine_channel(name="CH2", points=300)])
        self.app._math_options["op"] = "subtract"
        self.app.set_view("math")
        self.assertTrue(any("different lengths" in text for text in self.texts()), self.texts())

    def test_the_xy_view_draws_one_channel_against_the_other(self):
        self.app.scope = self.fake_scope([sine_channel(), sine_channel(frequency=2000.0, name="CH2")])
        self.app.set_view("xy")
        self.assertIn("CH2", self.app.ax.get_ylabel())
        self.assertEqual(len(self.app.ax.lines[0].get_xdata()), POINTS)

    def test_the_xy_view_says_when_only_one_channel_is_available(self):
        self.app.set_view("xy")
        self.assertTrue(any("XY needs two channels" in text for text in self.texts()), self.texts())


class CursorTests(ViewTestCase):
    def test_two_markers_read_the_amplitude_and_the_period(self):
        self.app.set_view("time")
        # The peak is a quarter of a cycle in, the trough three quarters: 250 us and
        # 750 us for a 1 kHz sine.
        self.app.place_cursor("t1", 0.00025)
        self.app.place_cursor("t2", 0.00075)
        text = self.app.cursor_text.get()
        self.assertIn("ΔT 500 us", text)
        self.assertIn("1/ΔT 2 kHz", text)
        self.assertIn("CH1", text)
        # And the number itself: the trough is 24.95 V below the peak, one sample
        # short of 25 because the markers are interpolated on a 20 us grid.
        reading = self.app._cursor_reading
        self.assertAlmostEqual(reading["traces"]["CH1"]["dv"], -24.95, delta=0.1)

    def test_a_horizontal_pair_reads_a_voltage_difference(self):
        self.app.set_view("time")
        self.app.place_cursor("v1", -12.5)
        self.app.place_cursor("v2", 12.5)
        self.assertIn("ΔV 25 V", self.app.cursor_text.get())

    def test_the_markers_are_drawn_and_cleared(self):
        self.app.set_view("time")
        self.app.place_cursor("t1", 0.0004)
        self.assertIn("t1", self.app._cursor_artists)
        self.app.clear_cursors()
        self.assertEqual(self.app._cursor_artists, {})
        self.assertIn("none placed", self.app.cursor_text.get())

    def test_a_press_near_a_marker_picks_it_up(self):
        self.app.set_view("time")
        self.app.place_cursor("t1", 0.0004)
        self.app.canvas.draw()
        pixel = self.app.ax.transData.transform((0.0004, 0.0))
        event = SimpleNamespace(inaxes=self.app.ax, x=pixel[0], y=pixel[1],
                                xdata=0.0004, ydata=0.0)
        self.assertEqual(self.app._marker_under(event), "t1")

    def test_a_press_far_from_every_marker_picks_up_nothing(self):
        self.app.set_view("time")
        event = SimpleNamespace(inaxes=self.app.ax, x=-500.0, y=-500.0,
                                xdata=0.0, ydata=0.0)
        self.assertIsNone(self.app._marker_under(event))

    def test_a_click_places_the_selected_marker(self):
        self.app.set_view("time")
        self.app.set_cursor_target("v2")
        event = SimpleNamespace(inaxes=self.app.ax, x=100.0, y=100.0, xdata=0.0002, ydata=6.0)
        self.app._on_plot_press(event)
        self.assertAlmostEqual(self.app._cursors["v2"], 6.0)
        self.assertIsNone(self.app._cursors["t1"])

    def test_the_target_button_shows_which_marker_is_next(self):
        self.app.set_cursor_target("v1")
        self.assertEqual(self.app._cursor_buttons["v1"].cget("bg"), "#9cdc9c")
        self.assertEqual(self.app._cursor_buttons["t1"].cget("bg"), "#444c4e")

    def test_a_marker_outside_the_record_reads_nothing_rather_than_a_number(self):
        self.app.set_view("time")
        self.app.place_cursor("t1", 0.00025)
        self.app.place_cursor("t2", 99.0)
        reading = self.app._cursor_reading
        self.assertIsNone(reading["traces"]["CH1"]["at_t2"])


class TableTests(ViewTestCase):
    def test_the_table_has_a_row_per_sample_and_a_column_per_channel(self):
        self.app.scope = self.fake_scope([sine_channel(), sine_channel(name="CH2")])
        columns, rows = self.app.table_data()
        self.assertEqual(columns, ["index", "time_s", "CH1", "CH2"])
        self.assertEqual(len(rows), POINTS)
        self.assertEqual(rows[0][0], 0)
        self.assertAlmostEqual(float(rows[1][1]), INTERVAL)

    def test_the_table_says_nothing_when_there_is_no_capture(self):
        self.app.scope = self.fake_scope([])
        columns, rows = self.app.table_data()
        self.assertEqual((columns, rows), ([], []))

    def test_the_dialog_shows_the_rows_and_caps_them(self):
        rows = [[index, "0"] for index in range(modern_lab.CaptureTableDialog.DISPLAY_LIMIT + 10)]
        dialog = modern_lab.CaptureTableDialog(self.root, ["index", "time_s"], rows, on_save=None)
        try:
            dialog.update()
            self.assertTrue(dialog.winfo_exists())
        finally:
            dialog.destroy()


class ExportTests(ViewTestCase):
    def test_an_export_of_the_live_view_carries_its_provenance(self):
        self.app.set_view("time")
        self.app.setup.values["calibration_trim"] = 0.968992
        path = self.path("capture.xlsx")
        waveform_export.export_capture(path, self.app.capture_channels(),
                                       self.app.provenance_block())
        sheets = waveform_export.read_xlsx_sheets(path)
        found = {row[0]: row[1] for row in sheets["Settings"][1:] if len(row) >= 2}
        self.assertEqual(found["model"], "HDS271")
        self.assertEqual(found["serial"], "25520161")
        self.assertAlmostEqual(float(found["calibration_trim"]), 0.968992)
        self.assertEqual(found["view"], "time")

    def test_a_saved_capture_can_be_opened_again(self):
        path = waveform_export.write_csv(self.path("capture.csv"), self.app.capture_channels(),
                                        self.app.provenance_block())
        self.app.scope = self.fake_scope([])                     # nothing live any more
        loaded = self.app.load_capture_file(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded.channels), 1)
        self.assertEqual(len(loaded.channels[0]["waveform_data"]), POINTS)
        # The interval is measured from the file rather than assumed.
        self.assertAlmostEqual(loaded.channels[0]["point_interval"], INTERVAL, delta=1e-9)
        self.assertIn("FILE", self.app.capture_state.get())

    def test_opening_a_file_stops_the_live_loop(self):
        path = waveform_export.write_csv(self.path("capture.csv"), self.app.capture_channels(),
                                        self.app.provenance_block())
        self.app.auto_refresh_var.set(True)
        self.app.load_capture_file(path)
        self.assertFalse(self.app.auto_refresh_var.get())

    def test_an_empty_file_is_refused(self):
        path = self.path("empty.csv")
        open(path, "w", encoding="utf-8").write("channel,index,time_s,volts\n")
        with patch("main.messagebox.showwarning") as warned:
            self.assertIsNone(self.app.load_capture_file(path))
        warned.assert_called_once()

    def test_a_file_we_cannot_read_is_reported_not_crashed(self):
        path = self.path("notes.txt")
        open(path, "w", encoding="utf-8").write("not a capture")
        with patch("main.messagebox.showerror") as reported:
            self.assertIsNone(self.app.load_capture_file(path))
        reported.assert_called_once()
        self.assertIn("Could not open", self.app.log_text.get("1.0", "end"))


class ConsoleTests(ViewTestCase):
    def test_a_query_is_sent_and_its_reply_shown(self):
        # The command is a round trip, so the request reports that it went and the
        # reply arrives through the console when the instrument answers.
        self.scope.query.return_value = "OWON,HDS271,25520161,V1.3.0"
        shown = []
        self.app._console_sink = shown.append
        status = self.app.run_scpi_command("*IDN?", False)
        self.assertIn("sent", status)
        self.assertTrue(self.app.wait_for_instrument())
        self.scope.query.assert_called_once_with("*IDN?")
        self.assertEqual(shown, ["OWON,HDS271,25520161,V1.3.0"])
        self.assertIn("OWON,HDS271,25520161,V1.3.0", self.app.log_text.get("1.0", "end"))

    def test_a_write_is_refused_unless_it_was_asked_for(self):
        reply = self.app.run_scpi_command(":CHANnel1:SCALe 5v", False)
        self.assertIn("refused", reply)
        self.scope.send_command.assert_not_called()

    def test_a_write_goes_through_when_allowed(self):
        self.scope.send_command.return_value = "OK"
        shown = []
        self.app._console_sink = shown.append
        self.assertIn("sent", self.app.run_scpi_command(":TRIGger:FORCe", True))
        self.assertTrue(self.app.wait_for_instrument())
        self.scope.send_command.assert_called_once()
        self.assertEqual(shown, ["OK"])

    def test_a_command_while_disconnected_says_so(self):
        self.scope.is_connected = False
        self.assertEqual(self.app.run_scpi_command("*IDN?", True), "not connected")

    def test_the_console_reports_a_failure_rather_than_raising(self):
        self.scope.query.side_effect = RuntimeError("endpoint busy")
        shown = []
        self.app._console_sink = shown.append
        self.assertIn("sent", self.app.run_scpi_command("*IDN?", False))
        self.assertTrue(self.app.wait_for_instrument())
        self.assertTrue(shown and "endpoint busy" in shown[0])
        self.assertIn("endpoint busy", self.app.log_text.get("1.0", "end"))

    def test_the_dialog_sends_and_logs(self):
        seen = []

        def sender(text, allow):
            seen.append((text, allow))
            return "sent"

        dialog = modern_lab.ScpiConsoleDialog(self.root, on_send=sender, scope=self.scope)
        try:
            dialog.entry.insert(0, "*IDN?")
            dialog.send()
            self.assertEqual(seen, [("*IDN?", False)])
            self.assertIsNone(dialog.entry.get() or None)
            self.assertIn("*IDN?", dialog.log.get("1.0", "end"))
            self.assertIn("sent", dialog.log.get("1.0", "end"))
            # And the reply is written when it lands, which is after send() returns.
            dialog.append("OK")
            self.assertIn("OK", dialog.log.get("1.0", "end"))
        finally:
            dialog.destroy()


class MultimeterModeTests(ViewTestCase):
    """The multimeter panel offers what the instrument answers, not what the manual lists."""

    def setUp(self):
        super().setUp()
        # A real report, because the shape of it is what the panel is built from.
        self.scope.dmm_capabilities.return_value = {
            "reading": True, "typed": {"VOLTage": "DC", "CURRent": "AC"},
            "silent": ["RESistance", "DIODe", "CONTinuity", "CAPacitance"], "relative": True}
        self.scope.get_dmm_function.return_value = ("VOLTage", "DC")
        self.scope.get_dmm_relative.return_value = False

    def test_only_the_answerable_functions_are_offered(self):
        offered = self.app.refresh_dmm_modes(
            {"reading": True, "typed": {"VOLTage": "DC", "CURRent": "AC"},
             "silent": ["RESistance", "DIODe", "CONTinuity", "CAPacitance"]})
        self.assertEqual(offered, ["VOLT DC/AC", "AMP DC/AC"])
        self.assertEqual(tuple(self.app.dmm_mode_box.cget("values")), tuple(offered))

    def test_a_function_the_unit_answers_is_offered_and_settable(self):
        self.app.refresh_dmm_modes({"reading": True, "typed": {}, "silent": []})
        self.assertIn("RESISTANCE", self.app.dmm_mode_box.cget("values"))
        self.scope.dmm_capabilities.return_value = {"reading": True, "typed": {}, "silent": [],
                                                    "relative": True}
        self.scope.set_dmm_function.return_value = ("RESistance", None)
        self.app.set_dmm_mode("RESISTANCE")
        self.scope.set_dmm_function.assert_called_once_with("RESistance")

    def test_a_write_the_instrument_ignores_is_reported_not_shown_as_success(self):
        self.scope.set_dmm_function.return_value = (None, None)
        self.app.set_dmm_mode("RESISTANCE")
        self.assertIn("did not take", self.app.dmm_note.get())
        self.assertIn("did not take", self.app.log_text.get("1.0", "end"))

    def test_no_mode_chosen_says_so(self):
        self.app.dmm_mode.set("")
        self.app.set_dmm_mode("")
        self.assertIn("no mode chosen", self.app.log_text.get("1.0", "end"))

    def test_setting_a_mode_while_the_instrument_fails_is_reported(self):
        self.scope.set_dmm_function.side_effect = RuntimeError("no reply")
        self.app.set_dmm_mode("DIODE")
        self.assertIn("Multimeter mode failed", self.app.log_text.get("1.0", "end"))


class ResponsivenessTests(ViewTestCase):
    """The window must keep beating while the instrument is slow.

    This is the complaint the whole I/O worker exists for: during live acquisition
    the panel stopped answering. The cause was not the capture - that always ran on
    a worker - but the framing watch, the cursor readback and the multimeter read,
    which were made from the Tk callbacks, so each of them froze the window for the
    length of a USB round trip.
    """

    SLOW = 0.2

    def slow(self, result):
        """A scope call that takes 200 ms and then answers."""
        def call(*args, **kwargs):
            time.sleep(self.SLOW)
            return result() if callable(result) else result
        return call

    def make_the_instrument_slow(self):
        self.scope.download_waveform_data.side_effect = self.slow(True)
        self.scope.framing_signature.side_effect = self.slow(("5v", "10X", "1ms"))
        self.scope.get_trigger_level_volts.side_effect = self.slow(1.0)
        self.scope.get_horizontal_position_seconds.side_effect = self.slow(0.0)
        self.scope.get_dmm_reading.side_effect = self.slow(0.0)

    def beat_for(self, seconds, action):
        """Run the main loop for a while, timing the gaps between heartbeats."""
        beats = []

        def beat():
            beats.append(time.monotonic())
            if time.monotonic() < deadline:
                self.root.after(10, beat)

        deadline = time.monotonic() + seconds
        self.root.after(10, beat)
        action()
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.001)
        self.assertTrue(self.app.wait_for_instrument())
        return [later - earlier for earlier, later in zip(beats, beats[1:])]

    def test_the_window_keeps_beating_while_the_instrument_is_slow(self):
        self.make_the_instrument_slow()

        def live():
            self.app.auto_refresh_var.set(True)
            self.app.toggle_live()

        gaps = self.beat_for(1.6, live)
        self.assertTrue(gaps, "the main loop never ran at all")
        # A callback that waits on the instrument shows up here as one long gap.
        self.assertLess(max(gaps), 0.15,
                        "the window was frozen for %.0f ms while the instrument was "
                        "answering" % (max(gaps) * 1000))
        self.app.auto_refresh_var.set(False)

    def test_a_control_answers_without_waiting_for_the_instrument(self):
        self.make_the_instrument_slow()
        started = time.monotonic()
        self.app.set_channel_display(1, False)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.05, "the press waited on the instrument")
        self.assertTrue(self.app.wait_for_instrument())


class TriggerPanelTests(ViewTestCase):
    def test_the_panel_lists_what_the_instrument_says(self):
        self.scope.get_trigger_mode.return_value = "AUTO"
        self.scope.get_timebase_scale.return_value = "500us"
        # The dialog asks for the list; the instrument's rows come back from the
        # worker and the capture's own rows are added on this side.
        rows = {}
        self.app.trigger_state_rows(lambda values, error=None: rows.update(values))
        self.assertTrue(self.app.wait_for_instrument())
        self.assertEqual(rows["Trigger mode"], "AUTO")
        self.assertEqual(rows["Timebase"], "500us")
        # And the capture's own numbers, which are what the plot was drawn with.
        self.assertIn("Captured CH1 interval", rows)

    def test_a_node_that_fails_is_reported_against_its_own_row(self):
        self.scope.get_trigger_level_volts.side_effect = RuntimeError("no reply")
        rows = dict(self.app.read_trigger_rows(self.scope))
        self.assertIn("error", rows["Trigger level"])

    def test_a_readout_dialog_shows_the_rows(self):
        self.scope.get_trigger_mode.return_value = "SINGle"
        dialog = modern_lab.ReadoutDialog(self.root, "Trigger", self.app.trigger_state_rows)
        try:
            dialog.update()
            self.assertTrue(dialog.body.winfo_children())
        finally:
            dialog.destroy()


class AutosetTests(ViewTestCase):
    def real_scope(self, replies):
        scope = OWONScopeController(family="hds")
        scope.query = lambda node: replies.get(node.upper(), "")
        scope.send_command = lambda node: replies.setdefault("--sent", []).append(node) or "OK"
        scope.is_connected = True
        scope.download_waveform_data = lambda **kwargs: False
        return scope

    def test_the_time_axis_is_framed_on_the_measured_frequency(self):
        scope = self.real_scope({})
        scope.get_measurements_numeric = lambda channel=1: {"frequency": 1000.0, "vpp": 25.0}
        sent = []
        scope.set_timebase_scale = lambda value: sent.append(value) or "OK"
        scope.get_timebase_scale = lambda: "200us"
        self.app.scope = scope
        self.app.software_autoset()
        self.assertTrue(self.app.wait_for_instrument())
        # Three cycles of 1 kHz across 12 divisions is 250 us/div, and the ladder
        # step nearest by ratio is 200us.
        self.assertEqual(sent, ["200us"])
        logged = self.app.log_text.get("1.0", "end")
        self.assertIn("Press AUTO on the instrument for the vertical", logged)

    def test_no_frequency_means_no_write(self):
        scope = self.real_scope({})
        scope.get_measurements_numeric = lambda channel=1: {}
        sent = []
        scope.set_timebase_scale = lambda value: sent.append(value) or "OK"
        self.app.scope = scope
        self.app.software_autoset()
        self.assertTrue(self.app.wait_for_instrument())
        self.assertEqual(sent, [])
        self.assertIn("no frequency", self.app.log_text.get("1.0", "end"))

    def test_autoset_while_disconnected_says_so(self):
        self.scope.is_connected = False
        self.app.software_autoset()
        self.assertIn("Connect the instrument", self.app.status_var.get())


class RecordingTests(ViewTestCase):
    def test_recording_writes_one_file_per_capture(self):
        folder = os.path.join(self.dir.name, "records")
        self.app.setup.values["record_folder"] = folder
        self.app.toggle_recording()
        self.assertTrue(self.app.recording)
        first = self.app.record_capture()
        second = self.app.record_capture()
        # The files are written on the worker, so the frame does not wait for the
        # disk; the test waits for the writes the same way the app would.
        self.assertTrue(self.app.wait_for_instrument())
        self.app.toggle_recording()
        self.assertFalse(self.app.recording)
        self.assertEqual(self.app._records, 2)
        self.assertNotEqual(first, second)
        for path in (first, second):
            self.assertTrue(os.path.exists(path))
            settings, channels = waveform_export.read_capture(path)
            self.assertEqual(len(channels[0]["waveform_data"]), POINTS)

    def test_recording_without_a_folder_explains_what_to_do(self):
        self.app.setup.values["record_folder"] = ""
        with patch("main.messagebox.showinfo") as told:
            self.app.toggle_recording()
        self.assertFalse(self.app.recording)
        told.assert_called_once()
        self.assertIn("SETUP", told.call_args[0][1])

    def test_a_failed_write_stops_the_recording_rather_than_going_quiet(self):
        self.app.setup.values["record_folder"] = os.path.join(self.dir.name, "file-not-folder")
        open(self.app.setup.values["record_folder"], "w").close()      # a file, not a directory
        self.app.recording = True
        with patch.object(waveform_export, "write_csv", side_effect=OSError("disk full")):
            path = self.app.record_capture()
            # The write is on the worker, so the failure is known a moment later -
            # and it still stops the recording rather than going quiet. The frame is
            # named and returned either way; what the disk made of it arrives after.
            self.assertTrue(self.app.wait_for_instrument())
        self.assertTrue(path)
        self.assertFalse(self.app.recording)
        self.assertIn("stopped", self.app.record_label.get())


class SetupDialogTests(ViewTestCase):
    def test_the_dialog_collects_the_new_settings(self):
        dialog = SetupDialog(self.root, self.app.setup, devices=[], last_amplitude=None,
                             on_apply=None)
        try:
            dialog.record_folder.set("D:/captures")
            dialog.live_interval.set("0.75")
            dialog.palette_choice.set("Print")
            dialog.fft_window_choice.set("blackman")
            dialog.fft_format_choice.set("Vrms")
            collected = dialog.collect()
            self.assertEqual(collected.values["record_folder"], "D:/captures")
            self.assertAlmostEqual(collected.values["live_interval_s"], 0.75)
            self.assertEqual(collected.values["palette"], "Print")
            self.assertEqual(collected.values["fft_window"], "blackman")
            self.assertEqual(collected.values["fft_format"], "Vrms")
        finally:
            dialog.destroy()

    def test_a_bad_interval_is_clamped_rather_than_stored(self):
        dialog = SetupDialog(self.root, self.app.setup, devices=[], last_amplitude=None,
                             on_apply=None)
        try:
            dialog.live_interval.set("0")
            self.assertGreaterEqual(dialog.collect().values["live_interval_s"], 0.05)
            dialog.live_interval.set("nonsense")
            self.assertAlmostEqual(dialog.collect().values["live_interval_s"], 0.5)
        finally:
            dialog.destroy()

    def test_the_palette_names_are_the_ones_the_plot_knows(self):
        self.assertEqual(set(ScopeSetup.PALETTE_NAMES), set(modern_lab.PALETTES))

    def test_the_spectrum_choices_are_the_ones_the_analysis_knows(self):
        self.assertEqual(set(ScopeSetup().DEFAULTS["fft_window"]) and
                         set(analysis.WINDOWS), set(analysis.WINDOWS))

    def test_every_choice_the_dialog_offers_is_one_the_setup_will_keep(self):
        setup = ScopeSetup({"palette": "Print", "fft_window": "blackman", "fft_format": "Vrms"})
        self.assertEqual(setup.values["palette"], "Print")
        self.assertEqual(setup.values["fft_window"], "blackman")
        self.assertEqual(setup.values["fft_format"], "Vrms")
        store = ScopeSetup({"palette": "Neon", "fft_window": "gaussian", "fft_format": "watts"})
        self.assertEqual(store.values["palette"], "Dark")
        self.assertEqual(store.values["fft_window"], "hanning")
        self.assertEqual(store.values["fft_format"], "dBV")


class PaletteTests(ViewTestCase):
    def test_every_palette_has_the_colours_the_plot_asks_for(self):
        for name, colours in modern_lab.PALETTES.items():
            for key in ("face", "grid", "text", "trace1", "trace2", "math"):
                self.assertIn(key, colours, name)

    def test_changing_the_palette_redraws_without_losing_the_capture(self):
        self.app.set_view("time")
        self.app.set_palette("Print")
        self.assertEqual(self.app._palette_name, "Print")
        self.assertEqual(self.app.ax.get_facecolor()[:3], (1.0, 1.0, 1.0))
        self.assertTrue(self.app.ax.lines)

    def test_an_unknown_palette_is_ignored(self):
        self.app.set_palette("Dark")
        self.app.set_palette("Hologram")
        self.assertEqual(self.app._palette_name, "Dark")


if __name__ == "__main__":
    unittest.main()
