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
