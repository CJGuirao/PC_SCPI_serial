"""Modern Lab - a desktop front panel for OWON oscilloscopes.

The package is split by domain, so a reader can take one part at a time:

    instrument/   talking to the hardware: transport, protocol, capture decoding
    analysis/     numbers from samples: spectrum, maths, cursors, time axis
    storage/      getting captures in and out: CSV, JSON, XLSX, PNG, PDF
    settings/     the bench: which scope, and the calibration for it
    app/          the application itself: the I/O worker and the window

Nothing here imports the UI, and the UI imports these - never the other way
round, which is what keeps each part testable without a window.
"""

import os

#: The project directory: the parent of this package. Anything that belongs to the
#: installation rather than to a module - the bench settings beside the app, the logs -
#: resolves from here. Deriving it per module instead would silently move those files
#: the moment a module is moved into a subpackage, which is exactly what happened to
#: scope_setup.json when this package was created.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
