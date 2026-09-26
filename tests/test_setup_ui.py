"""The bench setup as the panel offers it: the SETUP key, the dialog, and the
serial that reaches the connect path.

The dialog collects values and hands them to the app; the app owns the file and
the applying. These tests drive that path end to end with a mocked controller and
a temporary settings file, so what is asserted is the wiring rather than the
widgets.
"""

import json
import os
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from owon_controller import OWONScopeController
from scope_setup import ScopeSetup


class PanelTestCase(unittest.TestCase):
    """A live panel backed by a mocked controller, with no hardware attached."""

    def setUp(self):
        from main import App
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root)
        self.scope = Mock(spec=OWONScopeController)
        self.scope.is_connected = True
        for name in ("VOLTAGE_SCALES", "TIMEBASE_SCALES", "PROBE_ATTEN", "COUPLING_MODES",
                     "ACQ_TYPES", "AVG_COUNTS", "MEMORY_DEPTHS", "TRIGGER_SLOPES"):
            setattr(self.scope, name, getattr(OWONScopeController, name))
        self.scope.parse_scale = OWONScopeController.parse_scale
        self.scope.waveform_data = Mock()
        self.scope.waveform_data.channels = []
        self.app.scope = self.scope
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, "scope_setup.json")
        self.app.setup = ScopeSetup(path=self.path)

    def tearDown(self):
        for window in (getattr(self.app, "_setup_window", None), None):
            if window is not None:
                try:
                    window.destroy()
                except tk.TclError:
                    pass
        self.folder.cleanup()
        try:
            self.app.close_panel()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


class ZoomRemovedTests(PanelTestCase):
    """The zoom keys are gone while live framing owns the view."""

    def test_there_is_no_zoom_key(self):
        labels = [child.cget("text") for child in self.app._display_keys.winfo_children()]
        self.assertTrue(labels)
        for label in labels:
            self.assertNotIn("ZOOM", label.upper())
        self.assertIn("SETUP", " ".join(labels))

    def test_the_zoom_methods_are_gone_too(self):
        self.assertFalse(hasattr(self.app, "zoom_in"))
        self.assertFalse(hasattr(self.app, "zoom_out"))


class SetupDialogTests(PanelTestCase):
    """SETUP opens, prefilled, and what it saves is what the decode then uses."""

    def open_dialog(self, devices=None):
        with patch("main.attached_scopes", return_value=list(devices or [])):
            self.app.open_setup()
        return self.app._setup_window

    def test_the_dialog_opens_prefilled_with_what_is_in_force(self):
        self.app.setup = ScopeSetup({"usb_serial": "25520161", "calibration_trim": 0.5,
                                     "probe": "X1", "known_amplitude": 25.0}, path=self.path)
        dialog = self.open_dialog()
        self.assertIsNotNone(dialog)
        self.assertEqual("0.5", dialog.trim.get())
        self.assertEqual("25", dialog.amplitude.get().rstrip(".0"))

    def test_a_scope_with_a_serial_is_offered_and_selectable(self):
        devices = [{"product": "Oscilloscope MSC+HID", "serial": "25520161",
                    "vid": 0x5345, "pid": 0x1234},
                   {"product": "Oscilloscope MSC+HID", "serial": "99999999",
                    "vid": 0x5345, "pid": 0x1234}]
        dialog = self.open_dialog(devices)
        values = list(dialog.device_box.cget("values"))
        self.assertEqual(3, len(values))                       # automatic + the two
        self.assertIn("25520161", " ".join(values))
        dialog.device_choice.set([v for v in values if "99999999" in v][0])
        self.assertEqual("99999999", dialog.collect().usb_serial)

    def test_a_scope_without_a_serial_cannot_be_chosen_by_serial(self):
        dialog = self.open_dialog([{"product": "OWON scope", "serial": None,
                                    "vid": 0x5345, "pid": 0x1234}])
        # It is listed, but choosing it means "first attached", not a serial: an
        # empty serial would match nothing and connect to nothing.
        self.assertIn("no serial", " ".join(dialog.device_box.cget("values")))
        self.assertIsNone(dialog.collect().usb_serial)

    def test_save_and_apply_writes_the_file_and_moves_the_decode(self):
        import waveform_data
        before = waveform_data.HDS_MANUAL_CALIBRATION_TRIM
        try:
            dialog = self.open_dialog()
            dialog.trim.set("0.5")
            dialog.save_and_apply()
            self.assertTrue(os.path.exists(self.path))
            with open(self.path, "r", encoding="utf-8") as handle:
                self.assertAlmostEqual(0.5, json.load(handle)["calibration_trim"], places=9)
            self.assertAlmostEqual(0.5, waveform_data.HDS_MANUAL_CALIBRATION_TRIM, places=9)
            self.assertAlmostEqual(0.5, self.app.setup.calibration_trim, places=9)
        finally:
            waveform_data.HDS_MANUAL_CALIBRATION_TRIM = before

    def test_save_alone_writes_the_file_without_applying_it(self):
        import waveform_data
        before = waveform_data.HDS_MANUAL_CALIBRATION_TRIM
        try:
            dialog = self.open_dialog()
            dialog.trim.set("0.25")
            dialog.save_only()
            with open(self.path, "r", encoding="utf-8") as handle:
                self.assertAlmostEqual(0.25, json.load(handle)["calibration_trim"], places=9)
            self.assertEqual(before, waveform_data.HDS_MANUAL_CALIBRATION_TRIM)
        finally:
            waveform_data.HDS_MANUAL_CALIBRATION_TRIM = before

    def test_calibrate_now_turns_the_last_capture_into_a_trim(self):
        self.app.setup = ScopeSetup({"known_amplitude": 25.0}, path=self.path)
        dialog = self.open_dialog()
        # A capture showing 25.8 Vpp for a generator set to 25.0.
        self.scope.waveform_data.channels = [
            {"name": "CH1", "units": "V", "waveform_data": [-12.9, 12.9]}]
        with patch.object(self.app, "last_capture_amplitude", return_value=25.8):
            dialog.derive_trim()
        self.assertAlmostEqual(25.0 / 25.80, float(dialog.trim.get()), places=6)
        self.assertIn("25.8", dialog.status.get())

    def test_calibrate_now_says_so_when_there_is_no_capture(self):
        dialog = self.open_dialog()
        with patch.object(self.app, "last_capture_amplitude", return_value=None):
            dialog.derive_trim()
        self.assertIn("capture", dialog.status.get().lower())
        self.assertAlmostEqual(1.0, float(dialog.trim.get()), places=9)

    def test_the_capture_amplitude_is_read_the_way_the_panel_shows_it(self):
        self.scope.waveform_data.channels = [
            {"name": "CH1", "units": "V", "waveform_data": [-12.8, 0.0, 12.8]}]
        self.assertAlmostEqual(25.6, self.app.last_capture_amplitude(), places=6)
        self.scope.waveform_data.channels = []
        self.assertIsNone(self.app.last_capture_amplitude())


class DeviceSelectionTests(PanelTestCase):
    """The saved serial is what steers the HID connect."""

    def connect(self, conn_type="usb"):
        self.app.conn_type.set(conn_type)
        self.app.scope.connect_usb_hid.return_value = True
        self.app.scope.connect_usb.return_value = True
        with patch("main.attached_scopes", return_value=[]), \
                patch.object(self.app, "query_all_states"), \
                patch.object(self.app, "sync_dmm"), \
                patch.object(self.app, "refresh_cursors"), \
                patch.object(self.app, "apply_channel_count"), \
                patch.object(self.app, "plot_waveform"):
            self.app.connect_scope()

    def test_a_saved_serial_is_used_to_open_that_scope(self):
        self.app.setup = ScopeSetup({"usb_serial": "25520161"}, path=self.path)
        self.connect()
        self.scope.connect_usb_hid.assert_called_once_with(serial="25520161")

    def test_without_a_saved_serial_the_first_attached_one_is_opened(self):
        self.connect()
        self.scope.connect_usb_hid.assert_not_called()
        self.scope.connect_usb.assert_called()

    def test_several_attached_scopes_are_reported_to_the_user(self):
        self.app.setup = ScopeSetup({"usb_serial": "25520161"}, path=self.path)
        devices = [{"product": "a", "serial": "25520161", "vid": 0x5345, "pid": 0x1234},
                   {"product": "b", "serial": "99999999", "vid": 0x5345, "pid": 0x1234}]
        with patch("main.attached_scopes", return_value=devices):
            self.app.conn_type.set("usb")
            self.app.scope.connect_usb_hid.return_value = True
            with patch.object(self.app, "query_all_states"), patch.object(self.app, "sync_dmm"), \
                    patch.object(self.app, "refresh_cursors"), \
                    patch.object(self.app, "apply_channel_count"), \
                    patch.object(self.app, "plot_waveform"):
                self.app.connect_scope()
        log = self.app.log_text.get("1.0", "end")
        self.assertIn("2 OWON USB devices", log)
        self.assertIn("25520161", log)


if __name__ == "__main__":
    unittest.main()
