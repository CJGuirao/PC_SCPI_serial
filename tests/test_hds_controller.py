import unittest
from types import SimpleNamespace

from owon_controller import OWONScopeController
from waveform_data import WaveformData


class ControllerTests(unittest.TestCase):
    def test_hds_command_mapping(self):
        scope = OWONScopeController(family="hds")
        sent = []
        scope.send_command = lambda command: sent.append(command) or True
        scope.query = lambda command: sent.append(command) or "OK"

        scope.set_timebase_scale("1ms")
        scope.set_acquire_type("SAMPle")
        scope.set_acquire_average("16")
        scope.set_memory_depth("10K")
        scope.set_trigger_mode("AUTO")
        scope.set_trigger_source("CH1")
        scope.set_trigger_slope("RISE")
        scope.set_trigger_level("0")

        self.assertIn(":HORIzontal:SCALe 1ms", sent)
        self.assertIn(":ACQuire:MODE SAMPle", sent)
        self.assertIn(":ACQuire:AVERage:NUM 16", sent)
        self.assertIn(":ACQuire:DEPMem 10K", sent)
        self.assertIn(":TRIGger:SINGle:SWEEp AUTO", sent)
        self.assertIn(":TRIGger:SINGle:EDGE:SOURce CH1", sent)
        self.assertIn(":TRIGger:SINGle:EDGE:SLOPe RISE", sent)
        self.assertIn(":TRIGger:SINGle:EDGE:LEVel 0", sent)

    def test_usb_port_detection_prefers_owon_vid_pid(self):
        scope = OWONScopeController(family="hds")
        fake_ports = [
            SimpleNamespace(device="COM7", vid=0x1234, pid=0x5678, description="Other device", manufacturer="Other"),
            SimpleNamespace(device="COM11", vid=0x5345, pid=0x1234, description="OWON HDS200", manufacturer="OWON"),
        ]
        import owon_controller as module
        original = module.list_ports.comports
        module.list_ports.comports = lambda: fake_ports
        try:
            self.assertEqual(scope.detect_usb_port(), "COM11")
        finally:
            module.list_ports.comports = original

    def test_hds_waveform_parsing(self):
        wf = WaveformData()
        header = {
            "timebase": {"scale": "1.0ms", "hoffset": 0},
            "sample": {"fullscreen": 4, "slowmove": -1, "datalen": 4, "samplerate": "(500ks/s)", "type": "sample", "depmem": "10k"},
            "channel": [{"name": "ch1", "display": "on", "coupling": "dc", "probe": "10x", "scale": "5.00mv", "offset": 0, "frequence": 0, "inverse": "off"}],
            "datatype": "screen",
            "runstatus": "auto",
            "trig": {"mode": "single", "type": "edge", "items": {"channel": "ch1", "level": "32.0mv", "edge": "rise", "coupling": "dc", "holdoff": "100ns"}, "sweep": "auto"},
        }
        wf.parse_hds_capture(header, {"CH1": b"\x00\x08\x01\x08\x02\x08\x03\x08"})
        self.assertEqual(len(wf.channels), 1)
        self.assertEqual(wf.channels[0]["name"], "CH1")
        self.assertEqual(wf.channels[0]["num_points"], 4)
        self.assertEqual(len(wf.channels[0]["waveform_data"]), 4)


if __name__ == "__main__":
    unittest.main()
