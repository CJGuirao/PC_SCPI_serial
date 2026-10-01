"""UART / RS-232 / TTL serial decoder.

Decodes 8N1, 8E1, 8O1, 7N1, etc.  Handles both idle-high (standard UART/TTL)
and idle-low (RS-232 after level shifting, where the scope sees the inverted
logic) via the ``invert`` flag.

Usage::

    from modernlab.decode.uart import decode
    frames = decode(times, volts, baud=115200)
    for f in frames:
        print(f.t_start, f.label, f.error)
"""
import numpy as np
from .common import Frame, threshold_crossings, sample_at, auto_threshold, estimate_baud

STANDARD_BAUDS = [300, 1200, 2400, 4800, 9600, 14400, 19200, 38400,
                  57600, 115200, 230400, 460800, 921600, 1000000, 2000000]


def decode(times, volts, baud=None, threshold=None, data_bits=8, stop_bits=1,
           parity="none", invert=False, channel="CH1"):
    """Decode a UART signal into a list of Frame objects.

    Parameters
    ----------
    times, volts : array-like
        The capture's time axis and voltage samples.
    baud : int or None
        Baud rate.  When None the decoder estimates it from the shortest pulse.
    threshold : float or None
        Logic threshold in volts.  When None the midpoint of the signal swing
        is used (``auto_threshold``).
    data_bits : int
        5–9.  Default 8.
    stop_bits : int or float
        1, 1.5 or 2.
    parity : str
        "none", "even", "odd", "mark", "space".
    invert : bool
        True for idle-low / RS-232 level-shifted signals.
    channel : str
        Label used in the returned frames.

    Returns
    -------
    list of Frame
    """
    t = np.asarray(times, dtype=float)
    v = np.asarray(volts, dtype=float)
    if t.size < 4:
        return []

    if threshold is None:
        threshold = auto_threshold(v)

    # Invert the logic levels so the rest of the code always sees idle-HIGH.
    if invert:
        v = -v
        threshold = -threshold

    # Detect start bits: falling edges from idle-HIGH to LOW.
    rising, falling = threshold_crossings(t, v, threshold, hysteresis=0.0)

    if baud is None:
        baud = estimate_baud(t, v, threshold)
    if not baud:
        return [Frame("UART", "error", float(t[0]), float(t[-1]), channel=channel,
                      label="Cannot estimate baud rate", error="no_baud")]

    bit_time = 1.0 / baud
    frames = []

    # Each falling edge is a candidate start bit.
    skip_until = -1.0
    for t_fall in falling:
        if t_fall < skip_until:
            continue
        # The start bit centre is half a bit after the falling edge.
        t_start_centre = t_fall + bit_time * 0.5
        if t_start_centre > t[-1]:
            break
        # Confirm the line is still LOW at the centre of the start bit.
        if sample_at(t, v, t_start_centre, threshold):
            continue  # noise glitch — skip

        # Sample each data bit at its centre.
        bits = []
        for bit_idx in range(data_bits):
            tc = t_fall + bit_time * (1.0 + bit_idx + 0.5)
            bits.append(int(sample_at(t, v, tc, threshold)))

        # LSB first (standard UART).
        byte_value = 0
        for i, b in enumerate(bits):
            byte_value |= (b << i)

        error = None
        # Parity check.
        if parity in ("even", "odd"):
            t_par = t_fall + bit_time * (1.0 + data_bits + 0.5)
            par_bit = int(sample_at(t, v, t_par, threshold))
            expected = (bin(byte_value).count("1") % 2 == 0)
            if parity == "odd":
                expected = not expected
            if bool(par_bit) != expected:
                error = "parity"

        # Stop bit check.
        t_stop = t_fall + bit_time * (1.0 + data_bits +
                                       (1 if parity != "none" else 0) + 0.5)
        if t_stop <= t[-1]:
            stop_high = sample_at(t, v, t_stop, threshold)
            if not stop_high:
                error = "framing"

        t_frame_end = t_fall + bit_time * (1.0 + data_bits +
                                            (1 if parity != "none" else 0) +
                                            float(stop_bits))

        ch = chr(byte_value) if 0x20 <= byte_value <= 0x7e else "."
        label = "0x%02X '%s'" % (byte_value, ch)
        if error:
            label += " [%s err]" % error

        frames.append(Frame(
            protocol="UART",
            kind="error" if error else "data",
            t_start=float(t_fall),
            t_end=min(float(t_frame_end), float(t[-1])),
            value=byte_value,
            label=label,
            channel=channel,
            error=error,
        ))
        skip_until = t_frame_end

    return frames


def describe(frames):
    """One-line summary of a UART decode result."""
    if not frames:
        return "UART: no frames decoded."
    errors = [f for f in frames if f.kind == "error"]
    data = [f for f in frames if f.kind == "data"]
    text = "UART: %d byte%s" % (len(frames), "" if len(frames) == 1 else "s")
    if errors:
        text += "  (%d error%s)" % (len(errors), "" if len(errors) == 1 else "s")
    try:
        raw = bytes(f.value for f in data if f.value is not None)
        printable = raw.decode("ascii", errors="replace").replace("\x00", ".")
        if printable:
            text += "  → %r" % printable[:80]
    except Exception:
        pass
    return text
