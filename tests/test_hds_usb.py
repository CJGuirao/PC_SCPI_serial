import unittest

from hds_usb import REPORT_SIZE, HdsHidTransport


class FakeEndpoint:
    def __init__(self, incoming=None):
        self.incoming = list(incoming or [])
        self.written = []

    def write(self, data, timeout=None):
        self.written.append(bytes(data))
        return len(data)

    def read(self, size, timeout=None):
        if not self.incoming:
            raise RuntimeError("USBTimeoutError")
        return self.incoming.pop(0)


def transport_with(incoming=None):
    transport = HdsHidTransport()
    transport._ep_out = FakeEndpoint()
    transport._ep_in = FakeEndpoint(incoming)
    transport.is_open = True
    transport.timeout = 0.05
    return transport


class TransportTests(unittest.TestCase):
    def test_write_pads_command_to_one_report(self):
        transport = transport_with()
        transport.write(b"*IDN?\n")
        self.assertEqual(transport._ep_out.written, [b"*IDN?\n" + b"\x00" * (REPORT_SIZE - 6)])

    def test_write_splits_long_payload_across_reports(self):
        transport = transport_with()
        transport.write(b"x" * 100)
        self.assertEqual(len(transport._ep_out.written), 2)
        self.assertTrue(all(len(chunk) == REPORT_SIZE for chunk in transport._ep_out.written))
        self.assertEqual(b"".join(transport._ep_out.written)[:100], b"x" * 100)

    def test_readline_returns_line_and_drops_report_padding(self):
        report = b"OWON,HDS271,25520161,V1.3.0\n".ljust(REPORT_SIZE, b"\x00")
        transport = transport_with([report])
        self.assertEqual(transport.readline(), b"OWON,HDS271,25520161,V1.3.0\n")
        self.assertEqual(bytes(transport._buffer), b"")

    def test_read_is_binary_safe(self):
        report = b"\x00\x01\x02\n\x00\xff" + b"\x00" * (REPORT_SIZE - 6)
        transport = transport_with([report])
        self.assertEqual(transport.read(6), b"\x00\x01\x02\n\x00\xff")
        self.assertEqual(transport.read(1), b"\x00")

    def test_readline_without_newline_returns_trimmed_payload(self):
        transport = transport_with([b"AUTo" + b"\x00" * (REPORT_SIZE - 4)])
        self.assertEqual(transport.readline(), b"AUTo")


if __name__ == "__main__":
    unittest.main()
