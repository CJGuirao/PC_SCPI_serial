"""Hardware-free checks for the front-panel work: channel probe, live cursors, AUTO."""
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import modern_lab
from owon_controller import OWONScopeController
from modernlab.instrument.capture.waveform import WaveformData
from unittest.mock import Mock, patch
from modernlab.instrument.capture.waveform import HDS_VERTICAL_DIVISIONS


class InstrumentShapeTests(unittest.TestCase):
    """The channel count is probed, not inferred from the model name."""

    def scope_with(self, replies):
        scope = OWONScopeController(family="hds")
        scope.query = lambda node: replies.get(node.upper(), "")
        return scope

    def test_single_channel_instrument_reports_one(self):
        scope = self.scope_with({":CH1:SCALE?": "5e-01", ":CH2:SCALE?": ""})
        self.assertEqual(1, scope.get_channel_count())

    def test_two_channel_instrument_reports_two(self):
        scope = self.scope_with({":CH1:SCALE?": "5e-01", ":CH2:SCALE?": "1e+00"})
        self.assertEqual(2, scope.get_channel_count())

    def test_stale_payload_is_not_mistaken_for_a_channel(self):
        # Unsupported nodes have been seen to answer with a stale capture
        # payload, so only a parseable number counts as a channel.
        scope = self.scope_with({":CH2:SCALE?": '{"HEAD":3}'})
        self.assertEqual(1, scope.get_channel_count())

    def test_probe_runs_once(self):
        asked = []
        scope = OWONScopeController(family="hds")
        scope.query = lambda node: (asked.append(node), "")[1]
        scope.get_channel_count()
        scope.get_channel_count()
        self.assertEqual(3, len(asked))


class AutoFrameTests(unittest.TestCase):
    """AUTO has no instrument command, so it is computed - and must be right."""

    def test_nearest_timebase_compares_by_ratio(self):
        scope = OWONScopeController(family="hds")
        self.assertEqual("200us", scope.nearest_timebase(2.4e-4))
        self.assertEqual("500us", scope.nearest_timebase(4.5e-4))
        self.assertIsNone(scope.nearest_timebase(0))

    def test_frame_from_trace_measures_a_square_wave(self):
        scope = OWONScopeController(family="hds")
        scope.waveform.channels = [dict(
            name="CH1", point_interval=1e-4,
            waveform=[-1.0] * 5 + [1.0] * 5 + [-1.0] * 5 + [1.0] * 5)]
        trace = scope.frame_from_trace(1)
        # Two rising edges 10 samples apart at 100us is one period: 1kHz.
        self.assertAlmostEqual(trace["frequency"], 1000.0, delta=1.0)
        self.assertAlmostEqual(trace["vpp"], 2.0, places=6)
        self.assertAlmostEqual(trace["vmean"], 0.0, places=6)

    def test_frame_from_trace_survives_a_flat_trace(self):
        scope = OWONScopeController(family="hds")
        scope.waveform.channels = [dict(
            name="CH1", point_interval=1e-4, waveform=[0.5] * 20)]
        trace = scope.frame_from_trace(1)
        self.assertNotIn("frequency", trace)
        self.assertAlmostEqual(trace["vpp"], 0.0, places=6)

    def test_frame_from_trace_without_a_capture_is_empty(self):
        scope = OWONScopeController(family="hds")
        scope.waveform.channels = []
        self.assertEqual({}, scope.frame_from_trace(1))


class LevelFormatTests(unittest.TestCase):
    def test_levels_are_spelled_with_a_unit(self):
        self.assertEqual("0mV", OWONScopeController.format_level(0.0))
        self.assertEqual("500mV", OWONScopeController.format_level(0.5))
        self.assertEqual("2.50V", OWONScopeController.format_level(2.5))
        self.assertIsNone(OWONScopeController.format_level(None))


class TimeAxisTests(unittest.TestCase):
    """The captures are in seconds; the axis must say so."""

    def test_unit_is_chosen_from_the_capture_span(self):
        from main import App
        self.assertEqual((1e6, "us"), App.time_axis_units(6e-6))
        self.assertEqual((1e3, "ms"), App.time_axis_units(6e-3))
        self.assertEqual((1.0, "s"), App.time_axis_units(6.0))
        self.assertEqual((1.0, "s"), App.time_axis_units(0))


class PanelTestCase(unittest.TestCase):
    """A live panel backed by a mocked controller, with no hardware attached."""

    def setUp(self):
        from unittest.mock import Mock
        from main import App
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root)
        self.scope = Mock(spec=OWONScopeController)
        self.scope.is_connected = True
        for name in ("VOLTAGE_SCALES", "TIMEBASE_SCALES", "PROBE_ATTEN", "COUPLING_MODES",
                     "ACQ_TYPES", "AVG_COUNTS", "MEMORY_DEPTHS", "TRIGGER_SLOPES",
                     "TRIGGER_MODES", "TRIGGER_SOURCES", "TRIGGER_COUPLING"):
            setattr(self.scope, name, getattr(OWONScopeController, name))
        # A spec'd Mock mocks the static helpers too, and the real code calls them.
        self.scope.parse_scale = OWONScopeController.parse_scale
        self.scope.waveform = Mock()
        self.scope.waveform.channels = []
        self.app.scope = self.scope

    def tearDown(self):
        # A window left alive stays Tk's default root, and the next test's photo
        # images would then be created in this one's interpreter instead of its
        # own, which Tk reports as "image ... doesn't exist".
        try:
            self.app.close_panel()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


class ChannelHidingTests(PanelTestCase):
    """A one-channel instrument must not leave a dead CH2 column on screen."""

    def measurement_sources(self):
        values = self.app.meas_source.cget("values")
        if isinstance(values, str):
            values = values.replace("{", "").replace("}", "").split()
        return tuple(values)

    def test_second_channel_column_is_hidden_when_absent(self):
        self.scope.get_channel_count.return_value = 1
        self.app.apply_channel_count()
        # The count is probed on the worker and the columns follow when it lands.
        self.assertTrue(self.app.wait_for_instrument())
        self.assertTrue(self.app._channel_frames[1].winfo_manager())
        self.assertFalse(self.app._channel_frames[2].winfo_manager())
        self.assertEqual(("CH1",), self.measurement_sources())

    def test_second_channel_column_returns_for_a_two_channel_instrument(self):
        self.scope.get_channel_count.return_value = 2
        self.app.apply_channel_count()
        self.assertTrue(self.app.wait_for_instrument())
        self.assertTrue(self.app._channel_frames[1].winfo_manager())
        self.assertTrue(self.app._channel_frames[2].winfo_manager())
        self.assertEqual(("CH1", "CH2"), self.measurement_sources())

    def test_disconnected_panel_shows_one_channel_without_probing(self):
        # Nothing attached means the count is unknown, and an unprobed panel is
        # not allowed to grow a column for a channel that may not exist.
        self.scope.is_connected = False
        self.app.apply_channel_count()
        self.assertFalse(self.app._channel_frames[2].winfo_manager())
        self.scope.get_channel_count.assert_not_called()

    def test_position_knob_writes_through_without_a_set_button(self):
        self.scope.get_timebase_scale.return_value = "500us"
        self.app.timebase_offset.delete(0, "end")
        self.app.timebase_offset.insert(0, "2")
        self.app.set_timebase_position()
        self.assertTrue(self.app.wait_for_instrument())
        self.scope.set_timebase_offset.assert_called_once_with("1ms")
        self.assertAlmostEqual(self.app._horizontal_position, 1e-3)

    def test_trigger_knob_moves_the_cursor(self):
        self.app.trigger_level.delete(0, "end")
        self.app.trigger_level.insert(0, "0.75")
        self.app.set_trigger_level()
        self.assertAlmostEqual(self.app._trigger_level_v, 0.75)
        self.assertTrue(self.app.wait_for_instrument())
        self.scope.set_edge_trigger_level.assert_called_once_with(0.75)

    def test_position_shows_above_the_x_axis(self):
        self.app._horizontal_position = 1e-3
        self.app.draw_reference_lines(True)
        self.assertIn("HPOS 1ms", self.app.ax.get_title(loc="right"))

    def test_trigger_cursor_is_drawn_for_a_real_level(self):
        self.app._trigger_level_v = 0.5
        self.app.draw_reference_lines(True)
        colors = [line.get_color() for line in self.app.ax.lines]
        self.assertIn("#ff7b72", colors)

    def test_a_nonsense_level_does_not_break_plotting(self):
        # A reply the parser could not turn into a number must not take the plot
        # down with it.
        self.app._trigger_level_v = "garbage"
        self.app.draw_reference_lines(True)
        self.assertNotIn("#ff7b72", [line.get_color() for line in self.app.ax.lines])


class MultimeterTests(unittest.TestCase):
    """The scope also contains a multimeter; what it answers is what is offered.

    :DMM:CONFigure? answers a literal "error" on this firmware, and the manual
    documents resistance, diode, continuity and capacitance for the series while
    this instrument is silent for all four - so the function in use comes from
    the typed sub-node and the panel only offers what really answers.
    """

    def scope_with(self, replies):
        scope = OWONScopeController(family="hds")
        scope.query = lambda node: replies.get(node.upper(), "")
        return scope

    def test_reading_is_the_dmm_value(self):
        scope = self.scope_with({":DMM:MEAS?": "1.2345e+00"})
        self.assertAlmostEqual(1.2345, scope.get_dmm_reading())

    def test_a_silent_multimeter_reads_as_nothing(self):
        self.assertIsNone(self.scope_with({}).get_dmm_reading())

    def test_active_function_comes_from_the_typed_sub_node(self):
        scope = self.scope_with({":DMM:CONFIGURE:VOLTAGE?": "DC",
                                 ":DMM:CONFIGURE:CURRENT?": "ERROR"})
        self.assertEqual(("VOLTage", "DC"), scope.get_dmm_function())

    def test_current_is_reported_when_that_is_the_one_in_use(self):
        scope = self.scope_with({":DMM:CONFIGURE:VOLTAGE?": "ERROR",
                                 ":DMM:CONFIGURE:CURRENT?": "AC"})
        self.assertEqual(("CURRent", "AC"), scope.get_dmm_function())

    def test_an_unanswered_function_is_not_invented(self):
        self.assertEqual((None, None), self.scope_with({}).get_dmm_function())

    def test_ui_names_map_to_the_documented_nodes(self):
        scope = OWONScopeController(family="hds")
        sent = []
        scope.send_command = sent.append
        scope.query = lambda node: ""
        scope.set_dmm_function("V", "AC")
        scope.set_dmm_function("AMP", "DC")
        self.assertEqual([":DMM:CONFigure:VOLTage AC", ":DMM:CONFigure:CURRent DC"], sent)

    def test_relative_off_reads_as_off_not_as_zero(self):
        scope = self.scope_with({":DMM:REL?": "OFF"})
        self.assertIs(False, scope.get_dmm_relative())

    def test_a_zero_relative_offset_is_not_off(self):
        # :DMM:REL? answers the stored offset once it is engaged, and a zero
        # offset is a value, not a state.
        scope = self.scope_with({":DMM:REL?": "0.0000e+00"})
        self.assertIsNot(False, scope.get_dmm_relative())
        self.assertAlmostEqual(0.0, scope.get_dmm_relative())

    def test_capabilities_record_what_the_instrument_answers(self):
        scope = self.scope_with({":DMM:MEAS?": "0.0000e+00",
                                 ":DMM:CONFIGURE:VOLTAGE?": "DC",
                                 ":DMM:CONFIGURE:CURRENT?": "ERROR",
                                 ":DMM:REL?": "OFF"})
        caps = scope.dmm_capabilities()
        self.assertTrue(caps["reading"])
        self.assertTrue(caps["relative"])
        self.assertEqual("DC", caps["typed"]["VOLTage"])
        self.assertIsNone(caps["typed"]["CURRent"])
        self.assertEqual(["RESistance", "DIODe", "CONTinuity", "CAPacitance"], caps["silent"])


class MultimeterPanelTests(PanelTestCase):
    """What the panel shows, and what it refuses to claim."""

    def test_reading_is_shown_with_its_unit(self):
        self.scope.get_dmm_reading.return_value = 1.2345
        self.app._dmm_caption = "V DC"
        self.app.poll_dmm()
        self.assertTrue(self.app.wait_for_instrument())
        self.assertEqual("1.2345 V DC", self.app.dmm_value.get())

    def test_relative_is_shown_beside_the_reading(self):
        self.scope.get_dmm_reading.return_value = 0.0
        self.app._dmm_caption = "V DC"
        self.app.dmm_relative.set(True)
        self.app.poll_dmm()
        self.assertTrue(self.app.wait_for_instrument())
        self.assertEqual("REL 0.0000 V DC", self.app.dmm_value.get())

    def test_a_nonsense_reply_never_reaches_the_display(self):
        from unittest.mock import Mock
        self.scope.get_dmm_reading.return_value = Mock()
        self.app.poll_dmm()
        self.assertTrue(self.app.wait_for_instrument())
        self.assertEqual("—", self.app.dmm_value.get())

    def test_a_disconnected_multimeter_shows_nothing(self):
        self.scope.get_dmm_reading.reset_mock()
        self.scope.is_connected = False
        self.app.poll_dmm()
        self.assertTrue(self.app.wait_for_instrument())
        self.assertEqual("—", self.app.dmm_value.get())
        self.scope.get_dmm_reading.assert_not_called()

    def test_an_unconfirmed_function_change_is_reported(self):
        # Voltage/current switching is accepted and ignored by this firmware, so
        # the panel says what the instrument reports instead of what was asked.
        self.scope.set_dmm_function.return_value = ("VOLTage", "DC")
        self.app.dmm_function.set("AMP")
        self.app.dmm_type.set("DC")
        self.app.set_dmm_function()
        self.assertIn("asked for AMP DC", self.app.dmm_note.get())
        self.assertIn("instrument reports VOLTage DC", self.app.dmm_note.get())

    def test_a_type_change_that_was_ignored_is_reported(self):
        # The AC/DC switch is accepted and ignored on this firmware too: the
        # function matches and the type does not, and that is not a success.
        self.scope.set_dmm_function.return_value = ("VOLTage", "DC")
        self.app.dmm_function.set("VOLT")
        self.app.dmm_type.set("AC")
        self.app.set_dmm_function()
        self.assertIn("asked for VOLT AC, instrument reports VOLTage DC", self.app.dmm_note.get())

    def test_a_function_change_that_landed_is_not_flagged(self):
        self.scope.set_dmm_function.return_value = ("CURRent", "DC")
        self.app.dmm_function.set("AMP")
        self.app.dmm_type.set("DC")
        self.app.set_dmm_function()
        self.assertEqual("", self.app.dmm_note.get())
        self.assertEqual("A DC", self.app._dmm_caption)

    def test_relative_engaging_a_zero_offset_counts_as_on(self):
        self.scope.set_dmm_relative.return_value = 0.0
        self.app.dmm_relative.set(False)
        self.app.toggle_dmm_relative()
        self.assertTrue(self.app.dmm_relative.get())
        self.assertEqual("", self.app.dmm_note.get())

    def test_relative_that_did_not_engage_is_reported(self):
        self.scope.set_dmm_relative.return_value = False
        self.app.dmm_relative.set(False)
        self.app.toggle_dmm_relative()
        self.assertFalse(self.app.dmm_relative.get())
        self.assertIn("REL did not engage", self.app.dmm_note.get())

    def test_connecting_reads_the_multimeter_state(self):
        self.scope.dmm_capabilities.return_value = {"reading": True, "typed": {}, "silent": []}
        self.scope.get_dmm_function.return_value = ("VOLTage", "DC")
        self.scope.get_dmm_relative.return_value = False
        self.app.sync_dmm()
        self.assertTrue(self.app.wait_for_instrument())
        self.assertEqual("VOLT", self.app.dmm_function.get())
        self.assertEqual("DC", self.app.dmm_type.get())
        self.assertEqual("V DC", self.app._dmm_caption)


class CachedHeaderTests(unittest.TestCase):
    """A capture header is a third of a frame, so a live loop keeps the one it has."""

    def scope_counting_headers(self):
        scope = OWONScopeController(family="hds")
        scope.is_connected = True
        scope.headers = []
        scope._capture_header = lambda: (scope.headers.append(1), {"HEAD": 1})[1]
        scope.query_binary = lambda node, timeout=5: b""
        return scope

    def test_the_first_capture_reads_the_header(self):
        scope = self.scope_counting_headers()
        scope.download_waveform_data(reuse_header=True)
        self.assertEqual(1, len(scope.headers))

    def test_a_later_live_capture_keeps_it(self):
        scope = self.scope_counting_headers()
        scope.download_waveform_data(reuse_header=True)
        scope.download_waveform_data(reuse_header=True)
        self.assertEqual(1, len(scope.headers))

    def test_a_capture_the_user_asked_for_re_reads_it(self):
        scope = self.scope_counting_headers()
        scope.download_waveform_data(reuse_header=True)
        scope.download_waveform_data()
        self.assertEqual(2, len(scope.headers))

    def test_a_write_drops_the_cached_header(self):
        scope = OWONScopeController(family="hds")
        scope._cached_header = {"HEAD": 1}
        scope.legacy_send_command = lambda command: True
        scope.send_command(":CH1:SCALe 2e-01")
        self.assertIsNone(scope._cached_header)

    def test_a_query_leaves_it_alone(self):
        scope = OWONScopeController(family="hds")
        scope._cached_header = {"HEAD": 1}
        scope.legacy_send_command = lambda command: True
        scope.send_command(":CH1:SCALe?")
        self.assertIsNotNone(scope._cached_header)


class LiveCaptureTests(PanelTestCase):
    """The live loop asks for the fast capture; a manual one asks for a fresh header."""

    def capture_with(self, **kwargs):
        """Ask for a capture, then let the instrument worker hand it over.

        The app no longer returns a frame from the call: the work happens off the
        Tk thread and the result is applied when it lands, so a test drives that
        same path rather than a parallel one.
        """
        self.app.download_waveform(**kwargs)
        self.assertTrue(self.app.wait_for_instrument(), "the capture never reported back")

    def test_live_frames_reuse_the_header(self):
        # Reusing the header is the fast path; the volts still get calibrated on
        # the frames LIVE_CALIBRATE_EVERY picks, so the decode does not depend on
        # the header's volts/div label staying in step with the gain.
        self.capture_with(reuse_header=True)
        self.scope.download_waveform_data.assert_called_once_with(
            reuse_header=True, calibrate=True)

    def test_a_later_live_frame_rides_on_the_remembered_slope(self):
        # The fast path: with a frame count that is not a multiple of the interval,
        # a live frame reuses both the header and the slope already in hand, and the
        # measurement block is not read again.
        self.scope._calibrated_volts_per_code = 0.0195
        self.app._live_frames = 3                 # not a multiple of the interval
        self.capture_with(reuse_header=True)
        self.scope.download_waveform_data.assert_called_once_with(
            reuse_header=True, calibrate=False)

    def test_a_manual_capture_gets_a_fresh_header_and_a_calibration(self):
        self.capture_with()
        self.scope.download_waveform_data.assert_called_once_with(
            reuse_header=False, calibrate=True)


class ScopeNameTests(PanelTestCase):
    """The big header label names the unit that is actually on the cable.

    It used to read "MODERN LAB", which tells an operator nothing about whether a scope
    is attached, let alone which one - and the answer has to come from the instrument,
    because the same app is used with an HDS271 and an HDS272.
    """

    def label(self):
        return self.app.brand_label.cget("text")

    def test_it_starts_unconnected(self):
        # Constructed before anything is attached, which is how the app opens.
        self.assertEqual("Unconnected Scope", self.label())

    def test_nothing_attached_says_so(self):
        self.scope.is_connected = False
        self.app.show_scope_name()
        self.assertEqual("Unconnected Scope", self.label())

    def test_a_connected_scope_names_itself(self):
        self.scope.is_connected = True
        self.scope.model = "HDS272"
        self.app.show_scope_name()
        self.assertEqual("HDS272", self.label())

    def test_the_name_comes_from_the_instrument_not_the_requested_family(self):
        # The app is built for the HDS dialect and asks no further; the model in the
        # header is what the unit reports about itself, so a differently numbered scope
        # is not mislabelled as the one this code was tested against.
        self.scope.model = "HDS272"
        self.app.show_scope_name()
        self.assertEqual("HDS272", self.label())

    def test_a_scope_that_does_not_name_itself_is_not_a_model(self):
        self.scope.is_connected = True
        self.scope.model = ""
        self.app.show_scope_name()
        self.assertEqual("Connected Scope", self.label())

    def test_losing_the_link_goes_back_to_unconnected(self):
        self.scope.model = "HDS272"
        self.app.show_scope_name()
        self.scope.is_connected = False
        self.app.show_scope_name()
        self.assertEqual("Unconnected Scope", self.label())

    def test_the_window_title_says_the_same_thing(self):
        # Same source, so the taskbar and the header cannot disagree about what is
        # attached - and an operator reading either one is told the same thing.
        self.scope.model = "HDS272"
        self.app.show_scope_name()
        self.assertEqual("HDS272", self.app.root.title())
        self.scope.is_connected = False
        self.app.show_scope_name()
        self.assertEqual("Unconnected Scope", self.app.root.title())

    def test_the_window_title_starts_unconnected(self):
        self.assertEqual("Unconnected Scope", self.app.root.title())

    def test_the_state_indicator_drives_it(self):
        # Every path that connects or drops goes through the state indicator, so the
        # name follows the link rather than each path remembering to update it.
        self.scope.model = "HDS272"
        self.app.update_state_indicator()
        self.assertEqual("HDS272", self.label())
        self.scope.is_connected = False
        self.app.update_state_indicator()
        self.assertEqual("Unconnected Scope", self.label())


class ChoiceListTests(PanelTestCase):
    """The HDS series takes different modes and depths from the SDS dialect."""

    def test_hds_lists_come_from_the_manual(self):
        scope = OWONScopeController(family="hds")
        self.assertEqual(["SAMPle", "PEAK"], scope.acquire_mode_choices())
        self.assertEqual(["4K", "8K"], scope.memory_depth_choices())

    def test_the_sds_lists_are_left_alone(self):
        scope = OWONScopeController(family="sds")
        self.assertEqual(list(OWONScopeController.ACQ_TYPES), scope.acquire_mode_choices())
        self.assertEqual(list(OWONScopeController.MEMORY_DEPTHS), scope.memory_depth_choices())
        self.assertNotEqual(["4K", "8K"], scope.memory_depth_choices())

    def test_a_controller_that_cannot_answer_falls_back(self):
        # A spec'd Mock returns a Mock from these, and the panel still has to build.
        self.assertEqual(list(OWONScopeController.ACQ_TYPES),
                         self.app.choices("acquire_mode_choices", "ACQ_TYPES"))


class DialectFallbackTests(unittest.TestCase):
    """The HDS overrides replace the originals, being in the same class.

    A fallback written as ``OWONScopeController.query(self, ...)`` therefore
    resolves back to the override and recurses until the stack dies, which is
    what every non-HDS instrument used to do. The aliases exist so it cannot.
    """

    OVERRIDDEN = ("query", "send_command", "query_binary", "download_waveform_data",
                  "set_trigger_slope", "get_trigger_slope",
                  "set_trigger_coupling", "get_trigger_coupling")

    def test_aliases_point_somewhere_other_than_the_overrides(self):
        for name in self.OVERRIDDEN:
            with self.subTest(name=name):
                self.assertIsNot(getattr(OWONScopeController, "legacy_" + name),
                                 getattr(OWONScopeController, name))

    def test_a_serial_query_uses_the_original_implementation(self):
        scope = OWONScopeController(family="sds")
        scope.legacy_query = lambda command: "IDN:" + command
        self.assertEqual("IDN:*IDN?", scope.query_serial("*IDN?"))

    def test_a_binary_query_falls_back_without_recursing(self):
        scope = OWONScopeController(family="sds")
        scope.legacy_query_binary = lambda command, timeout=None: "bin:" + command
        self.assertEqual("bin:X?", scope.query_binary("X?"))

    def test_download_falls_back_without_recursing(self):
        scope = OWONScopeController(family="sds")
        scope.legacy_download_waveform_data = lambda: "legacy"
        self.assertEqual("legacy", scope.download_waveform_data())

    def test_trigger_helpers_fall_back_without_recursing(self):
        scope = OWONScopeController(family="sds")
        scope.legacy_get_trigger_slope = lambda: "RISe"
        scope.legacy_get_trigger_coupling = lambda: "DC"
        scope.legacy_set_trigger_slope = lambda slope: "slope:" + slope
        scope.legacy_set_trigger_coupling = lambda coupling: "coupling:" + coupling
        self.assertEqual("RISe", scope.get_trigger_slope())
        self.assertEqual("DC", scope.get_trigger_coupling())
        self.assertEqual("slope:RISE", scope.set_trigger_slope("RISE"))
        self.assertEqual("coupling:AC", scope.set_trigger_coupling("AC"))

    def test_an_hds_scope_never_reaches_the_aliases(self):
        scope = OWONScopeController(family="hds")
        scope.legacy_get_trigger_slope = lambda: "legacy"
        scope.query = lambda node: "FALL"
        self.assertEqual("FALL", scope.get_trigger_slope())


class TriggerStatusTests(unittest.TestCase):
    """Both documented trigger queries answer without capturing a frame."""

    def scope_with(self, replies):
        scope = OWONScopeController(family="hds")
        scope.query = lambda node: replies.get(node.upper(), "")
        return scope

    def test_sweep_is_matched_to_the_documented_words(self):
        self.assertEqual("AUTO", self.scope_with({":TRIGGER:SINGLE:SWEEP?": "AUTo"}).get_trigger_sweep())

    def test_status_is_reported_as_answered(self):
        self.assertEqual("TRIG", self.scope_with({":TRIGGER:STATUS?": "TRIG"}).get_trigger_status())

    def test_a_silent_trigger_reports_nothing(self):
        self.assertIsNone(self.scope_with({}).get_trigger_status())


class AutoFrameReportTests(unittest.TestCase):
    """AUTO must aim the trigger with a number it can stand behind, and must not
    write the vertical framing.

    The decoded trace is preferred, and it is now decoded from the same codes and
    the same volts/div LABEL the instrument's own figures use, so the two agree by
    construction and a disagreement means a qualified or stale reading. The
    vertical scale is reported, never written: on this firmware a volts/div write
    is accepted, reads back, and changes nothing but the label (writing a position
    shift of one label-division moved the samples by 0 codes), which would leave
    the instrument reporting every signal at the label's ratio of its real size.
    """

    def framed_scope(self, trace, measured):
        scope = OWONScopeController(family="hds")
        scope.written = []
        scope._timebase = "500us"
        scope.frame_from_trace = lambda channel: trace
        scope.get_measurements_numeric = lambda channel=1: measured
        scope.get_timebase_scale = lambda: scope._timebase
        scope.set_timebase_scale = lambda value: (scope.written.append(("timebase", value)),
                                                  setattr(scope, "_timebase", value))
        scope.set_trigger_level = lambda value: scope.written.append(("level", value))
        scope.get_channel_scale = lambda channel: "500mv"
        scope.set_channel_scale = lambda channel, value: scope.written.append(("scale", value))
        scope.get_channel_probe = lambda channel: "10X"
        scope.query = lambda node: "0.00mV"
        scope.download_waveform_data = lambda **kwargs: True
        return scope

    def levels(self, scope):
        return [value for kind, value in scope.written if kind == "level"]

    def scales(self, scope):
        return [value for kind, value in scope.written if kind == "scale"]

    def test_a_wildly_out_of_range_trace_is_reported_and_not_written(self):
        # 265 V from the decode against 25.6 V from the instrument: a come-apart.
        # The note says so, nothing is aimed at the trigger, and the vertical
        # suggestion is withheld rather than derived from either reading.
        scope = self.framed_scope({"frequency": 1000.0, "vpp": 265.0, "vmean": -2.26},
                                  {"Vpp": 25.6, "Vmax": 12.8, "Vmin": -12.8, "Vmean": 0.008})
        report = scope.auto_frame()
        self.assertEqual([], self.levels(scope))
        self.assertTrue(any("the instrument measures" in note for note in report["notes"]))
        self.assertTrue(any("cannot be reconciled" in note or "disagree" in note
                            for note in report["notes"]))
        self.assertIsNone(report["vertical_scale"])

    def test_the_vertical_scale_is_never_written(self):
        # The whole point: report the framing, hand it to the front panel.
        scope = self.framed_scope({"frequency": 1000.0, "vpp": 2.0, "vmean": 0.5},
                                  {"Vpp": 2.05, "Vmax": 1.0, "Vmin": -1.0, "Vmean": 0.51})
        report = scope.auto_frame()
        self.assertEqual([], self.scales(scope))
        # A suggestion is still reported, chosen from the decoded amplitude: 2 V
        # over 70% of eight divisions, converted to the connector figure a 10X
        # probe means.
        self.assertEqual("50mv", report["vertical_scale"])
        self.assertTrue(any("front-panel setting" in note for note in report["notes"]))

    def test_agreeing_sources_keep_the_decoded_trace(self):
        scope = self.framed_scope({"frequency": 1000.0, "vpp": 2.0, "vmean": 0.5},
                                  {"Vpp": 2.05, "Vmax": 1.0, "Vmin": -1.0, "Vmean": 0.51})
        report = scope.auto_frame()
        self.assertEqual("decoded trace", report["measured_from"])
        self.assertEqual(["500mV"], self.levels(scope))
        self.assertEqual([], [n for n in report["notes"] if "spans" in n])

    def test_a_reading_the_instrument_qualifies_is_flagged_not_trusted(self):
        # A gap wider than the trace's own thickness means one of the two is not
        # describing the signal, so it is called out for the user to check against
        # the front panel rather than silently believed - and the trigger is left
        # alone, since one of the two readings contradicts any level taken from the
        # other.
        scope = self.framed_scope({"frequency": 1000.0, "vpp": 25.0, "vmean": 0.5},
                                  {"Vpp": 4.46, "Vmax": 2.2, "Vmin": -2.2, "Vmean": 0.04})
        report = scope.auto_frame()
        self.assertEqual("decoded trace", report["measured_from"])
        self.assertTrue(any("the instrument measures" in note for note in report["notes"]))
        self.assertTrue(any("front panel" in note for note in report["notes"]))
        self.assertEqual([], self.levels(scope))

    def test_a_level_outside_the_measured_span_is_not_written(self):
        scope = self.framed_scope({"frequency": 1000.0, "vpp": 2.0, "vmean": 5.0},
                                  {"Vpp": 2.0, "Vmax": 1.0, "Vmin": -1.0, "Vmean": 5.0})
        report = scope.auto_frame()
        self.assertEqual([], self.levels(scope))
        self.assertTrue(any("outside the measured span" in note for note in report["notes"]))

    def test_a_confirmed_timebase_is_reported_as_applied(self):
        scope = self.framed_scope({"frequency": 1000.0, "vpp": 2.0, "vmean": 0.0},
                                  {"Vpp": 2.0, "Vmax": 1.0, "Vmin": -1.0, "Vmean": 0.0})
        report = scope.auto_frame()
        self.assertEqual("200us", report["timebase"])
        self.assertIn("timebase", report["applied"])

    def test_an_unconfirmed_level_is_not_reported_as_applied(self):
        scope = self.framed_scope({"frequency": 1000.0, "vpp": 2.0, "vmean": 0.5},
                                  {"Vpp": 2.0, "Vmax": 1.0, "Vmin": -1.0, "Vmean": 0.5})
        scope.query = lambda node: "4293V"
        report = scope.auto_frame()
        self.assertNotIn("trigger level", report["applied"])
        self.assertTrue(any("4293V" in note for note in report["notes"]))


class WorkerStartupTests(PanelTestCase):
    """A worker that cannot start must not leave the panel refusing input.

    Every control is gated on the busy flag, so the AUTO command that referred to
    an unimported module did not merely do nothing: it left the whole panel
    wedged until the window was reopened.
    """

    def test_a_thread_that_cannot_start_releases_the_panel(self):
        from unittest.mock import patch

        def refuse(*args, **kwargs):
            raise RuntimeError("no threads today")

        self.app._busy = True
        with patch.object(modern_lab.threading, "Thread", refuse):
            started = self.app.start_worker(lambda: None, "AUTO")
        self.assertFalse(started)
        self.assertFalse(self.app._busy)
        self.assertIn("Could not start AUTO", self.app.log_text.get("1.0", "end"))


class AutoFrameButtonTests(PanelTestCase):
    """Pressing AUTO runs the framing and says what it did."""

    def test_the_button_runs_the_framing_and_reports_it(self):
        self.scope.auto_frame.return_value = {"applied": ["timebase"], "timebase": "200us",
                                             "notes": ["a note"], "trigger_level": None}
        self.app.action(self.app.auto_frame)
        self.assertTrue(self.app.wait_for_instrument())
        self.assertFalse(self.app._busy)
        log = self.app.log_text.get("1.0", "end")
        self.assertIn("AUTO set time/div 200us", log)
        self.assertIn("AUTO: a note", log)

    def test_the_panel_follows_the_framing_auto_applied(self):
        # Otherwise the controls still show the framing AUTO just replaced.
        self.scope.auto_frame.return_value = {"applied": ["timebase", "trigger level"],
                                             "timebase": "200us", "trigger_level": "481mV",
                                             "trigger_level_readback": "480mV", "notes": []}
        self.app.action(self.app.auto_frame)
        self.assertTrue(self.app.wait_for_instrument())
        self.assertEqual("200us", self.app._vars["timebase_scale"].get())
        self.assertEqual("0.48", self.app.trigger_level.get())

    def test_an_unconfirmed_level_never_reaches_the_trigger_box(self):
        # The box is a live control: putting an unconfirmed readback in it would
        # have the panel write that value straight back to the instrument.
        self.scope.auto_frame.return_value = {"applied": [], "timebase": "200us",
                                             "trigger_level_readback": "4293V", "notes": []}
        before = self.app.trigger_level.get()
        self.app.action(self.app.auto_frame)
        self.assertTrue(self.app.wait_for_instrument())
        self.assertEqual(before, self.app.trigger_level.get())
        self.scope.set_edge_trigger_level.assert_not_called()

    def test_an_auto_that_changed_nothing_says_so(self):
        self.scope.auto_frame.return_value = {"applied": [], "notes": []}
        self.app.action(self.app.auto_frame)
        self.assertTrue(self.app.wait_for_instrument())
        self.assertIn("AUTO could not change anything", self.app.log_text.get("1.0", "end"))

    def test_a_failing_auto_releases_the_panel_and_logs_it(self):
        self.scope.auto_frame.side_effect = RuntimeError("transport went away")
        self.app.action(self.app.auto_frame)
        self.assertTrue(self.app.wait_for_instrument())
        self.assertFalse(self.app._busy)
        self.assertIn("transport went away", self.app.log_text.get("1.0", "end"))


class AutoReadbackTests(PanelTestCase):
    """AUTO leaves every control showing what the instrument is actually set to.

    The framing readback alone left the trigger, acquisition and channel controls
    showing whatever they held before AUTO - a panel that had stopped describing
    the instrument it was talking to. The readback that fixes it is written with
    live application off: a value the instrument has just given back must never
    be re-sent as a write, or the control fights the instrument.
    """

    #: What the instrument answers, one entry per control under test. Values that
    #: differ from the panel's defaults, so a control that was not written is
    #: plainly visible as one.
    REPLIES = {
        "get_trigger_mode": "NORMal",
        "get_trigger_coupling": "HF",
        "get_trigger_source": "CH2",
        "get_trigger_slope": "FALL",
        "get_trigger_level_volts": 0.25,
        "get_acquire_type": "PEAK",
        "get_memory_depth": "8K",
        "get_channel_scale": "500mv",
        "get_channel_coupling": "AC",
        "get_channel_display": "ON",
    }

    def instrument_answers(self, **overrides):
        """Point every getter the readback uses at a setting of its own."""
        for name, value in dict(self.REPLIES, **overrides).items():
            getattr(self.scope, name).return_value = value
        self.scope.get_channel_count.return_value = 1
        return self.scope

    def run_auto(self):
        self.scope.auto_frame.return_value = {"applied": [], "notes": []}
        self.app.action(self.app.auto_frame)
        self.assertTrue(self.app.wait_for_instrument())

    def test_every_control_follows_the_instrument_after_auto(self):
        self.instrument_answers()
        self.run_auto()
        self.assertEqual("NORMal", self.app.trigger_mode.get())
        self.assertEqual("HF", self.app.trigger_coupling.get())
        self.assertEqual("CH2", self.app.trigger_source.get())
        self.assertEqual("FALL", self.app.trigger_slope.get())
        self.assertEqual("0.25", self.app.trigger_level.get())
        self.assertEqual("PEAK", self.app.acq_type.get())
        self.assertEqual("8K", self.app.mem_depth.get())
        self.assertEqual("500mv", self.app.ch1_scale.get())
        self.assertEqual("AC", self.app.ch1_coupling.get())
        self.assertTrue(self.app.ch1_display.get())

    def test_the_second_channel_is_read_back_too(self):
        # Only the channels the panel is showing are read, so show two first.
        self.instrument_answers()
        self.app.show_channels(2)
        self.scope.get_channel_scale.side_effect = lambda number: ("2v" if number == 2 else "500mv")
        self.scope.get_channel_coupling.side_effect = lambda number: ("GND" if number == 2 else "AC")
        self.run_auto()
        self.assertEqual("500mv", self.app.ch1_scale.get())
        self.assertEqual("2v", self.app.ch2_scale.get())
        self.assertEqual("GND", self.app.ch2_coupling.get())

    def test_a_column_that_is_not_on_screen_is_not_read(self):
        # This firmware answers for more channels than it has, and a column that
        # is hidden has no control to fill: reading it is round trips for nothing.
        self.instrument_answers()
        self.app.show_channels(1)
        self.run_auto()
        self.scope.get_channel_scale.assert_called_once_with(1)
        self.scope.get_channel_coupling.assert_called_once_with(1)

    def test_a_readback_writes_nothing_back_to_the_instrument(self):
        # The whole point of the guard: these controls apply what they hold, so a
        # value straight off the instrument would be sent back as a write.
        self.instrument_answers()
        self.run_auto()
        for name in dir(self.scope):
            if name.startswith("set_"):
                getattr(self.scope, name).assert_not_called()
        # And the readback did not quietly queue a live write to be fired later,
        # which is the other way a control can turn a readback into a write: the
        # guard is dropped by then, so the queued call would go straight out.
        self.assertEqual({}, self.app._live_apply)

    def test_a_readback_that_lands_in_the_live_trigger_box_writes_nothing(self):
        # The trigger level is the live control of the set: it applies what it
        # holds as soon as it settles, so this one is asserted on its own.
        self.instrument_answers()
        self.run_auto()
        self.scope.set_edge_trigger_level.assert_not_called()
        # Nor did the readback queue a live write of its own behind the guard.
        self.app.apply_live("trigger_level", self.app.set_trigger_level)
        self.scope.set_edge_trigger_level.assert_not_called()

    def test_a_silent_node_leaves_its_control_alone(self):
        # The HDS does not answer :CHn:DISPlay?, and an empty reply read as "off"
        # would untick a channel that is plainly on screen.
        self.instrument_answers(get_channel_display="")
        self.app.ch1_display.set(True)
        self.run_auto()
        self.assertTrue(self.app.ch1_display.get())

    def test_a_failing_readback_is_reported_and_changes_nothing(self):
        self.scope.get_trigger_mode.side_effect = RuntimeError("bus went quiet")
        self.scope.auto_frame.return_value = {"applied": [], "notes": []}
        before = self.app.trigger_mode.get()
        self.app.action(self.app.auto_frame)
        self.assertTrue(self.app.wait_for_instrument())
        self.assertEqual(before, self.app.trigger_mode.get())
        self.scope.set_trigger_mode.assert_not_called()

    def test_the_framing_readback_does_not_queue_a_write_either(self):
        # The framing part of AUTO writes the confirmed level into the same live
        # box as the readback, and it has to be under the guard too: this
        # firmware's level readback ("4293V") landing in that box and going out
        # as a write is what "the panel fights the instrument" is.
        self.instrument_answers()
        self.scope.auto_frame.return_value = {
            "applied": ["timebase", "trigger level"], "timebase": "200us",
            "trigger_level": "-2.04V", "trigger_level_readback": "4293V", "notes": []}
        self.app.action(self.app.auto_frame)
        self.assertTrue(self.app.wait_for_instrument())
        self.assertEqual("0.25", self.app.trigger_level.get())
        self.assertEqual({}, self.app._live_apply)
        self.scope.set_edge_trigger_level.assert_not_called()

    def test_a_readback_never_reaches_the_instrument_from_the_panel_loop(self):
        # What the instrument reported and the panel now shows must survive the
        # live loop: a write queued behind the readback would put the old value
        # back on the instrument.
        self.instrument_answers()
        self.run_auto()
        self.app.set_trigger_level()
        self.assertTrue(self.app.wait_for_instrument())
        self.scope.set_edge_trigger_level.assert_called_once_with(0.25)

    def test_the_new_trigger_coupling_control_still_writes_when_the_user_picks(self):
        # The readback must not have left the new control read-only in effect:
        # a control that reports the instrument but cannot set it is the other
        # half of the same problem.
        self.instrument_answers()
        self.run_auto()
        self.app.trigger_coupling.set("LF")
        self.app.set_trigger_coupling()
        self.assertTrue(self.app.wait_for_instrument())
        self.scope.set_edge_trigger_coupling.assert_called_once_with("LF")


class MeasurementPanelTests(PanelTestCase):
    """The measured signal and the instrument's own readout are not the same thing.

    The capture is calibrated against a known-amplitude signal; the instrument's
    block is referred to its volts/div and probe labels and can read a fraction of
    the same input at a fine setting, so the two are labelled rather than printed
    side by side as though they agreed.
    """

    def read(self):
        self.app.get_measurements()
        # The instrument's block is read on the worker and written when it lands,
        # which is after the request returns.
        self.assertTrue(self.app.wait_for_instrument())
        return self.app.measurement_text.get("1.0", "end")

    def test_the_calibrated_capture_figures_are_shown_first(self):
        self.scope.frame_from_trace.return_value = {"vpp": 4.93, "vmax": 2.9, "vmin": -2.03,
                                                   "vmean": 0.43, "frequency": 1000.0}
        self.scope.get_all_measurements.return_value = {"Vpp": "8.8400e-01"}
        text = self.read()
        self.assertIn("From the capture (calibrated", text)
        self.assertIn("Vpp: 4.93 V", text)
        self.assertIn("Frequency: 1000 Hz", text)
        # The instrument's own block is still there, and says what it is.
        self.assertIn("From the instrument (referred to its own volts/div and probe labels)",
                      text)
        self.assertIn("Vpp: 8.8400e-01", text)
        self.assertLess(text.index("From the capture"), text.index("From the instrument"))

    def test_a_trace_that_is_not_a_dict_is_ignored(self):
        # A spec'd Mock hands back a Mock, which must not reach a format string.
        self.scope.get_all_measurements.return_value = {"Vpp": "0.5"}
        text = self.read()
        self.assertIn("From the instrument", text)
        self.assertNotIn("From the capture", text)


class VerticalFrameTests(PanelTestCase):
    """The software must draw the scale the capture was decoded at.

    Autoscaling to the data cancels a volts/div change out: the trace keeps the
    same size on screen while the instrument's own display changes, which is the
    "the knob moves the instrument but not the software" report.

    The axis is built from `true_volts_per_div` - the decoded volts per code times
    the codes in a division - not from the instrument's volts/div label. On this
    firmware that label moves without the gain moving, and an axis built from it
    put a measured 25.6 Vpp signal at 275 V.
    """

    def channel(self, volts_per_div, offset_div=0.0, attenuation="10X", units="V",
                true_volts_per_div=None):
        return {"name": "CH1", "units": units, "volts_per_div": volts_per_div,
                "true_volts_per_div": (volts_per_div if true_volts_per_div is None
                                       else true_volts_per_div),
                "volts_per_code_source": "instrument measurements",
                "vertical_offset_div": offset_div, "attenuation": attenuation,
                "point_interval": 1e-4, "waveform": [0.0, 1.0, -1.0]}

    def frame_of(self, *channels):
        self.scope.waveform.channels = list(channels)
        with patch.object(self.app, "sync_vertical_controls"):
            self.app.plot_waveform()
        return self.app.ax.get_ylim()

    def test_the_frame_is_the_instruments_screen(self):
        low, high = self.frame_of(self.channel(5.0))
        half = HDS_VERTICAL_DIVISIONS / 2.0 * 5.0
        self.assertAlmostEqual(-half, low, places=6)
        self.assertAlmostEqual(half, high, places=6)

    def test_a_coarser_volts_div_widens_the_frame(self):
        fine = self.frame_of(self.channel(1.0))
        coarse = self.frame_of(self.channel(5.0))
        self.assertLess(abs(fine[1] - fine[0]), abs(coarse[1] - coarse[0]))

    def test_the_position_label_does_not_move_the_axis(self):
        # The position is a display shift: writing one division of it moved the
        # captured codes by 0, so it cannot move volts.
        moved = self.frame_of(self.channel(5.0, offset_div=1.0))
        centred = self.frame_of(self.channel(5.0))
        self.assertAlmostEqual(0.0, (moved[0] + moved[1]) / 2.0, places=6)
        self.assertAlmostEqual(centred[0], moved[0], places=9)
        self.assertAlmostEqual(centred[1], moved[1], places=9)

    def test_the_position_control_moves_the_trace_and_not_the_grid(self):
        # The panel's position control: a position knob moves the trace across a
        # graticule that is fixed to the screen. Adding the shift to the window as
        # well made the two cancel out, so the trace held still and the grid slid -
        # and a reading taken against a sliding grid is worth nothing.
        channel = self.channel(5.0)
        self.scope.waveform.channels = [channel]
        with patch.object(self.app, "sync_vertical_controls"):
            self.app.plot_waveform()
        low, high = self.app.ax.get_ylim()
        ticks = list(self.app.ax.get_yticks())
        trace = list(self.app.ax.lines[0].get_ydata())        # the plotted trace

        self.app._display_offset[1] = 5.0                      # one row of 5 V/div
        with patch.object(self.app, "sync_vertical_controls"):
            self.app.plot_waveform()

        frame = self.app.ax.get_ylim()
        self.assertAlmostEqual(low, frame[0], places=9)
        self.assertAlmostEqual(high, frame[1], places=9)
        self.assertEqual(ticks, list(self.app.ax.get_yticks()))
        moved = list(self.app.ax.lines[0].get_ydata())
        self.assertAlmostEqual(5.0, moved[0] - trace[0], places=9)
        self.assertAlmostEqual(5.0, moved[1] - trace[1], places=9)
        # And the capture itself is untouched: the position is a view setting,
        # so no measurement or export can come back different for it.
        self.assertEqual([0.0, 1.0, -1.0], list(channel["waveform"]))

    def test_every_enabled_channel_gets_a_zero_marker(self):
        # The point the trace refers to, and the only place an applied position is
        # visible - the grid deliberately does not move with it.
        self.frame_of(self.channel(5.0))
        marks = self.app._zero_marks
        self.assertEqual(1, len(marks))
        self.assertEqual("CH1", marks[0]["name"])
        self.assertAlmostEqual(0.0, marks[0]["offset"], places=9)
        self.assertEqual("CH1 0V", marks[0]["label"])
        # Drawn, not merely remembered.
        self.assertIn("CH1 0V", [text.get_text() for text in self.app.ax.texts])

    def test_a_moved_trace_carries_its_zero_marker_and_says_how_far(self):
        self.scope.waveform.channels = [self.channel(5.0)]
        self.app._display_offset[1] = 5.0
        with patch.object(self.app, "sync_vertical_controls"):
            self.app.plot_waveform()
        self.assertIn("CH1 0V  +5 V  (+1.00 div)",
                      [text.get_text() for text in self.app.ax.texts])
        self.assertAlmostEqual(5.0, self.app._zero_marks[0]["offset"], places=9)
        # And the grid is exactly where it was: one row is still 5 V, so the
        # marker is what tells the two traces apart.
        low, high = self.app.ax.get_ylim()
        self.assertAlmostEqual(-20.0, low, places=9)
        self.assertAlmostEqual(20.0, high, places=9)

    def test_a_channel_with_no_trace_gets_no_marker(self):
        silent = self.channel(5.0)
        silent["waveform"] = []
        self.frame_of(self.channel(5.0), silent)
        self.assertEqual(["CH1"], [mark["name"] for mark in self.app._zero_marks])

    def test_both_zero_labels_sit_at_the_right_edge(self):
        # One place to read them: with CH1 labelled on the left and CH2 on the
        # right, comparing two zero points meant looking at two edges for one
        # number each.
        self.scope.waveform.channels = [self.channel(5.0),
                                             dict(self.channel(5.0), name="CH2")]
        with patch.object(self.app, "sync_vertical_controls"):
            self.app.plot_waveform()
        labels = [text for text in self.app.ax.texts if text.get_text().endswith("0V")]
        self.assertEqual(2, len(labels))
        for label in labels:
            self.assertEqual("right", label.get_ha())
            self.assertEqual(1.0, label.xy[0])

    def test_a_finer_volts_div_draws_a_taller_trace(self):
        # The whole point of framing on the instrument's screen: a row is the
        # selected volts/div, so the same volts must fill more rows at a finer
        # setting. The instrument's own trace grows as the knob goes down; the
        # software must move the same way, not the opposite one.
        data = [-5.0, 5.0]

        def fraction_of_frame(volts_per_div):
            channel = self.channel(volts_per_div)
            channel["waveform"] = data
            low, high = self.frame_of(channel)
            return (max(data) - min(data)) / (high - low)

        self.assertGreater(fraction_of_frame(1.0), fraction_of_frame(10.0))
        self.assertAlmostEqual(10.0, fraction_of_frame(1.0) / fraction_of_frame(10.0),
                               places=6)

    def test_ticks_land_on_the_instruments_divisions(self):
        self.frame_of(self.channel(5.0))
        ticks = self.app.ax.get_yticks()
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        self.assertTrue(gaps)
        for gap in gaps:
            self.assertAlmostEqual(5.0, gap, places=6)

    def test_the_axis_says_which_framing_it_is_drawing(self):
        self.frame_of(self.channel(5.0))
        self.assertIn("5 V/div", self.app.ax.get_ylabel())
        # The probe in use is named too - the one the panel records, not the label
        # the instrument announces, which on this bench says 10X for a 1X probe.
        self.app.set_channel_probe(1, "1X")
        self.frame_of(self.channel(5.0))
        self.assertIn("1X", self.app.ax.get_ylabel())

    def test_a_capture_without_a_scale_falls_back_to_autoscaling(self):
        # Raw codes are not volts, so the instrument's framing cannot be drawn.
        low, high = self.frame_of(self.channel(None, units="codes"))
        self.assertLess(low, 0.0)
        self.assertGreaterEqual(high, 1.0)


class FrontPanelSyncTests(PanelTestCase):
    """A change made on the instrument must reach the panel's own controls."""

    def channel(self, volts_per_div, attenuation="10X", true_volts_per_div=None):
        return {"name": "CH1", "units": "V", "volts_per_div": volts_per_div,
                "true_volts_per_div": (volts_per_div if true_volts_per_div is None
                                       else true_volts_per_div),
                "vertical_offset_div": 0.0, "attenuation": attenuation,
                "point_interval": 1e-4, "waveform": [0.0, 1.0]}

    def test_the_control_follows_the_decoded_scale_not_the_reported_one(self):
        # The control names volts per division, straight - no probe arithmetic,
        # because this instrument's readings are real volts at the BNC whatever
        # probe it announces. What it follows is what the capture DECODED to, never
        # the volts/div the instrument reports, which moves without the gain moving.
        self.scope.waveform.channels = [self.channel(5.0)]
        self.app.plot_waveform()
        self.assertEqual("5v", self.app.ch1_scale.get())

    def test_the_scale_control_moves_the_row_and_leaves_the_reading_alone(self):
        # The property the user asked for, stated as arithmetic: picking a volts/div
        # changes how tall the trace is drawn and what a row is worth, and the two
        # cancel - rows x volts-per-row is the same signal at every setting.
        def reading():
            low, high = self.app.ax.get_ylim()
            row = (high - low) / 8.0
            entry = self.scope.waveform.channels[0]
            ratio = self.app.display_ratio(1, entry)
            data = [v * ratio for v in entry["waveform"]]
            rows = (max(data) - min(data)) / row
            return row, rows, rows * row

        self.scope.waveform.channels = [self.channel(2.0)]
        self.app.plot_waveform()
        auto_row, auto_rows, auto_volts = reading()
        self.assertEqual("2v", self.app.ch1_scale.get())

        self.app.set_channel_scale(1, self.scope.VOLTAGE_SCALES[0])
        fine_row, fine_rows, fine_volts = reading()
        self.assertLess(fine_row, auto_row)                 # a finer row is worth less
        self.assertGreater(fine_rows, auto_rows)            # so the trace fills more rows
        self.assertAlmostEqual(auto_volts, fine_volts, places=6)

        self.app.set_channel_scale(1, "2v")
        coarse_row, coarse_rows, coarse_volts = reading()
        self.assertGreater(coarse_row, fine_row)
        self.assertLess(coarse_rows, fine_rows)
        self.assertAlmostEqual(auto_volts, coarse_volts, places=6)

    def test_the_probe_label_does_not_scale_the_amplitude(self):
        # Measured with a 1X probe on the input: the instrument announced 10X and
        # still read the 25 Vpp signal as 25.6 V, so its volts are the volts at the
        # BNC. Multiplying by the announced probe put every amplitude out by ten.
        self.scope.waveform.channels = [self.channel(20.0)]
        self.app.plot_waveform()
        self.assertEqual(1.0, self.app.display_ratio(1, self.scope.waveform.channels[0]))
        self.app.set_channel_probe(1, "1X")
        self.assertEqual(1.0, self.app.display_ratio(1, self.scope.waveform.channels[0]))
        self.assertIn("1X", self.app.ax.get_ylabel())

    def test_the_selected_volts_per_division_gives_the_divisions_the_user_expects(self):
        # The acceptance figure, as the user stated it: a 25 Vpp signal at 5 V/div is
        # 5 divisions, at 10 V/div is 2.5, at 1 V/div is 25. The grid must show that.
        volts = 25.6
        channel = self.channel(4.29, true_volts_per_div=4.29)
        channel["waveform"] = [-volts / 2, volts / 2]
        self.scope.waveform.channels = [channel]

        def rows_at(selected):
            self.app.set_channel_scale(1, selected)
            low, high = self.app.ax.get_ylim()
            row = (high - low) / 8.0
            data = [v * self.app.display_ratio(1, channel) for v in channel["waveform"]]
            return row, (max(data) - min(data)) / row

        for selected, expected in (("5v", volts / 5), ("10v", volts / 10), ("1v", volts / 1)):
            row, rows = rows_at(selected)
            # The row IS the setting - that is the whole contract - and the trace is
            # the amplitude divided by it.
            self.assertAlmostEqual(OWONScopeController.parse_scale(selected), row, places=9)
            self.assertAlmostEqual(expected, rows, places=6)

    def test_an_off_ladder_scale_snaps_and_the_axis_names_both(self):
        # 20 V/div is not on the control (its ladder stops at 10v), so the row snaps
        # to 10 V/div and the axis says what the capture actually decoded to. The
        # amplitude is unaffected either way - rows x row is the signal at any scale.
        self.scope.waveform.channels = [self.channel(20.0)]
        self.app.plot_waveform()
        self.assertEqual("10v", self.app.ch1_scale.get())
        self.assertIn("20 V/div", self.app.ax.get_ylabel())      # what it decoded to
        self.assertIn("10 V/div", self.app.ax.get_ylabel())      # what it drew with

    def test_a_reported_label_a_long_way_out_does_not_move_the_control(self):
        self.scope.waveform.channels = [
            self.channel(50.0, true_volts_per_div=5.0)]
        self.app.plot_waveform()
        self.assertEqual("5v", self.app.ch1_scale.get())

    def test_a_probe_set_on_the_instrument_reaches_the_control(self):
        self.scope.waveform.channels = [self.channel(0.2, attenuation="1X")]
        self.app.plot_waveform()
        self.assertEqual("X1", self.app.ch1_probe.get())

    def test_following_the_instrument_never_writes_back(self):
        # The control is a control, not a live one: syncing must not send a write.
        self.scope.waveform.channels = [self.channel(2.0)]
        self.app.plot_waveform()
        self.scope.set_channel_scale.assert_not_called()

    def test_a_widget_already_showing_the_value_is_left_alone(self):
        # No churn on the control when it already names the decoded scale: a
        # Combobox rewritten every frame would fight the user's own selection.
        self.scope.waveform.channels = [self.channel(10.0)]
        self.app.ch1_scale.set("10v")
        self.app.plot_waveform()
        self.assertEqual("10v", self.app.ch1_scale.get())
        self.assertNotIn("display scale auto", self.app.log_text.get("1.0", "end"))


class ConnectionFieldTests(PanelTestCase):
    """USB is the default transport, and a LAN address is not shown for it."""

    def test_usb_is_the_default_transport(self):
        self.assertEqual("usb", self.app.conn_type.get())

    def test_the_address_field_is_not_on_screen_for_usb(self):
        self.assertEqual("", self.app.conn_address.winfo_manager())
        self.assertEqual("pack", self.app.conn_hint.winfo_manager())

    def test_choosing_lan_brings_the_address_back(self):
        self.app.conn_type.set("lan")
        self.app.sync_connection_fields()
        self.assertEqual("pack", self.app.conn_address.winfo_manager())
        self.assertEqual("", self.app.conn_hint.winfo_manager())

    def test_choosing_usb_again_takes_it_away(self):
        self.app.conn_type.set("lan")
        self.app.sync_connection_fields()
        self.app.conn_type.set("usb")
        self.app.sync_connection_fields()
        self.assertEqual("", self.app.conn_address.winfo_manager())

    def test_the_address_field_keeps_its_text_across_the_switch(self):
        # Switching back to LAN must not lose the address that was typed.
        self.app.conn_address.delete(0, "end")
        self.app.conn_address.insert(0, "192.168.0.7")
        for value in ("usb", "lan"):
            self.app.conn_type.set(value)
            self.app.sync_connection_fields()
        self.assertEqual("192.168.0.7", self.app.conn_address.get())

    def test_connecting_over_usb_never_uses_the_lan_address(self):
        # The field still holds the LAN default underneath; USB must not see it.
        self.scope.connect_usb.return_value = True
        self.scope.get_idn.return_value = "OWON,HDS271,25520161,V1.3.0"
        self.app.connect_scope()
        # The transport is opened on the worker, so the call is made a moment later.
        self.assertTrue(self.app.wait_for_instrument())
        self.scope.connect_usb.assert_called_once_with("auto", 115200)


class ControlReadbackTests(PanelTestCase):
    """A value read from the instrument has to reach the control that shows it.

    The live controls apply what they hold, so a control left holding something
    else is not merely cosmetic - one detent of it sends that stale value to the
    instrument, which is what a knob that "does not work" looks like from outside.
    """

    CHANNEL = {"name": "CH1", "units": "V", "volts_per_div": 5.0,
               "true_volts_per_div": 5.0, "vertical_offset_div": 0.0,
               "point_interval": 1e-4, "waveform": [0.0, 1.0]}

    def settle_live(self, seconds=0.4):
        """Let a live control's debounce fire, the way a running panel would."""
        import time
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)

    def test_the_trigger_level_reaches_the_knob(self):
        self.app.apply_cursor_readings(1.25, None)
        self.assertEqual("1.25", self.app.trigger_level.get())

    def test_a_readback_is_not_sent_straight_back(self):
        # Long enough for the debounce to fire: the instrument just said this
        # value, so it must not come back as a write.
        self.app.apply_cursor_readings(1.25, None)
        self.settle_live(0.35)
        self.assertTrue(self.app.wait_for_instrument())
        self.scope.set_edge_trigger_level.assert_not_called()

    def test_a_knob_turned_from_a_readback_sends_that_value(self):
        self.app.apply_cursor_readings(-2.0, None)
        # As the panel's knob drives it: 0.1 V detents, applied when it settles.
        self.app.step_number("trigger_level", 1, self.app.set_trigger_level, 0.1, True)
        self.settle_live()
        self.assertTrue(self.app.wait_for_instrument())
        self.scope.set_edge_trigger_level.assert_called_once_with(-1.9)

    def test_an_empty_box_still_turns(self):
        self.app._vars["trigger_level"].set("")
        self.app.step_number("trigger_level", 1, self.app.set_trigger_level, 0.1, True)
        self.assertEqual("0.1", self.app._vars["trigger_level"].get())

    def test_the_position_follows_the_instrument_when_it_moves(self):
        channel = dict(self.CHANNEL, vertical_offset_div=1.0)
        self.app.follow_instrument_position(1, channel)
        self.assertAlmostEqual(5.0, self.app._display_offset[1], places=9)
        self.assertEqual("5", self.app.ch1_offset.get())

    def test_a_position_that_has_not_moved_is_left_alone(self):
        # The readback arrives with every frame; taking it each time would wipe a
        # value set on the panel several times a second.
        self.app.follow_instrument_position(1, dict(self.CHANNEL))
        self.app.set_channel_offset(1, 2.5)
        self.app.follow_instrument_position(1, dict(self.CHANNEL))
        self.assertAlmostEqual(2.5, self.app._display_offset[1], places=9)

    def test_a_position_with_nothing_to_scale_is_ignored(self):
        raw = dict(self.CHANNEL, true_volts_per_div=None, vertical_offset_div=1.0)
        self.app.follow_instrument_position(1, raw)
        self.assertAlmostEqual(0.0, self.app.display_offset(1), places=9)


class AutoFramingTests(PanelTestCase):
    """Auto must frame the capture, not round it to a finer setting.

    The codes span the eight rows of the instrument's screen, so one row is worth
    exactly the scale the capture was decoded at and the trace fills the grid there.
    A setting FINER than that draws the trace taller than the grid: its peaks run
    past the top row, each fast edge is chopped at the edge, and a sawtooth's flyback
    is drawn as a spike standing over the ramp - which is how it was reported from
    the bench, as spikes the instrument's own screen does not show.
    """

    def capture(self, volts_per_div=3.187, span=12.7):
        """A capture decoded at `volts_per_div` a row, spanning four rows each way."""
        return {"name": "CH1", "units": "V", "volts_per_div": volts_per_div,
                "true_volts_per_div": volts_per_div,
                "volts_per_code_source": "instrument measurements",
                "vertical_offset_div": 0.0, "attenuation": "10X",
                "point_interval": 2e-5,
                "waveform": [-span, 0.0, span]}

    def frame(self, volts_per_div):
        return {"low": -4.0 * volts_per_div, "high": 4.0 * volts_per_div}

    def test_auto_names_a_setting_at_least_as_coarse_as_the_capture(self):
        # 3.187 V/div decoded: the nearest offered setting is 2, which is finer than
        # the capture and clips it. The one that holds it is 5.
        chosen = self.app.framing_choice(3.187, self.app.choice_values(self.app.ch1_scale))
        self.assertEqual(5.0, WaveformData._scale_to_float(chosen))

    def test_the_sawtooth_fits_the_grid_on_auto(self):
        entry = self.capture(3.187, span=12.7)
        self.app._display_scale[1] = None                       # Auto
        scale = self.app.display_scale_value(1, entry)
        self.assertGreaterEqual(scale * 4.0, 12.7)              # the frame holds it
        self.assertIsNone(self.app.off_scale_report(-12.7, 12.7, self.frame(scale)))

    def test_a_unipolar_capture_gets_a_frame_that_holds_it(self):
        # The bench case: the instrument's own MIN reads 0.0000e+00, so every sample
        # sits at or above the decode's zero. A grid centred on 0 V then keeps the
        # whole trace in its top half and chops the peaks at the edge.
        entry = {"name": "CH1", "units": "V", "volts_per_div": 5.0,
                 "true_volts_per_div": 3.187,
                 "volts_per_code_source": "instrument measurements",
                 "vertical_offset_div": 0.0, "attenuation": "10X",
                 "point_interval": 2e-5, "waveform": [0.0, 12.7, 25.4]}
        self.app._display_scale[1] = None                       # Auto
        frame = self.app.vertical_frame([entry])
        self.assertLessEqual(frame["low"], 0.0)
        self.assertGreaterEqual(frame["high"], 25.4)
        self.assertIsNone(self.app.off_scale_report(0.0, 25.4, frame))

    def test_a_finer_setting_that_chops_the_trace_says_so(self):
        note = self.app.off_scale_report(-12.7, 12.7, self.frame(2.0))
        self.assertIn("off the grid", note)
        self.assertIn("5v", note)                               # what would frame it

    def test_a_trace_inside_the_grid_says_nothing(self):
        self.assertIsNone(self.app.off_scale_report(-7.0, 7.0, self.frame(2.0)))

    def test_a_pinned_setting_is_honoured_even_when_it_magnifies(self):
        self.app._display_scale[1] = "1v"
        self.assertEqual(1.0, self.app.display_scale_value(1, self.capture()))

    def test_the_panel_reports_it_rather_than_chopping_silently(self):
        entry = self.capture(3.187, span=12.7)
        self.scope.waveform.channels = [entry]
        self.app._display_scale[1] = "2v"
        self.app.plot_time()
        self.assertIn("off the grid", self.app.status_var.get())


class StateIndicatorTests(PanelTestCase):
    """The panel says what it is doing: a mode, a rate, and a colour for it."""

    def test_before_anything_is_attached_it_says_offline(self):
        self.scope.is_connected = False
        self.app.update_state_indicator()
        self.assertEqual("pause", self.app._state_face)
        self.assertEqual("OFFLINE", self.app.state_word.cget("text"))
        self.assertEqual("", self.app.state_rate.cget("text"))

    def test_a_single_capture_shows_the_hold_icon(self):
        self.app.capture_state.set("ACQUIRING\u2026")
        self.assertEqual("refresh", self.app._state_face)
        self.assertEqual("SINGLE", self.app.state_word.cget("text"))

    def test_live_shows_a_play_icon_and_the_rate(self):
        self.app._live_period = 0.5
        self.app.auto_refresh_var.set(True)
        self.app.update_state_indicator()
        self.assertEqual("play", self.app._state_face)
        self.assertEqual("LIVE", self.app.state_word.cget("text"))
        self.assertEqual("2.0 fps", self.app.state_rate.cget("text"))

    def test_the_rate_is_dropped_when_the_panel_stops(self):
        self.app._live_period = 0.5
        self.app.auto_refresh_var.set(True)
        self.app.update_state_indicator()
        self.app.auto_refresh_var.set(False)
        self.app.update_state_indicator()
        self.assertEqual("", self.app.state_rate.cget("text"))

    def test_the_button_promises_the_other_state(self):
        # What a transport button is for: it says what pressing it will do.
        self.assertEqual("play", self.app._live_button_kind)
        self.app.auto_refresh_var.set(True)
        self.app.update_live_button()
        self.assertEqual("pause", self.app._live_button_kind)
        self.app.auto_refresh_var.set(False)
        self.app.update_live_button()
        self.assertEqual("play", self.app._live_button_kind)

    def test_a_capture_from_disk_is_not_called_single(self):
        self.app.capture_state.set("FILE \u2022 bench.csv")
        self.assertEqual("refresh", self.app._state_face)
        self.assertEqual("FILE", self.app.state_word.cget("text"))

    def test_the_icons_are_drawn_not_typed(self):
        # A pause bar is two bars drawn at the size asked for, not a character the
        # window manager may substitute for something else.
        from modern_lab import PAUSE_RED
        image = self.app.icon("pause", PAUSE_RED)
        self.assertEqual(16, image.width())
        self.assertIs(image, self.app.icon("pause", PAUSE_RED))     # drawn once


class CaptureHeaderRetryTests(unittest.TestCase):
    """A missed capture header is transient on this firmware, so retry once."""

    def scope(self):
        scope = OWONScopeController(family="hds")
        scope.is_connected = True
        scope.query_binary = lambda *args, **kwargs: None
        return scope

    def test_a_missed_header_is_retried_once_then_fails(self):
        scope = self.scope()
        scope._capture_header = Mock(return_value=None)
        self.assertFalse(scope.download_waveform_data())
        self.assertEqual(2, scope._capture_header.call_count)

    def test_a_header_on_the_second_try_is_kept_for_the_live_frames(self):
        # The live path reuses the cached header, so the retry's header must be
        # cached too - otherwise every frame pays for a second attempt.
        scope = self.scope()
        header = {"channel": [{"name": "CH1", "scale": "1e+00", "probe": "10X"}]}
        scope._capture_header = Mock(side_effect=[None, header])
        scope._capture_channels = Mock(return_value=[])
        self.assertFalse(scope.download_waveform_data())  # nothing to plot, but
        self.assertEqual(2, scope._capture_header.call_count)
        self.assertIs(header, scope._cached_header)


class GraticuleTests(PanelTestCase):
    """The grid has to be the instrument's screen: 12 columns by 8 rows."""

    def channel(self, volts_per_div=5.0, interval=2e-4, points=300, offset_div=0.0,
                true_volts_per_div=None):
        return {"name": "CH1", "units": "V", "volts_per_div": volts_per_div,
                "true_volts_per_div": (volts_per_div if true_volts_per_div is None
                                       else true_volts_per_div),
                "vertical_offset_div": offset_div, "attenuation": "10X",
                "point_interval": interval, "waveform": [0.0] * points}

    def draw(self, channel, timebase=None):
        self.scope.waveform.channels = [channel]
        self.scope.waveform.timebase_scale = timebase
        with patch.object(self.app, "sync_vertical_controls"):
            self.app.plot_waveform()
        return self.app.ax

    def test_the_frame_is_eight_divisions_tall(self):
        ax = self.draw(self.channel(volts_per_div=5.0))
        low, high = ax.get_ylim()
        self.assertAlmostEqual(8 * 5.0, high - low, places=6)

    def test_the_rows_are_volts_per_division_apart(self):
        ax = self.draw(self.channel(volts_per_div=5.0))
        ticks = ax.get_yticks()
        self.assertEqual(9, len(ticks))
        for gap in [b - a for a, b in zip(ticks, ticks[1:])]:
            self.assertAlmostEqual(5.0, gap, places=6)

    def test_the_columns_are_the_selected_time_per_division(self):
        # 200us/div over 12 columns is a 2.4ms window, so one column is 200us.
        # The axis is drawn in the unit the trace is plotted in (ms here).
        ax = self.draw(self.channel(interval=2e-4), timebase=2e-4)
        self.assertAlmostEqual(0.0, ax.get_xlim()[0], places=12)
        self.assertAlmostEqual(2.4, ax.get_xlim()[1], places=12)
        ticks = ax.get_xticks()
        self.assertEqual(13, len(ticks))
        for gap in [b - a for a, b in zip(ticks, ticks[1:])]:
            self.assertAlmostEqual(0.2, gap, places=12)

    def test_the_time_label_names_the_division_not_the_span(self):
        ax = self.draw(self.channel(interval=2e-4), timebase=2e-4)
        self.assertIn("200 us/div", ax.get_xlabel())

    def test_without_a_reported_timebase_the_samples_set_the_window(self):
        ax = self.draw(self.channel(interval=2e-4, points=300))
        # 299 intervals of 200us, on an axis drawn in ms.
        self.assertAlmostEqual(299 * 2e-4 * 1e3, ax.get_xlim()[1], places=9)

    def test_the_grid_is_dotted_with_a_brighter_centre_cross(self):
        ax = self.draw(self.channel(volts_per_div=5.0), timebase=2e-4)
        gridline = ax.get_xgridlines()[0]
        self.assertEqual(":", gridline.get_linestyle())
        mid = 2.4 / 2          # the axis is in ms, as the trace is plotted
        vertical, horizontal = [], []
        for line in ax.lines:
            if line.get_color() != "#8ea3a4":
                continue          # the trace and the trigger cursor sit on top
            xdata, ydata = list(line.get_xdata()), list(line.get_ydata())
            if len(xdata) == 2 and xdata[0] == xdata[1] and abs(xdata[0] - mid) < 1e-12:
                vertical.append(line)
            elif len(xdata) == 2 and xdata == [0, 1] and len(set(ydata)) == 1:
                horizontal.append(line)
        self.assertTrue(vertical, "no vertical centre line on the graticule")
        self.assertTrue(horizontal, "no horizontal centre line on the graticule")
        for line in vertical + horizontal:
            self.assertEqual(":", line.get_linestyle())
            self.assertGreater(line.get_linewidth(), gridline.get_linewidth())


class AcquisitionSettingTests(unittest.TestCase):
    """:ACQuire:MODe, :ACQuire:DEPMem and :MEASurement:DISPlay all take writes."""

    def scope_with(self, replies):
        scope = OWONScopeController(family="hds")
        scope.sent = []
        scope.query = lambda node: replies.get(node.upper(), "")
        scope.send_command = scope.sent.append
        return scope

    def test_acquire_mode_is_reported_after_the_write(self):
        scope = self.scope_with({":ACQUIRE:MODE?": "PEAK"})
        self.assertEqual("PEAK", scope.set_acquire_mode("PEAK"))
        self.assertIn(":ACQuire:MODe PEAK", scope.sent)

    def test_memory_depth_is_reported_after_the_write(self):
        scope = self.scope_with({":ACQUIRE:DEPMEM?": "8K"})
        self.assertEqual("8K", scope.set_memory_depth("8K"))

    def test_measurement_display_is_a_boolean(self):
        scope = self.scope_with({":MEASUREMENT:DISPLAY?": "ON"})
        self.assertIs(True, scope.get_measurement_display())
        scope = self.scope_with({":MEASUREMENT:DISPLAY?": "OFF"})
        self.assertIs(False, scope.get_measurement_display())

    def test_a_silent_instrument_reports_nothing_rather_than_a_default(self):
        scope = self.scope_with({})
        self.assertIsNone(scope.get_measurement_display())


if __name__ == "__main__":
    unittest.main()
