"""The bench setup: which scope, and the numbers that calibrate it.

These settings outlive a run and are edited by hand, so the interesting behaviour
is all about what happens to a file that is missing, damaged, or holds a value
that cannot mean anything.
"""

import json
import os
import tempfile
import unittest

from modernlab.instrument.capture import waveform
from modernlab.settings.bench import (DEFAULT_PATH, SETTINGS_VERSION, PROBE_CHOICES, ScopeSetup,
                         device_label)


class SetupDefaultsTests(unittest.TestCase):
    """A bench that has never been configured must behave exactly as shipped."""

    def test_defaults_leave_the_decode_as_it_ships(self):
        setup = ScopeSetup()
        self.assertIsNone(setup.usb_serial)
        self.assertIsNone(setup.calibration_trim)      # None, not 1.0: "no trim"
        self.assertEqual("X1", setup.values["probe"])
        # No instrument chosen means the first attached one, which is what the
        # connect path did before there was a setup at all.
        self.assertIn("first attached", setup.as_text())

    def test_applying_the_defaults_changes_nothing(self):
        before = waveform.HDS_MANUAL_CALIBRATION_TRIM
        try:
            changed = ScopeSetup().apply()
            self.assertIsNone(waveform.HDS_MANUAL_CALIBRATION_TRIM)
            self.assertIn("untrimmed", " ".join(changed))
        finally:
            waveform.HDS_MANUAL_CALIBRATION_TRIM = before

    def test_a_missing_file_is_not_a_condition_to_fix(self):
        missing = os.path.join(tempfile.gettempdir(), "scope_setup_that_is_not_there.json")
        if os.path.exists(missing):
            os.remove(missing)
        setup = ScopeSetup.load(missing)
        self.assertEqual(ScopeSetup.DEFAULTS["probe"], setup.values["probe"])
        self.assertEqual(missing, setup.path)

    def test_a_damaged_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "scope_setup.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{ this is not json")
            setup = ScopeSetup.load(path)
            self.assertEqual(ScopeSetup.DEFAULTS["probe"], setup.values["probe"])
            self.assertIsNone(setup.calibration_trim)

    def test_a_file_holding_a_list_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "scope_setup.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump([1, 2, 3], handle)
            self.assertIsNone(ScopeSetup.load(path).calibration_trim)

    def test_an_unknown_setting_is_dropped_not_stored(self):
        setup = ScopeSetup({"something_else": 42})
        self.assertNotIn("something_else", setup.values)


class SetupRoundTripTests(unittest.TestCase):
    """What is saved must be what comes back."""

    def test_save_then_load_returns_the_same_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "nested", "scope_setup.json")
            original = ScopeSetup({"usb_serial": "25520161", "calibration_trim": 0.968992,
                                   "reference_volts_per_code": 0.15625,
                                   "probe": "X1", "known_amplitude": 25.0}, path=path)
            self.assertTrue(original.save())
            self.assertTrue(os.path.exists(path))
            loaded = ScopeSetup.load(path)
            self.assertEqual("25520161", loaded.usb_serial)
            self.assertAlmostEqual(0.968992, loaded.calibration_trim, places=9)
            self.assertAlmostEqual(0.15625, loaded.values["reference_volts_per_code"], places=9)
            self.assertAlmostEqual(25.0, loaded.values["known_amplitude"], places=9)

    def test_the_saved_file_carries_its_version(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "scope_setup.json")
            ScopeSetup({"calibration_trim": 0.5}, path=path).save()
            with open(path, "r", encoding="utf-8") as handle:
                self.assertEqual(SETTINGS_VERSION, json.load(handle)["version"])

    def test_an_unwritable_path_reports_failure_rather_than_raising(self):
        setup = ScopeSetup(path=os.path.join(tempfile.gettempdir(), "no_such_dir_xyz",
                                             "sub", "file.json"))
        weird = os.path.join(tempfile.gettempdir(), "bench.\\/:*?\"<>|")
        setup.path = weird
        self.assertFalse(setup.save())


class SetupValidationTests(unittest.TestCase):
    """A hand-edited file must not be able to put nonsense into the decode."""

    def test_a_trim_that_is_not_a_number_keeps_the_default(self):
        self.assertIsNone(ScopeSetup({"calibration_trim": "half"}).calibration_trim)

    def test_a_trim_of_zero_or_less_keeps_the_default(self):
        # A zero trim would flatten every trace to a line at zero volts.
        for value in (0, -3, "0"):
            self.assertIsNone(ScopeSetup({"calibration_trim": value}).calibration_trim)

    def test_a_trim_of_exactly_one_means_no_trim(self):
        self.assertIsNone(ScopeSetup({"calibration_trim": 1.0}).calibration_trim)

    def test_a_trim_is_reported_when_it_is_not_one(self):
        self.assertAlmostEqual(0.5, ScopeSetup({"calibration_trim": 0.5}).calibration_trim,
                               places=9)

    def test_a_blank_serial_means_the_first_attached(self):
        self.assertIsNone(ScopeSetup({"usb_serial": "   "}).usb_serial)

    def test_the_probe_is_normalised_or_dropped(self):
        self.assertEqual("X10", ScopeSetup({"probe": "10"}).values["probe"])
        self.assertEqual("X10", ScopeSetup({"probe": "x10"}).values["probe"])
        self.assertEqual("X1", ScopeSetup({"probe": "sideways"}).values["probe"])
        self.assertIn(ScopeSetup({"probe": "X1000"}).values["probe"], PROBE_CHOICES)

    def test_a_dud_reference_keeps_the_modules_own_value(self):
        self.assertIsNone(ScopeSetup({"reference_volts_per_code": "lots"}).values[
            "reference_volts_per_code"])
        self.assertAlmostEqual(0.15625,
                               ScopeSetup({"reference_volts_per_code": "0.15625"}).values[
                                   "reference_volts_per_code"], places=9)


class SetupApplyTests(unittest.TestCase):
    """Applying has to reach the decode, and has to be reversible."""

    def setUp(self):
        self.trim = waveform.HDS_MANUAL_CALIBRATION_TRIM
        self.reference = waveform.HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X

    def tearDown(self):
        waveform.HDS_MANUAL_CALIBRATION_TRIM = self.trim
        waveform.HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X = self.reference

    def test_a_trim_reaches_the_decode(self):
        changed = ScopeSetup({"calibration_trim": 0.5}).apply()
        self.assertAlmostEqual(0.5, waveform.HDS_MANUAL_CALIBRATION_TRIM, places=9)
        self.assertIn("x0.5", " ".join(changed))

    def test_a_reference_reaches_the_decode(self):
        ScopeSetup({"reference_volts_per_code": 0.15625}).apply()
        self.assertAlmostEqual(0.15625,
                               waveform.HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X, places=9)

    def test_an_unset_reference_leaves_the_modules_value_alone(self):
        waveform.HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X = 0.1449
        ScopeSetup().apply()
        self.assertAlmostEqual(0.1449,
                               waveform.HDS_REFERENCE_VOLTS_PER_CODE_TIP_10X, places=9)

    def test_an_applied_trim_decodes_a_capture_away_from_the_instrument(self):
        # End to end through the real decode: the trim has to move the volts, or
        # the whole feature is decorative.
        from tests.test_hds_controller import hds_header, hds_payload
        waveform.HDS_MANUAL_CALIBRATION_TRIM = None
        plain = waveform.WaveformData()
        plain.parse_hds_capture(hds_header("500mv"), {"CH1": hds_payload([113, 131, 149])})
        ScopeSetup({"calibration_trim": 0.5}).apply()
        trimmed = waveform.WaveformData()
        trimmed.parse_hds_capture(hds_header("500mv"), {"CH1": hds_payload([113, 131, 149])})
        self.assertAlmostEqual(plain.channels[0]["voltage_per_point"] * 0.5,
                               trimmed.channels[0]["voltage_per_point"], places=9)
        self.assertIn("trimmed x0.5", trimmed.channels[0]["volts_per_code_source"])


class TrimFromTests(unittest.TestCase):
    """The arithmetic behind the Calibrate button."""

    def test_the_ratio_of_truth_to_reading(self):
        self.assertAlmostEqual(25.0 / 25.80, ScopeSetup.trim_from(25.0, 25.80), places=9)

    def test_a_second_calibration_compounds_with_the_first(self):
        # What the app shows already includes the trim in force, so calibrating
        # again from a trimmed reading must not undo the first correction: a bench
        # that reads 25.8 untrimmed shows 25.0 after trim 0.968992, and asking for
        # 25.0 again must leave it alone rather than multiplying by 1.032.
        once = ScopeSetup.trim_from(25.0, 25.80)
        self.assertAlmostEqual(25.0, 25.80 * once, places=6)
        again = ScopeSetup.trim_from(25.0, 25.0, current=once)
        self.assertAlmostEqual(once, again, places=9)

    def test_a_flat_or_missing_capture_gives_no_factor(self):
        for measured in (0, -1, None, "", "blank"):
            self.assertIsNone(ScopeSetup.trim_from(25.0, measured))
        self.assertIsNone(ScopeSetup.trim_from(0, 25.0))
        self.assertIsNone(ScopeSetup.trim_from("twenty five", 25.0))


class DeviceLabelTests(unittest.TestCase):
    """Several scopes may be attached; the label has to tell them apart."""

    def test_a_device_with_a_serial_names_it(self):
        label = device_label({"product": "Oscilloscope MSC+HID", "serial": "25520161",
                              "vid": 0x5345, "pid": 0x1234})
        self.assertIn("25520161", label)
        self.assertIn("5345:1234", label)

    def test_a_device_without_a_serial_says_so(self):
        label = device_label({"product": None, "serial": None, "vid": 0x5345, "pid": 0x1234})
        self.assertIn("no serial", label)
        self.assertIn("OWON scope", label)

    def test_a_nul_padded_descriptor_is_cleaned_up(self):
        # A real product string off this bench: "Oscilloscope MSC+HID\x00\x00\x00".
        label = device_label({"product": "Oscilloscope MSC+HID\x00\x00\x00",
                              "serial": "25520161\x00", "vid": 0x5345, "pid": 0x1234})
        self.assertIn("Oscilloscope MSC+HID - serial 25520161", label)
        self.assertNotIn("\x00", label)

    def test_an_empty_dict_still_produces_a_label(self):
        self.assertIn("no serial", device_label({}))
        self.assertIn("no serial", device_label(None))


class PerModelTests(unittest.TestCase):
    """Calibration belongs to an instrument, not to a machine."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="per-model-")
        self.addCleanup(self._dir.cleanup)

    def path(self):
        return os.path.join(self._dir.name, "scope_setup.json")

    def test_a_pre_model_file_gives_its_numbers_to_the_first_instrument(self):
        # What this bench looks like today: one trim, and no model name anywhere in
        # the file. It describes the instrument that wrote it, which is the one that
        # then identifies itself.
        setup = ScopeSetup({"calibration_trim": 0.968992, "known_amplitude": 25.0}, path=self.path())
        setup.use_model("HDS271")
        self.assertAlmostEqual(setup.values["calibration_trim"], 0.968992)
        self.assertAlmostEqual(setup.values["per_model"]["HDS271"]["calibration_trim"], 0.968992)
        setup.save()
        stored = json.loads(open(self.path(), encoding="utf-8").read())
        self.assertAlmostEqual(stored["per_model"]["HDS271"]["calibration_trim"], 0.968992)

    def test_a_second_model_does_not_inherit_the_first_ones_calibration(self):
        setup = ScopeSetup({"calibration_trim": 0.968992}, path=self.path())
        setup.use_model("HDS271")
        setup.use_model("HDS2062")
        self.assertIsNone(setup.calibration_trim)

    def test_coming_back_to_a_model_finds_its_numbers_again(self):
        setup = ScopeSetup({"calibration_trim": 0.968992}, path=self.path())
        setup.use_model("HDS271")
        setup.use_model("HDS2062")
        setup.values["calibration_trim"] = 1.25
        setup.remember_model()
        setup.use_model("HDS271")
        self.assertAlmostEqual(setup.values["calibration_trim"], 0.968992)
        setup.use_model("HDS2062")
        self.assertAlmostEqual(setup.values["calibration_trim"], 1.25)

    def test_two_objects_do_not_share_their_model_sections(self):
        first = ScopeSetup({"calibration_trim": 0.968992}, path=self.path())
        first.use_model("HDS271")
        second = ScopeSetup({}, path=self.path())
        self.assertEqual(second.values["per_model"], {})

    def test_an_empty_model_name_changes_nothing(self):
        setup = ScopeSetup({"calibration_trim": 0.968992}, path=self.path())
        self.assertIsNone(setup.use_model(""))
        self.assertAlmostEqual(setup.values["calibration_trim"], 0.968992)
        self.assertEqual(setup.values["per_model"], {})

    def test_the_model_name_is_upper_cased_so_the_key_does_not_drift(self):
        setup = ScopeSetup({}, path=self.path())
        setup.use_model("hds271")
        self.assertIn("HDS271", setup.values["per_model"])

    def test_a_damaged_section_is_dropped_rather_than_loaded(self):
        setup = ScopeSetup({"per_model": {"HDS271": "not a mapping",
                                          "HDS2062": {"calibration_trim": "0.5",
                                                      "nonsense": 3}}}, path=self.path())
        self.assertNotIn("HDS271", setup.values["per_model"])
        self.assertAlmostEqual(setup.values["per_model"]["HDS2062"]["calibration_trim"], 0.5)
        self.assertNotIn("nonsense", setup.values["per_model"]["HDS2062"])

    def test_the_log_line_names_the_model(self):
        setup = ScopeSetup({}, path=self.path())
        setup.use_model("HDS271")
        self.assertIn("model=HDS271", setup.as_text())


class SettingsLocationTests(unittest.TestCase):
    """The settings file belongs to the installation, not to a module.

    Deriving its path from this module's own __file__ put it inside the package the
    moment the code was split into one, and the app then ran on defaults while a
    perfectly good scope_setup.json sat beside main.py - the bench calibration with it.
    Measured in the log: "no ...\\modernlab\\settings\\scope_setup.json yet; using
    defaults".
    """

    def test_the_installed_path_is_beside_the_app(self):
        # INSTALLED_PATH, not DEFAULT_PATH: the suite redirects the latter into a temp
        # directory, which is exactly why the regression below went unnoticed.
        from modernlab import PROJECT_ROOT
        from modernlab.settings.bench import INSTALLED_PATH
        self.assertTrue(os.path.exists(os.path.join(PROJECT_ROOT, "main.py")),
                        "the project root is where the entry point is")
        self.assertEqual(os.path.join(PROJECT_ROOT, "scope_setup.json"), INSTALLED_PATH)

    def test_the_installed_path_is_not_inside_the_package(self):
        from modernlab.settings.bench import INSTALLED_PATH
        self.assertNotIn(os.sep + "modernlab", INSTALLED_PATH)


if __name__ == "__main__":
    unittest.main()
