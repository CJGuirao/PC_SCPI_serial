"""Shared primitives for all protocol decoders."""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Frame:
    """One decoded protocol unit: a byte, a field, an error marker, etc."""
    protocol: str           # "UART" | "SPI" | "I2C" | "CAN"
    kind: str               # "data" | "start" | "stop" | "ack" | "nak" | "error" | "id" | …
    t_start: float          # seconds, in the capture's time axis
    t_end: float
    value: Optional[int] = None         # integer value, e.g. byte 0x41 → 65
    label: Optional[str] = None         # human string, e.g. "0x41 'A'" or "ACK"
    channel: Optional[str] = None       # which signal this came from
    error: Optional[str] = None         # framing error description if kind=="error"
    fields: dict = field(default_factory=dict)  # protocol-specific extras

    def __post_init__(self):
        if self.label is None and self.value is not None:
            v = int(self.value)
            ch = chr(v) if 0x20 <= v <= 0x7e else "."
            self.label = "0x%02X '%s'" % (v, ch)


def threshold_crossings(times, volts, threshold, hysteresis=0.0):
    """Return (rising_times, falling_times) where the signal crosses threshold.

    Hysteresis: the signal must exceed threshold±hysteresis/2 before the next
    crossing is counted, which rejects noise near the threshold.
    """
    import numpy as np
    v = np.asarray(volts, dtype=float)
    t = np.asarray(times, dtype=float)
    if v.size < 2:
        return [], []
    hi = threshold + hysteresis / 2.0
    lo = threshold - hysteresis / 2.0

    rising, falling = [], []
    state = v[0] >= threshold   # True = HIGH
    for i in range(1, v.size):
        if not state and v[i] >= hi:
            # Linear interpolation for sub-sample accuracy.
            frac = (hi - v[i - 1]) / (v[i] - v[i - 1]) if v[i] != v[i - 1] else 0.0
            rising.append(float(t[i - 1] + frac * (t[i] - t[i - 1])))
            state = True
        elif state and v[i] <= lo:
            frac = (lo - v[i - 1]) / (v[i] - v[i - 1]) if v[i] != v[i - 1] else 0.0
            falling.append(float(t[i - 1] + frac * (t[i] - t[i - 1])))
            state = False
    return rising, falling


def sample_at(times, volts, t, threshold):
    """Return HIGH/LOW (True/False) at time t by nearest-sample lookup."""
    import numpy as np
    idx = int(np.searchsorted(times, t, side="left"))
    idx = max(0, min(idx, len(volts) - 1))
    return float(volts[idx]) >= threshold


def auto_threshold(volts):
    """Midpoint between the median of the lower and upper half: robust 50% threshold."""
    import numpy as np
    v = np.asarray(volts, dtype=float)
    mid = (v.max() + v.min()) / 2.0
    lo_med = float(np.median(v[v < mid])) if np.any(v < mid) else float(v.min())
    hi_med = float(np.median(v[v >= mid])) if np.any(v >= mid) else float(v.max())
    return (lo_med + hi_med) / 2.0


def estimate_baud(times, volts, threshold=None):
    """Estimate baud rate from the shortest pulse width seen in the signal."""
    import numpy as np
    v = np.asarray(volts, dtype=float)
    t = np.asarray(times, dtype=float)
    if threshold is None:
        threshold = auto_threshold(v)
    rising, falling = threshold_crossings(t, v, threshold)
    edges = sorted(rising + falling)
    if len(edges) < 2:
        return None
    intervals = np.diff(edges)
    intervals = intervals[intervals > 0]
    if intervals.size == 0:
        return None
    shortest = float(np.min(intervals))
    if shortest <= 0:
        return None
    # Round to the nearest standard baud rate.
    raw = 1.0 / shortest
    standard = [300, 1200, 2400, 4800, 9600, 14400, 19200, 38400, 57600,
                115200, 230400, 460800, 921600, 1000000, 2000000]
    return min(standard, key=lambda b: abs(b - raw))
