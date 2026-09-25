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
        for name in ("VOLTAGE_SCALES", "TIMEBASE_SCALES"):
            setattr(self.scope, name, getattr(OWONScopeController, name))
        self.scope.waveform_data = Mock()
        self.scope.waveform_data.channels = []
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
        self.app.step_choice("ch1_scale", self.scope.VOLTAGE_SCALES, 1,
                             lambda: self.app.set_channel_scale(1, self.app.ch1_scale.get()))
        self.scope.set_channel_scale.assert_called_once_with(1, "2v")
        self.app.ch1_scale.set(self.scope.VOLTAGE_SCALES[-1])
        self.scope.set_channel_scale.reset_mock()
        self.app.step_choice("ch1_scale", self.scope.VOLTAGE_SCALES, 1,
                             lambda: self.app.set_channel_scale(1, self.app.ch1_scale.get()))
        self.scope.set_channel_scale.assert_not_called()
        self.app.step_number("trigger_level", 1, self.app.set_trigger_level)
        self.scope.set_edge_trigger_level.assert_called_once_with(1)
        self.scope.is_connected = False
        before = self.app.timebase_scale.get()
        self.app.step_choice("timebase_scale", self.scope.TIMEBASE_SCALES, 1, self.app.set_timebase)
        self.assertEqual(before, self.app.timebase_scale.get())

    def test_capture_async_and_channel_identity(self):
        self.scope.waveform_data.channels = [
            dict(name="CH2", waveform_data=[0, 2, -2, 0], attenuation=0,
                 voltage_per_point=10, point_interval=1)]
        self.scope.download_waveform_data.return_value = True
        self.app.download_waveform()
        self.pump(lambda: not self.app._busy)
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

    def test_busy_controls_do_not_send_commands(self):
        self.app._busy = True
        before = self.app.ch1_scale.get()
        self.app.ch1_scale.set("2v")
        self.app.ch1_scale.event_generate("<<ComboboxSelected>>")
        self.assertEqual(before, self.app.ch1_scale.get())
        self.app.ch1_display.set(False)
        self.app.toggle_channel(1, self.app.ch1_display)
        self.assertTrue(self.app.ch1_display.get())
        self.scope.set_channel_scale.assert_not_called()
        self.scope.set_channel_display.assert_not_called()
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

