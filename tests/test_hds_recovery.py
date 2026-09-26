import unittest

from hds_usb import HdsHidTransport


class FakeDevice:
    def __init__(self):
        self.cleared = []

    def clear_halt(self, address):
        self.cleared.append(address)
        return None


class StallingEndpoint:
    def __init__(self, address):
        self.bEndpointAddress = address

    def read(self, size, timeout=None):
        raise RuntimeError("Pipe error")

    def write(self, data, timeout=None):
        raise RuntimeError("Pipe error")


def stalling_transport():
    transport = HdsHidTransport()
    transport._device = FakeDevice()
    transport._ep_in = StallingEndpoint(0x81)
    transport._ep_out = StallingEndpoint(0x01)
    transport.is_open = True
    transport.timeout = 0.01
    return transport


class RecoveryTests(unittest.TestCase):
    def test_recover_clears_both_endpoint_halts(self):
        transport = stalling_transport()
        self.assertTrue(transport.recover())
        self.assertEqual(transport._device.cleared, [0x81, 0x01])

    def test_read_failure_never_clears_halt(self):
        """Regression: clearing a halt mid-read is what truncated every capture.

        The scope pushes a whole reply into the interrupt endpoint and drops it
        if the host stops polling, so the read path must never issue clear_halt
        or count failures. This used to make even *IDN? return nothing.
        """
        transport = stalling_transport()
        calls = []
        transport.recover = lambda: calls.append("recover") or True
        for _ in range(5):
            transport._pump(5)
        self.assertEqual(calls, [])

    def test_write_failure_triggers_recovery_once(self):
        transport = stalling_transport()
        calls = []
        transport.recover = lambda: calls.append("recover") or True
        with self.assertRaises(Exception):
            transport.write(b"*IDN?\n")
        self.assertEqual(calls, ["recover"])

    def test_write_failure_skips_recovery_when_disabled(self):
        transport = stalling_transport()
        transport.recover_on_write_failure = False
        calls = []
        transport.recover = lambda: calls.append("recover") or True
        with self.assertRaises(Exception):
            transport.write(b"*IDN?\n")
        self.assertEqual(calls, [])

    def test_recover_without_a_device_is_safe(self):
        self.assertFalse(HdsHidTransport().recover())


if __name__ == "__main__":
    unittest.main()
