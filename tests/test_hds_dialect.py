import unittest

from owon_controller import OWONScopeController


class FakeScope(OWONScopeController):
    """Records what would be sent and replays canned replies."""

    def __init__(self, family="hds", replies=None):
        super().__init__(family=family)
        self.sent = []
        self.replies = replies or {}
        self.is_connected = True

    def send_command(self, command):
        self.sent.append(str(command).strip())
        return True

    def query(self, command):
        key = str(command).strip()
        self.sent.append(key)
        return self.replies.get(key)


class ChannelDialectTests(unittest.TestCase):
    def test_hds_channel_commands_use_the_short_node(self):
        scope = FakeScope("hds")
        scope.set_channel_scale(1, "5v")
        scope.set_channel_coupling(2, "DC")
        scope.set_channel_probe(1, "10X")
        scope.set_channel_offset(1, "0")
        self.assertIn(":CH1:SCALe 5v", scope.sent)
        self.assertIn(":CH2:COUPling DC", scope.sent)
        self.assertIn(":CH1:PROBe 10X", scope.sent)
        self.assertIn(":CH1:OFFSet 0", scope.sent)

    def test_hds_channel_commands_use_short_node(self):
        scope = FakeScope("hds")
        scope.set_channel_scale(1, "5v")
        scope.set_channel_coupling(1, "AC")
        self.assertIn(":CH1:SCALe 5v", scope.sent)
        self.assertIn(":CH1:COUPling AC", scope.sent)


class ReplyNormalisationTests(unittest.TestCase):
    def test_engineering_notation_maps_onto_the_choice_lists(self):
        scope = FakeScope("hds", {
            ":CH1:SCALe?": "5e+00",
            ":CH1:COUPling?": "AC",
            ":HORIzontal:SCALe?": "5e-04",
            ":TRIGger:SINGle:SWEEp?": "AUTo",
            ":ACQuire:MODe?": "SAMPle",
            ":ACQuire:DEPMem?": "4K",
        })
        self.assertEqual(scope.get_channel_scale(1), "5v")
        self.assertEqual(scope.get_channel_coupling(1), "AC")
        self.assertEqual(scope.get_timebase_scale(), "500us")
        self.assertEqual(scope.get_trigger_mode(), "AUTo")
        self.assertEqual(scope.get_acquire_type(), "SAMPle")
        self.assertEqual(scope.get_memory_depth(), "4K")
        self.assertIn("4K", scope.MEMORY_DEPTHS)

    def test_unmatched_replies_are_passed_through_unchanged(self):
        scope = FakeScope("hds", {":CH1:SCALe?": "7e-07", ":HORIzontal:SCALe?": None})
        self.assertEqual(scope.get_channel_scale(1), "7e-07")
        self.assertIsNone(scope.get_timebase_scale())

    def test_scale_parsing(self):
        scope = FakeScope("hds")
        self.assertAlmostEqual(scope.parse_scale("500us"), 5e-04)
        self.assertAlmostEqual(scope.parse_scale("5v"), 5.0)
        self.assertAlmostEqual(scope.parse_scale("100mv"), 0.1)
        self.assertAlmostEqual(scope.parse_scale("5e-04"), 5e-04)
        self.assertEqual(scope.match_scale("5e-04", scope.TIMEBASE_SCALES), "500us")
        self.assertEqual(scope.match_scale("1e-03", scope.TIMEBASE_SCALES), "1ms")
        self.assertIsNone(scope.match_scale("7e-07", scope.TIMEBASE_SCALES))
        self.assertEqual(scope.match_choice("AUTo", scope.TRIGGER_MODES), "AUTo")
        self.assertIsNone(scope.match_choice("", scope.TRIGGER_MODES))


class MeasurementTests(unittest.TestCase):
    def test_hds_measurements_are_composed_from_items(self):
        scope = FakeScope("hds", {
            ":MEASUrement:CH1:FREQuency?": "1.0000e+03",
            ":MEASUrement:CH1:PERiod?": "1.0000e+09",
            ":MEASUrement:CH1:PKPK?": "2.5600e+01",
            ":MEASUrement:CH1:MAX?": "1.2800e+01",
            ":MEASUrement:CH1:MIN?": "-1.2800e+01",
            ":MEASUrement:CH1:AVERage?": "-7.4000e-02",
        })
        payload = scope.get_all_measurements(1)
        self.assertEqual(payload["Vpp"], "2.5600e+01")
        self.assertEqual(payload["Frequency"], "1.0000e+03")
        self.assertEqual(payload["Vmin"], "-1.2800e+01")
        self.assertNotIn(":MEASUrement:CH1?", scope.sent)


class ConnectRoutingTests(unittest.TestCase):
    def test_auto_usb_on_hds_goes_to_hid_and_identifies(self):
        scope = OWONScopeController(family="hds")
        calls = []
        scope.connect_usb_hid = lambda serial=None, timeout=2.0: calls.append("hid") or True
        scope.identify_model = lambda: calls.append("identify") or {}
        self.assertTrue(scope.connect_usb("auto"))
        self.assertEqual(calls, ["hid", "identify"])

    def test_hid_failure_returns_false(self):
        scope = OWONScopeController(family="hds")
        scope.connect_usb_hid = lambda serial=None, timeout=2.0: False
        self.assertFalse(scope.connect_usb("auto"))


if __name__ == "__main__":
    unittest.main()
