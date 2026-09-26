"""Test package.

One piece of housekeeping, applied once for every test module: the hand-set
calibration in waveform_data is neutralised.

HDS_MANUAL_CALIBRATION_TRIM is meant to be edited by whoever is at the bench, so
the suite must not depend on whatever number is sitting there - the numbers the
tests assert are the instrument's, not a bench's. The tests that are ABOUT the
trim set it themselves with patch.object, which still wins over this.
"""

import waveform_data

waveform_data.HDS_MANUAL_CALIBRATION_TRIM = None
