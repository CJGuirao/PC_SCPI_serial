"""Analysis of a captured waveform: spectrum, maths, cursors, and the time axis.

Everything here works on ONE channel's samples plus the interval they arrived at.
That interval is the only time reference a capture carries, and it is the honest
one: the instrument announces its own sample rate ("1MSa/s") which describes the
converter, not what was sent. A SCREEN capture is 300 points, so it arrives at
50 kSa/s whatever the announcement says, and a spectrum labelled from the
announcement would put every peak 20x too high.

None of this needs the instrument. It is arithmetic on data already captured, so
it works on a live frame, a saved file, or a synthetic array in a test.
"""

from __future__ import annotations

import math

import numpy as np

#: The windows the vendor's software offers, by the names it uses.
WINDOWS = ("rectangle", "hanning", "hamming", "blackman")

#: How an FFT's magnitude is expressed. dBV is 20*log10 of the rms volts, so a
#: 1 Vrms sine is 0 dBV and a 25 Vpp one is about +18.9.
FFT_FORMATS = ("dBV", "Vrms", "V")

#: Maths on two traces. A trace is a plain sequence of volts.
MATH_OPS = ("add", "subtract", "multiply", "invert")

#: The timebase ladder, in seconds per division. Kept here so analysis has no
#: dependency on the controller; tests/test_analysis.py asserts this list stays
#: identical to OWONScopeController.TIMEBASE_SCALES.
TIMEBASE_LADDER = [5e-9, 10e-9, 20e-9, 50e-9, 100e-9, 200e-9, 500e-9,
                   1e-6, 2e-6, 5e-6, 10e-6, 20e-6, 50e-6, 100e-6, 200e-6, 500e-6,
                   1e-3, 2e-3, 5e-3, 10e-3, 20e-3, 50e-3, 100e-3, 200e-3, 500e-3,
                   1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0]

#: How many divisions the instrument's screen is wide. The same number the plot
#: draws, so a timebase suggestion lands on a cycle count that matches the screen.
SCREEN_DIVISIONS = 12.0

#: Prefix, longest first, so "meg" is not swallowed by "m" or "g".
_SCALE_PREFIXES = (
    ("meg", 1e6), ("g", 1e9), ("k", 1e3), ("m", 1e-3), ("u", 1e-6),
    ("n", 1e-9), ("p", 1e-12), ("", 1.0),
)

#: The unit letters the instrument writes after the prefix: "500us", "2mv", "5ns".
_SCALE_UNITS = "svaw\u03a9" + "\u00b5"


def samples(channel):
    """The channel's volts as a float array."""
    return np.asarray(channel.get("waveform_data") or [], dtype=float)


def point_interval(channel):
    """Seconds between samples, or 0 when the capture did not say."""
    try:
        return float(channel.get("point_interval") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def sample_times(channel):
    """When each sample was taken, assuming the interval is uniform.

    The header carries one interval for the whole record; there is no timestamp
    per point to use instead.
    """
    values = samples(channel)
    return np.arange(values.size, dtype=float) * point_interval(channel)


def window_values(kind, count):
    """A window function, or all-ones for the rectangular one.

    Only the four the vendor offers, by their names, so a saved spectrum can say
    which was used in words a reader of either program recognises.
    """
    name = str(kind or "hanning").strip().lower()
    if name not in WINDOWS:
        raise ValueError("unknown window %r" % (kind,))
    if name == "rectangle":
        return np.ones(int(count), dtype=float)
    return {"hanning": np.hanning, "hamming": np.hamming, "blackman": np.blackman}[name](int(count))


def scale_to_float(text):
    """Turn a scale as the instrument writes it ("500us", "2mv", "1v") into a number.

    Returns None rather than guessing: a scale that cannot be read is a scale the
    caller should not use.
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = str(text).strip().lower().replace(" ", "")
    if not cleaned:
        return None
    # The instrument writes the unit as well as the prefix: "500us", "2mv", "5ns".
    # The micro sign arrives as either character depending on who wrote the file.
    cleaned = cleaned.replace("\u00b5", "u").replace("\u03bc", "u")
    if cleaned[-1:] in _SCALE_UNITS:
        cleaned = cleaned[:-1]
    for prefix, factor in _SCALE_PREFIXES:
        if prefix and not cleaned.endswith(prefix):
            continue
        number = cleaned[:-len(prefix)] if prefix else cleaned
        try:
            return float(number) * factor
        except ValueError:
            return None
    return None


def format_seconds(value):
    """A duration a person can read at a glance."""
    if value is None:
        return "—"
    magnitude = abs(value)
    for limit, factor, unit in ((1e-9, 1e12, "ps"), (1e-6, 1e9, "ns"),
                                (1e-3, 1e6, "us"), (1.0, 1e3, "ms")):
        if magnitude < limit:
            return "%.3g %s" % (value * factor, unit)
    return "%.4g s" % value


def format_volts(value):
    """A voltage a person can read at a glance."""
    if value is None:
        return "—"
    magnitude = abs(value)
    if magnitude < 1e-3:
        return "%.3g mV" % (value * 1e3)
    if magnitude < 1.0:
        return "%.4g mV" % (value * 1e3)
    return "%.4g V" % value


def format_hz(value):
    """A frequency a person can read at a glance."""
    if value in (None, 0):
        return "—"
    magnitude = abs(value)
    for limit, factor, unit in ((1e3, 1.0, "Hz"), (1e6, 1e-3, "kHz"), (1e9, 1e-6, "MHz")):
        if magnitude < limit:
            return "%.4g %s" % (value * factor, unit)
    return "%.4g GHz" % (value * 1e-9)


def format_rate(value):
    """A sample rate a person can read at a glance: "50 kSa/s", not "5e+04 Sa/s"."""
    if not value:
        return "—"
    for limit, factor, unit in ((1e3, 1.0, "Sa/s"), (1e6, 1e-3, "kSa/s"), (1e9, 1e-6, "MSa/s")):
        if abs(value) < limit:
            return "%.4g %s" % (value * factor, unit)
    return "%.4g GSa/s" % (value * 1e-9)


def nearest_from_ladder(value, ladder=None):
    """The closest step of a 1-2-5 ladder, or None when there is nothing to match."""
    steps = list(ladder or TIMEBASE_LADDER)
    if value is None or not steps:
        return None
    return min(steps, key=lambda step: abs(math.log(step / float(value))))


def timebase_for(frequency, cycles=3.0, divisions=SCREEN_DIVISIONS, ladder=None):
    """A seconds-per-division setting that puts `cycles` cycles across the screen.

    Used by the software autoset: the time axis is the one part of the framing
    this instrument accepts over the interface.
    """
    if not frequency or frequency <= 0:
        return None
    return nearest_from_ladder(cycles / (float(frequency) * float(divisions)), ladder)


def fft_spectrum(channel, window="hanning", fmt="dBV", detrend=True):
    """The single-sided spectrum of a captured channel.

    The frequency axis comes from the capture's own point interval, never from the
    instrument's announced sample rate. The magnitude is scaled so a sine reads its
    own amplitude: the window's coherent gain is divided out, which is why a
    Blackman window does not make every peak look smaller.
    """
    values = samples(channel)
    interval = point_interval(channel)
    if values.size < 4 or interval <= 0:
        return None
    count = values.size
    taper = window_values(window, count)
    centred = values - values.mean() if detrend else values
    spectrum = np.fft.rfft(centred * taper)
    gain = taper.sum()
    amplitude = 2.0 * np.abs(spectrum) / (gain if gain else 1.0)   # volts, peak
    amplitude[0] = abs(spectrum[0]) / (gain if gain else 1.0)      # DC is not doubled
    frequencies = np.fft.rfftfreq(count, d=interval)
    return {
        "frequencies": frequencies,
        "amplitudes": amplitude,
        "unit": _fft_unit(fmt),
        "values": _fft_values(amplitude, fmt),
        "fmt": str(fmt or "dBV").upper(),
        "window": str(window or "hanning").lower(),
        "bin_hz": 1.0 / (interval * count),
        "sample_rate": 1.0 / interval,
        "nyquist": 1.0 / (2.0 * interval),
        "span": interval * (count - 1),
        "points": count,
    }


def _fft_unit(fmt):
    return {"DBV": "dBV", "VRMS": "Vrms", "V": "V"}.get(str(fmt or "dBV").upper(), "dBV")


def _fft_values(amplitude, fmt):
    """Volts peak -> the requested display form."""
    name = str(fmt or "dBV").upper()
    if name == "V":
        return amplitude
    rms = amplitude / math.sqrt(2.0)
    if name == "VRMS":
        return rms
    floor = 1e-12
    return 20.0 * np.log10(np.maximum(rms, floor))


def display_spectrum(spectrum, max_points=1500, peak_ratio=0.02):
    """Frequencies and values thinned for drawing, keeping the real peaks.

    A live capture is 300 points and needs no thinning; a saved deep-memory file
    can be tens of thousands, and a plot of every bin is both slow to draw and
    impossible to read. Only peaks above a fraction of the strongest are kept: a
    windowed sine has hundreds of leakage lobes, and keeping every local maximum
    would defeat the thinning it is supposed to survive.
    """
    if not spectrum:
        return np.array([]), np.array([])
    frequencies, values = spectrum["frequencies"], spectrum["values"]
    if frequencies.size <= max_points:
        return frequencies, values
    amplitudes = spectrum.get("amplitudes")
    stride = int(math.ceil(frequencies.size / float(max_points)))
    keep = {0, frequencies.size - 1} | set(range(0, frequencies.size, stride))
    if amplitudes is not None and amplitudes.size == values.size:
        floor = float(np.max(amplitudes[1:])) * float(peak_ratio) if amplitudes.size > 1 else 0.0
        for index in _local_peaks(values):
            if amplitudes[index] >= floor:
                keep.add(index)
    order = np.array(sorted(keep))
    return frequencies[order], values[order]


def _local_peaks(values):
    if values.size < 3:
        return []
    middle = values[1:-1]
    neighbours = np.where((middle >= values[:-2]) & (middle > values[2:]))[0]
    return [int(i) + 1 for i in neighbours]


def spectrum_peaks(spectrum, count=5, floor_ratio=0.05):
    """The most prominent peaks, largest first, ignoring the DC bin.

    Ranked by the linear amplitude, not by the printed value: in dBV the floor
    sits near -130, so a magnitude comparison would rank the deepest null as the
    biggest peak. The floor is a fraction of the largest amplitude above DC, which
    is the one comparison that means the same thing in volts and in dBV.
    """
    if not spectrum:
        return []
    amplitudes = spectrum.get("amplitudes")
    values = spectrum["values"]
    if amplitudes is None or amplitudes.size < 3:
        return []
    top = float(np.max(amplitudes[1:]))
    floor = top * float(floor_ratio)
    found = []
    for index in _local_peaks(values):
        if index == 0 or amplitudes[index] < floor:
            continue
        found.append({"frequency": float(spectrum["frequencies"][index]),
                      "value": float(values[index]),
                      "amplitude": float(amplitudes[index]),
                      "index": int(index)})
    found.sort(key=lambda item: item["amplitude"], reverse=True)
    return found[:int(count)]


def describe_spectrum(spectrum, count=3):
    """One line for the log: what the spectrum says, and what it cannot.

    The Nyquist limit is stated rather than implied, because a spectrum drawn at
    20 us per point invites reading meaning into frequencies it never saw.
    """
    if not spectrum:
        return "No spectrum: the capture carries no sample interval."
    peaks = spectrum_peaks(spectrum, count=count)
    if not peaks:
        return ("Spectrum: no peaks above the floor. %s, Nyquist %s, %.4g Hz per bin."
                % (format_rate(spectrum["sample_rate"]), format_hz(spectrum["nyquist"]),
                   spectrum["bin_hz"]))
    listed = ", ".join("%s (%s)" % (format_hz(peak["frequency"]),
                                    _format_spectrum_value(peak["value"], spectrum["unit"]))
                       for peak in peaks)
    return ("Spectrum: %s. %s window, %s, Nyquist %s, %.4g Hz per bin."
            % (listed, spectrum["window"].capitalize(), format_rate(spectrum["sample_rate"]),
               format_hz(spectrum["nyquist"]), spectrum["bin_hz"]))


def _format_spectrum_value(value, unit):
    if unit == "dBV":
        return "%.1f dBV" % value
    return "%.4g %s" % (value, unit)


def math_trace(first, second=None, op="subtract"):
    """Arithmetic between two traces, or a single trace inverted.

    Lengths must agree for the two-trace operations; a capture whose channels came
    back with different point counts is a question for the caller, not something to
    silently pad.
    """
    name = str(op or "subtract").lower()
    if name not in MATH_OPS:
        raise ValueError("unknown maths operation %r" % (op,))
    left = np.asarray(first if first is not None else [], dtype=float)
    if name == "invert":
        return -left
    right = np.asarray(second if second is not None else [], dtype=float)
    if left.size == 0 or right.size == 0:
        return np.array([])
    if left.size != right.size:
        raise ValueError("traces have different lengths (%d and %d)" % (left.size, right.size))
    if name == "add":
        return left + right
    if name == "subtract":
        return left - right
    return left * right


def math_label(op, first_name, second_name=None):
    """How the maths trace names itself on the plot."""
    symbol = {"add": "+", "subtract": "−", "multiply": "×", "invert": "inverted"}[str(op).lower()]
    if str(op).lower() == "invert":
        return "%s %s" % (first_name, symbol)
    return "%s %s %s" % (first_name, symbol, second_name or "?")


def xy_pairs(first, second):
    """Channel pairs for an XY (Lissajous) plot, cut to the shorter record."""
    xs = np.asarray(first if first is not None else [], dtype=float)
    ys = np.asarray(second if second is not None else [], dtype=float)
    count = min(xs.size, ys.size)
    if count == 0:
        return np.array([]), np.array([])
    return xs[:count], ys[:count]


def value_at(times, values, when):
    """The trace's value at a time, interpolated, or None outside the record."""
    times = np.asarray(times if times is not None else [], dtype=float)
    values = np.asarray(values if values is not None else [], dtype=float)
    if times.size < 2 or values.size != times.size or when is None:
        return None
    if when < times[0] or when > times[-1]:
        return None
    return float(np.interp(float(when), times, values))


def cursor_readings(traces, vertical_times=(None, None), horizontal_volts=(None, None)):
    """What a pair of cursors reads off the traces.

    `traces` maps a name to (times, volts) already at display scale, because a
    cursor must read the trace the way it is drawn. Vertical cursors mark two
    times and give the interval, its reciprocal, and each trace's value at both;
    horizontal cursors mark two voltages and give their difference. Either pair
    may be unset, and a reading that cannot be taken comes back None rather than
    as a plausible number.
    """
    first_time, second_time = (list(vertical_times) + [None, None])[:2]
    first_volt, second_volt = (list(horizontal_volts) + [None, None])[:2]
    reading = {
        "t1": first_time, "t2": second_time,
        "dt": None, "frequency": None,
        "v1": first_volt, "v2": second_volt, "dv": None,
        "traces": {},
    }
    if first_time is not None and second_time is not None:
        delta = abs(float(second_time) - float(first_time))
        reading["dt"] = delta
        reading["frequency"] = (1.0 / delta) if delta else None
    if first_volt is not None and second_volt is not None:
        reading["dv"] = abs(float(second_volt) - float(first_volt))
    for name, (times, volts) in (traces or {}).items():
        entry = {"at_t1": None, "at_t2": None, "dv": None}
        entry["at_t1"] = value_at(times, volts, first_time)
        entry["at_t2"] = value_at(times, volts, second_time)
        if entry["at_t1"] is not None and entry["at_t2"] is not None:
            entry["dv"] = entry["at_t2"] - entry["at_t1"]
        reading["traces"][name] = entry
    return reading


def describe_cursors(reading):
    """The cursor pair as one line, for the panel and the log."""
    if not reading:
        return "Cursors: none placed."
    placed = sum(1 for key in ("t1", "t2", "v1", "v2") if reading.get(key) is not None)
    if not placed:
        return "Cursors: none placed."
    parts = []
    if reading["dt"] is not None:
        parts.append("ΔT %s" % format_seconds(reading["dt"]))
        if reading["frequency"]:
            parts.append("1/ΔT %s" % format_hz(reading["frequency"]))
    if reading["dv"] is not None:
        parts.append("ΔV %s" % format_volts(reading["dv"]))
    for name, entry in (reading.get("traces") or {}).items():
        if entry.get("dv") is not None:
            parts.append("%s Δ %s" % (name, format_volts(entry["dv"])))
    return "Cursors: " + (", ".join(parts) if parts else "one marker placed.")
