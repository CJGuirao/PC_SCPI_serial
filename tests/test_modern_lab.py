"""Hardware-free integration checks for the Modern Lab front panel."""
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock
from main import App
from modern_lab import Rotary
from owon_controller import OWONScopeController


class FrontPanelTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root)
        self.scope = Mock(spec=OWONScopeController)
        self.scope.is_connected = True
        for name in ("VOLTAGE_SCALES", "TIMEBASE_SCALES", "FRAMING_NAMES"):
            setattr(self.scope, name, getattr(OWONScopeController, name))
        self.scope.waveform = Mock()
        self.scope.waveform.channels = []
        self.app.scope = self.scope

    def tearDown(self):
        if self.root.winfo_exists():
            self.app.close_panel()

    def pump(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_encoder_detents_callbacks_and_limits(self):
        # The volts/div knob steps the widget and its callback sets the software's
        # display scale. It is not a write: on this firmware a volts/div written
        # over SCPI moves the label it reports and nothing else, so the control
        # that has to work is the one the app draws with.
        self.app.step_choice("ch1_scale", self.scope.VOLTAGE_SCALES, 1,
                             lambda: self.app.set_channel_scale(1, self.app.ch1_scale.get()))
        self.scope.set_channel_scale.assert_not_called()
        self.assertIn("display scale", self.app.log_text.get("1.0", "end"))
        self.assertEqual(self.app.ch1_scale.get(), self.app._display_scale[1])
        self.app.ch1_scale.set(self.scope.VOLTAGE_SCALES[-1])
        self.scope.set_channel_scale.reset_mock()
        self.app.step_choice("ch1_scale", self.scope.VOLTAGE_SCALES, 1,
                             lambda: self.app.set_channel_scale(1, self.app.ch1_scale.get()))
        self.scope.set_channel_scale.assert_not_called()
        self.app.step_number("trigger_level", 1, self.app.set_trigger_level)
        self.assertTrue(self.app.wait_for_instrument())
        self.scope.set_edge_trigger_level.assert_called_once_with(1)
        self.scope.is_connected = False
        before = self.app.timebase_scale.get()
        self.app.step_choice("timebase_scale", self.scope.TIMEBASE_SCALES, 1, self.app.set_timebase)
        self.assertEqual(before, self.app.timebase_scale.get())

    def test_a_framing_change_on_the_instrument_reaches_the_panel(self):
        # The user sets the volts/div from the CH1 menu, with no write from here.
        # The watch has to see it, drop the cached header so the next capture is
        # decoded against the new framing, and move the panel's own control - the
        # one that otherwise "does nothing" while the time base looks fine.
        self.scope.framing_signature = Mock(
            return_value=("500mv", "10X", "1ms"))
        self.app.watch_framing()
        self.assertTrue(self.app.wait_for_instrument())
        self.scope.invalidate_capture_header.assert_not_called()

        self.scope.framing_signature = Mock(return_value=("5v", "10X", "1ms"))
        self.app.watch_framing()
        self.assertTrue(self.app.wait_for_instrument())
        self.scope.invalidate_capture_header.assert_called_once_with()
        self.assertEqual("5v", self.app.ch1_scale.get())
        self.assertIn("framing changed", self.app.log_text.get("1.0", "end"))

    def test_an_unchanged_framing_costs_nothing(self):
        self.scope.framing_signature = Mock(return_value=("500mv", "10X", "1ms"))
        self.app.watch_framing()
        self.app.watch_framing()
        self.scope.invalidate_capture_header.assert_not_called()

    def test_capture_async_and_channel_identity(self):
        self.scope.waveform.channels = [
            dict(name="CH2", waveform=[0, 2, -2, 0], attenuation=0,
                 voltage_per_point=10, point_interval=1)]
        self.scope.download_waveform_data.return_value = True
        self.app.download_waveform()
        self.assertTrue(self.app.wait_for_instrument())
        self.assertEqual(self.app.capture_state.get(), "CAPTURED")
        self.assertEqual(self.app.ax.lines[0].get_color(), "#26d7e8")
        self.assertEqual(len(self.app.ax.lines[0].get_xdata()), 4)

    def test_failed_live_capture_stops_refresh(self):
        self.scope.download_waveform_data.return_value = False
        self.app.toggle_live()
        self.pump(lambda: not self.app._busy)
        self.assertFalse(self.app.auto_refresh_var.get())
        self.assertIsNone(self.app._live_timer)

    def test_pause_during_inflight_capture(self):
        release = threading.Event()
        self.scope.download_waveform_data.side_effect = lambda: release.wait(2) and False
        self.app.toggle_live()
        self.assertTrue(self.app._busy)
        self.app.toggle_live()
        self.assertFalse(self.app.auto_refresh_var.get())
        release.set()
        self.pump(lambda: not self.app._busy)
        self.assertIsNone(self.app._live_timer)

    def test_controls_still_act_while_a_capture_is_in_flight(self):
        # The reverse of what this used to assert. Refusing every control while a
        # capture ran made the panel read as unresponsive for half of every second
        # during live acquisition; now the press is taken and the instrument is
        # asked from the worker, which is what a real front panel does.
        self.app._busy = True
        self.app.ch1_display.set(False)
        self.app.toggle_channel(1, self.app.ch1_display)
        self.assertFalse(self.app.ch1_display.get())
        self.assertTrue(self.app.wait_for_instrument())
        self.scope.set_channel_display.assert_called_once_with(1, False)
        self.app._busy = False

    def test_drawer_and_layout(self):
        self.root.deiconify()
        for geometry in ("1440x900", "1120x760"):
            self.root.geometry(geometry)
            self.root.update()
            for index in range(3):
                self.app.show_drawer(index)
                self.root.update()
                self.assertTrue(self.app.drawer.winfo_ismapped())
                self.assertGreater(self.app.canvas.get_tk_widget().winfo_height(), 100)
                self.app.hide_drawer()
            for widget in (self.app.live_btn, self.app.ch2_probe, self.app.timebase_scale):
                bottom = widget.winfo_rooty() - self.root.winfo_rooty() + widget.winfo_height()
                self.assertLessEqual(bottom, self.root.winfo_height(), geometry)
        self.assertTrue(self.app.console_text.winfo_exists())
        self.assertTrue(self.app.measurement_text.winfo_exists())


if __name__ == "__main__":
    unittest.main()

