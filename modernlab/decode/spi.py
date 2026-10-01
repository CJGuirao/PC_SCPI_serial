"""SPI decoder (Mode 0/1/2/3, MSB/LSB first, 4-wire or 3-wire).

Needs at minimum SCK + MOSI.  MISO and CS are optional.

Usage::

    from modernlab.decode.spi import decode
    frames = decode(sck_times, sck_volts, mosi_times, mosi_volts,
                    mode=0, bits=8)
"""
import numpy as np
from .common import Frame, threshold_crossings, sample_at, auto_threshold


def decode(sck_times, sck_volts, mosi_times=None, mosi_volts=None,
           miso_times=None, miso_volts=None,
           cs_times=None, cs_volts=None,
           mode=0, bits=8, msb_first=True,
           threshold=None, cs_active_low=True,
           channel_mosi="MOSI", channel_miso="MISO"):
    """Decode SPI frames.

    Parameters
    ----------
    sck_times / sck_volts : array-like
        The clock signal.
    mosi/miso/cs : optional arrays
        Data and chip-select lines.  When mosi is None the decoder returns
        only clock-edge timing frames (useful for verifying the clock).
    mode : 0–3
        SPI mode (CPOL/CPHA).  Mode 0: CPOL=0 CPHA=0 (sample on rising).
    bits : int
        Bits per word. Default 8.
    msb_first : bool
        True = MSB first (standard).
    threshold, cs_active_low : as in uart.decode.

    Returns
    -------
    list of Frame
    """
    t_clk = np.asarray(sck_times, dtype=float)
    v_clk = np.asarray(sck_volts, dtype=float)
    if t_clk.size < 2:
        return []

    thr_clk = threshold if threshold is not None else auto_threshold(v_clk)

    # CPOL / CPHA from mode.
    cpol = mode >> 1        # 0 or 1
    cpha = mode & 1         # 0 or 1

    # Active clock edge: CPHA=0 → sample on leading edge; CPHA=1 → trailing.
    # Leading edge for CPOL=0 is rising; for CPOL=1 is falling.
    rising_clk, falling_clk = threshold_crossings(t_clk, v_clk, thr_clk)

    if cpol == 0:
        sample_edges = rising_clk if cpha == 0 else falling_clk
    else:
        sample_edges = falling_clk if cpha == 0 else rising_clk

    if not sample_edges:
        return [Frame("SPI", "error", float(t_clk[0]), float(t_clk[-1]),
                      label="No clock edges found", error="no_clock")]

    # CS gating (optional).
    active_windows = None
    if cs_times is not None and cs_volts is not None:
        t_cs = np.asarray(cs_times, dtype=float)
        v_cs = np.asarray(cs_volts, dtype=float)
        thr_cs = threshold if threshold is not None else auto_threshold(v_cs)
        r_cs, f_cs = threshold_crossings(t_cs, v_cs, thr_cs)
        if cs_active_low:
            asserts, deasserts = f_cs, r_cs   # CS asserted = falling
        else:
            asserts, deasserts = r_cs, f_cs
        active_windows = []
        for t_a in asserts:
            t_d = next((x for x in deasserts if x > t_a), float("inf"))
            active_windows.append((t_a, t_d))

    def in_active_window(t):
        if active_windows is None:
            return True
        return any(lo <= t <= hi for lo, hi in active_windows)

    # Group sample edges into words.
    frames = []
    word_edges = [e for e in sample_edges if in_active_window(e)]
    for word_start_idx in range(0, len(word_edges), bits):
        edges = word_edges[word_start_idx: word_start_idx + bits]
        if len(edges) < bits:
            break
        t_ws = edges[0]
        t_we = edges[-1]

        mosi_val = None
        miso_val = None
        if mosi_times is not None and mosi_volts is not None:
            t_mo = np.asarray(mosi_times, dtype=float)
            v_mo = np.asarray(mosi_volts, dtype=float)
            thr_mo = threshold if threshold is not None else auto_threshold(v_mo)
            raw = [int(sample_at(t_mo, v_mo, e, thr_mo)) for e in edges]
            if not msb_first:
                raw = raw[::-1]
            mosi_val = sum(b << (bits - 1 - i) for i, b in enumerate(raw))

        if miso_times is not None and miso_volts is not None:
            t_mi = np.asarray(miso_times, dtype=float)
            v_mi = np.asarray(miso_volts, dtype=float)
            thr_mi = threshold if threshold is not None else auto_threshold(v_mi)
            raw = [int(sample_at(t_mi, v_mi, e, thr_mi)) for e in edges]
            if not msb_first:
                raw = raw[::-1]
            miso_val = sum(b << (bits - 1 - i) for i, b in enumerate(raw))

        if mosi_val is not None:
            ch = chr(mosi_val) if 0x20 <= mosi_val <= 0x7e else "."
            frames.append(Frame("SPI", "data", t_ws, t_we,
                                value=mosi_val,
                                label="MOSI 0x%02X '%s'" % (mosi_val, ch),
                                channel=channel_mosi))
        if miso_val is not None:
            ch = chr(miso_val) if 0x20 <= miso_val <= 0x7e else "."
            frames.append(Frame("SPI", "data", t_ws, t_we,
                                value=miso_val,
                                label="MISO 0x%02X '%s'" % (miso_val, ch),
                                channel=channel_miso))
        if mosi_val is None and miso_val is None:
            frames.append(Frame("SPI", "clock", t_ws, t_we,
                                label="%d CLK edges" % len(edges)))

    return frames


def describe(frames):
    """One-line summary."""
    if not frames:
        return "SPI: no frames decoded."
    mosi = [f for f in frames if "MOSI" in (f.channel or "")]
    miso = [f for f in frames if "MISO" in (f.channel or "")]
    parts = ["SPI: %d word%s" % (len(mosi) or len(frames),
                                  "" if (len(mosi) or len(frames)) == 1 else "s")]
    if mosi:
        try:
            raw = bytes(f.value for f in mosi if f.value is not None)
            parts.append("MOSI %r" % raw.decode("ascii", errors="replace")[:40])
        except Exception:
            pass
    if miso:
        try:
            raw = bytes(f.value for f in miso if f.value is not None)
            parts.append("MISO %r" % raw.decode("ascii", errors="replace")[:40])
        except Exception:
            pass
    return "  ".join(parts)
