"""Test package.

Housekeeping, applied once for every test module: the settings in this project
that are meant to be edited by hand are neutralised, so the suite describes the
CODE rather than whoever's bench happens to be plugged into the machine.

Both of them are real state on a working bench - the volts gain trim, and
scope_setup.json holding the instrument's serial and its calibration - and a
developer who has set either would otherwise see unrelated tests fail. The tests
that are ABOUT them set their own values with patch.object, or their own path,
which still win over this.
"""

import os
import tempfile

from modernlab.settings import bench
from modernlab.instrument.capture import waveform

#: No hand-set trim. The numbers the decode tests assert are the instrument's.
waveform.HDS_MANUAL_CALIBRATION_TRIM = None

#: A settings path that is never written and never present, so every App built in
#: a test starts unconfigured: no serial to open, no trim to apply.
bench.DEFAULT_PATH = os.path.join(tempfile.gettempdir(),
                                        "scope_setup_never_written_by_tests.json")
if os.path.exists(bench.DEFAULT_PATH):                 # pragma: no cover - env
    try:
        os.remove(bench.DEFAULT_PATH)
    except OSError:
        pass
