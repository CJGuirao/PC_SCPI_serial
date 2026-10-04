"""Numbers derived from samples, with no instrument and no UI.

Spectrum, maths traces, cursors and the time axis. Everything here takes arrays
and returns arrays, which is why it is the easiest part of the project to test.

This package is the facade for the layer: import it as ``analysis`` and these names
are what you get, so callers never need to know which module inside holds what.
Splitting the module further means changing this list, not the callers.
"""

from .analysis import (
    FFT_FORMATS,
    MATH_OPS,
    MATH_OPS_EXTENDED,
    SCREEN_DIVISIONS,
    TIMEBASE_LADDER,
    WINDOWS,

    cursor_readings,
    describe_cursors,
    describe_spectrum,
    display_spectrum,
    fft_spectrum,
    format_hz,
    format_rate,
    format_seconds,
    format_volts,
    harmonic_analysis,
    math_label,
    math_label_extended,
    math_trace,
    math_trace_extended,
    nearest_from_ladder,
    point_interval,
    sample_times,
    samples,
    scale_to_float,
    spectrum_peaks,
    timebase_for,
    value_at,
    window_values,
    waveform_measurements,
    xy_pairs,
)

__all__ = [
    "FFT_FORMATS",
    "MATH_OPS",
    "MATH_OPS_EXTENDED",
    "SCREEN_DIVISIONS",
    "TIMEBASE_LADDER",
    "WINDOWS",
    "cursor_readings",
    "describe_cursors",
    "describe_spectrum",
    "display_spectrum",
    "fft_spectrum",
    "format_hz",
    "format_rate",
    "format_seconds",
    "format_volts",
    "harmonic_analysis",
    "math_label",
    "math_label_extended",
    "math_trace",
    "math_trace_extended",
    "nearest_from_ladder",
    "point_interval",
    "sample_times",
    "samples",
    "scale_to_float",
    "spectrum_peaks",
    "timebase_for",
    "value_at",
    "window_values",
    "waveform_measurements",
    "xy_pairs",
]
