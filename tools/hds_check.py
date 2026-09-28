r"""Live check of the OWON HDS USB HID link.

Run from the repository root with the project virtual environment:

    .venv\Scripts\python.exe tools\hds_check.py

It prints the instrument identity, sweeps candidate SCPI commands so the HDS
dialect can be confirmed against real hardware, then exercises the same link
through OWONScopeController.  Read-only apart from one block that sets benign
values and reads them back.
"""

import os
import sys
import time

# Run from the repository root. This script sits one directory below the package, and
# a script's own directory is what Python puts on the path first, so the root has to be
# added before `modernlab` can be imported.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modernlab.instrument.transport.usb_hid import HdsHidTransport, list_owon_devices

CHANNEL_CANDIDATES = [
    ":CH1:SCALe?",
    ":CHAN1:SCALe?",
    ":CHANnel:SCALe? CH1",
    ":CHANnel1:SCALe?",
    ":CH1:OFFSet?",
    ":CH1:COUPling?",
    ":CH1:PROBe?",
    ":CH1:DISPlay?",
]
WAVEFORM_CANDIDATES = [
    ":DATA:WAVE:SCREen:HEAD?",
    ":DATA:WAVE:SCREen:CH1?",
    ":DATA:WAVE:DEPMem:All?",
]
MEASUREMENT_CANDIDATES = [
    ":MEASUrement:CH1:FREQuency?",
    ":MEASUrement:CH1:PKPK?",
    ":MEASUrement:CH1:MAX?",
    ":MEASUrement:CH1?",
    ":MEASUrement:SOURce?",
]
WRITE_CANDIDATES = [
    (":HORIzontal:SCALe 1ms", ":HORIzontal:SCALe?"),
    (":ACQuire:MODE AVERage", ":ACQuire:MODE?"),
    (":ACQuire:MODE SAMPle", ":ACQuire:MODE?"),
    (":ACQuire:DEPMem 10K", ":ACQuire:DEPMem?"),
    (":TRIGger:SINGle:SWEEp AUTO", ":TRIGger:SINGle:SWEEp?"),
    (":TRIGger:SINGle:EDGE:LEVel 100mV", ":TRIGger:SINGle:EDGE:LEVel?"),
]


def main():
    print("=== OWON USB devices ===")
    devices = list_owon_devices()
    print(devices)
    if not devices:
        print("no OWON device on USB - is the scope powered and plugged in?")
        return 1

    transport = HdsHidTransport(serial=devices[0].get("serial"), timeout=0.5)
    transport.open()
    transport.timeout = 0.5
    print("opened:", transport.description)

    def ask(command, wait=0.2):
        transport.write(command.encode("ascii") + b"\n")
        time.sleep(wait)
        reply = transport.readline().decode("utf-8", "replace").strip()
        print("%-40s -> %s" % (command, reply if reply else "<no reply>"))
        return reply

    def ask_binary(command, reports=16):
        transport.write(command.encode("ascii") + b"\n")
        time.sleep(0.3)
        chunk = transport.read(64 * reports)
        print("%-40s -> %d bytes %r" % (command, len(chunk), chunk[:72]))
        transport.clear_input()
        return chunk

    print("=== identity ===")
    ask("*IDN?")

    print("=== channel command discovery ===")
    for command in CHANNEL_CANDIDATES:
        ask(command)

    print("=== waveform path discovery ===")
    ask_binary(WAVEFORM_CANDIDATES[0])
    ask_binary(WAVEFORM_CANDIDATES[1])
    ask_binary(WAVEFORM_CANDIDATES[2])

    print("=== measurement discovery ===")
    for command in MEASUREMENT_CANDIDATES:
        ask(command)

    print("=== write acceptance (set, then read back) ===")
    for command, query in WRITE_CANDIDATES:
        transport.write(command.encode("ascii") + b"\n")
        time.sleep(0.3)
        ask(query)

    transport.close()

    print("=== controller integration ===")
    from owon_controller import OWONScopeController
    scope = OWONScopeController(family="hds")
    if scope.connect_usb_hid(serial=devices[0].get("serial")):
        print("identity:", scope.identify_model())
        print("timebase:", scope.get_timebase_scale())
        print("acquire mode:", scope.get_acquire_type())
        print("trigger status:", scope.get_trigger_status())
        print("channel scale (expected silent until dialect fixed):", scope.get_channel_scale(1))
    else:
        print("controller connection FAILED")
    scope.disconnect()

    print("HDSCheckDone")
    return 0


if __name__ == "__main__":
    sys.exit(main())



