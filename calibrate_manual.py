"""Work out the trim for HDS_MANUAL_CALIBRATION_TRIM from a live capture.

    .venv\\Scripts\\python.exe calibrate_manual.py 25.0

Put a signal on the input whose amplitude you trust, give this that amplitude in
volts peak-to-peak, and it captures through the same path the panel uses, reads
the amplitude and the volts per code the app used, and prints the line to paste
into waveform_data.py along with what the grid will then show.

Why a hand number at all: the automatic calibration pins the scale to the
instrument's own reading, so the app agrees with the instrument. If the generator
is the reference and the instrument is the one that is off, this says so.

It prints a FACTOR, not a volts-per-code: the code span the instrument returns for
one signal moves a few percent capture to capture, and a factor multiplies a scale
measured on the same capture, so that movement cancels.
"""
import os
import re
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from owon_controller import OWONScopeController                  # noqa: E402
from waveform_data import HDS_MANUAL_CALIBRATION_TRIM            # noqa: E402


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    try:
        true_vpp = float(argv[1])
    except ValueError:
        print("give the amplitude in volts peak-to-peak, e.g. 25.0")
        return 2

    scope = OWONScopeController()
    try:
        scope.connect_usb_hid()
        if not scope.download_waveform_data(calibrate=True):
            print("capture failed")
            return 1

        entry = (scope.waveform_data.channels or [{}])[0]
        volts = entry.get("waveform_data") or []
        per_code = entry.get("voltage_per_point") or 0.0
        if not volts or not per_code:
            print("the capture carried no volts: %r" % (entry.get("volts_per_code_source"),))
            return 1

        shown = max(volts) - min(volts)
        print("in use now      : %.6g V per code (%s)"
              % (per_code, entry.get("volts_per_code_source")))
        if HDS_MANUAL_CALIBRATION_TRIM:
            print("trim in force   : x%.6g" % HDS_MANUAL_CALIBRATION_TRIM)
        print("this capture    : %.4g Vpp shown, against %.4g Vpp on your reference"
              % (shown, true_vpp))
        if shown <= 0:
            print("nothing to scale: the capture is flat")
            return 1

        trim = true_vpp / shown
        print("\ntrim needed     : %.6g  (%.4g / %.4g)\n" % (trim, true_vpp, shown))
        print("paste this line, replacing the one that is there now:\n")
        print("HDS_MANUAL_CALIBRATION_TRIM = %.6g\n" % trim)
        print("then the grid reads a %.4g Vpp signal as:" % true_vpp)
        for selected in ("1v", "5v", "10v"):
            value = OWONScopeController.parse_scale(selected)
            if value:
                print("   %-4s V/div -> %6.2f divisions" % (selected, true_vpp / value))
        print("\n(the row is the setting, so these are the numbers on the label too)")
        return 0
    finally:
        scope.disconnect()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
