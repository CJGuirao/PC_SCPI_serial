"""I2C decoder (standard / fast / fast-plus speeds, 7-bit and 10-bit addresses).

Needs SCL + SDA.

Usage::

    from modernlab.decode.i2c import decode
    frames = decode(scl_times, scl_volts, sda_times, sda_volts)
"""
import numpy as np
from .common import Frame, threshold_crossings, sample_at, auto_threshold


def decode(scl_times, scl_volts, sda_times, sda_volts,
           threshold=None, address_bits=7, channel="I2C"):
    """Decode I2C frames from SCL + SDA signals.

    Returns
    -------
    list of Frame
        Includes START, STOP, address, R/W, ACK/NAK and data byte frames.
    """
    t_scl = np.asarray(scl_times, dtype=float)
    v_scl = np.asarray(scl_volts, dtype=float)
    t_sda = np.asarray(sda_times, dtype=float)
    v_sda = np.asarray(sda_volts, dtype=float)
    if t_scl.size < 2 or t_sda.size < 2:
        return []

    thr = threshold if threshold is not None else (
        (auto_threshold(v_scl) + auto_threshold(v_sda)) / 2.0)

    r_scl, f_scl = threshold_crossings(t_scl, v_scl, thr)
    r_sda, f_sda = threshold_crossings(t_sda, v_sda, thr)

    # START: SDA falls while SCL is HIGH.
    # STOP:  SDA rises while SCL is HIGH.
    starts = [t for t in f_sda if sample_at(t_scl, v_scl, t, thr)]
    stops  = [t for t in r_sda if sample_at(t_scl, v_scl, t, thr)]

    if not starts:
        return [Frame("I2C", "error", float(t_scl[0]), float(t_scl[-1]),
                      label="No START condition found", error="no_start",
                      channel=channel)]

    frames = []
    rising_scl = sorted(r_scl)

    def next_rising_scl(after):
        for e in rising_scl:
            if e > after:
                return e
        return None

    def read_bits(n, after_t):
        """Sample n bits from SDA on the rising edges of SCL after after_t."""
        bits = []
        edges_used = []
        for e in rising_scl:
            if e <= after_t:
                continue
            if len(bits) >= n:
                break
            bits.append(int(sample_at(t_sda, v_sda, e, thr)))
            edges_used.append(e)
        return bits, edges_used

    i = 0
    while i < len(starts):
        t_start = starts[i]
        t_next_stop = next((s for s in stops if s > t_start), None)
        t_next_start = starts[i + 1] if i + 1 < len(starts) else None
        t_end = min(x for x in [t_next_stop, t_next_start, float(t_scl[-1])]
                    if x is not None)

        frames.append(Frame("I2C", "start", t_start, t_start,
                            label="START", channel=channel))

        # Read address (7 or 10 bits) + R/W.
        if address_bits == 7:
            addr_bits, edges = read_bits(8, t_start)
            if len(addr_bits) < 8:
                frames.append(Frame("I2C", "error", t_start, t_end,
                                    label="Incomplete address", error="short",
                                    channel=channel))
                i += 1
                continue
            addr = sum(b << (6 - j) for j, b in enumerate(addr_bits[:7]))
            rw = addr_bits[7]
            t_addr_start = t_start
            t_addr_end = edges[6] if len(edges) > 6 else t_end
            frames.append(Frame("I2C", "address", t_addr_start, t_addr_end,
                                value=addr,
                                label="ADDR 0x%02X %s" % (addr, "R" if rw else "W"),
                                channel=channel,
                                fields={"rw": rw}))
            last_edge = edges[7] if len(edges) > 7 else t_addr_end
        else:
            # 10-bit: first byte = 0b11110xx + RW; second byte = remaining 8 bits.
            bits1, edges1 = read_bits(9, t_start)
            bits2, edges2 = read_bits(9, edges1[-1] if edges1 else t_start)
            if len(bits1) < 9 or len(bits2) < 9:
                frames.append(Frame("I2C", "error", t_start, t_end,
                                    label="Incomplete 10-bit address", error="short",
                                    channel=channel))
                i += 1
                continue
            addr = ((bits1[6] << 9) | (bits1[7] << 8) |
                    sum(b << (7 - j) for j, b in enumerate(bits2[:8])))
            rw = bits1[8]
            frames.append(Frame("I2C", "address", t_start, edges2[7],
                                value=addr,
                                label="ADDR10 0x%03X %s" % (addr, "R" if rw else "W"),
                                channel=channel, fields={"rw": rw}))
            last_edge = edges2[8] if len(edges2) > 8 else edges2[-1]

        # ACK after address.
        ack_edge = next_rising_scl(last_edge)
        if ack_edge:
            ack = int(sample_at(t_sda, v_sda, ack_edge, thr))
            frames.append(Frame("I2C", "ack" if ack == 0 else "nak",
                                ack_edge, ack_edge,
                                label="ACK" if ack == 0 else "NAK",
                                channel=channel))
            last_edge = ack_edge

        # Data bytes until STOP or repeated START.
        while True:
            if last_edge is None:
                break
            # Check if next condition is a STOP or repeated START before 9 bits.
            next_boundary = min(
                x for x in [t_next_stop, t_next_start, float(t_scl[-1])]
                if x is not None and x > last_edge)
            data_bits_list, d_edges = read_bits(8, last_edge)
            if len(data_bits_list) < 8:
                break
            if d_edges and d_edges[-1] > next_boundary:
                break
            byte_val = sum(b << (7 - j) for j, b in enumerate(data_bits_list))
            ch = chr(byte_val) if 0x20 <= byte_val <= 0x7e else "."
            t_b_start = d_edges[0] if d_edges else last_edge
            t_b_end = d_edges[7] if len(d_edges) > 7 else t_b_start
            frames.append(Frame("I2C", "data", t_b_start, t_b_end,
                                value=byte_val,
                                label="0x%02X '%s'" % (byte_val, ch),
                                channel=channel))
            last_edge = d_edges[-1] if d_edges else last_edge
            # ACK/NAK after data byte.
            ack_edge = next_rising_scl(last_edge)
            if ack_edge and ack_edge <= next_boundary:
                ack = int(sample_at(t_sda, v_sda, ack_edge, thr))
                frames.append(Frame("I2C", "ack" if ack == 0 else "nak",
                                    ack_edge, ack_edge,
                                    label="ACK" if ack == 0 else "NAK",
                                    channel=channel))
                last_edge = ack_edge
            else:
                break

        if t_next_stop and (t_next_start is None or t_next_stop < t_next_start):
            frames.append(Frame("I2C", "stop", t_next_stop, t_next_stop,
                                label="STOP", channel=channel))
        i += 1

    return frames


def describe(frames):
    """One-line summary."""
    if not frames:
        return "I2C: no frames decoded."
    addrs = [f for f in frames if f.kind == "address"]
    data = [f for f in frames if f.kind == "data"]
    naks = [f for f in frames if f.kind == "nak"]
    parts = ["I2C:"]
    if addrs:
        parts.append(" ".join(f.label for f in addrs[:4]))
    parts.append("%d data byte%s" % (len(data), "" if len(data) == 1 else "s"))
    if naks:
        parts.append("%d NAK" % len(naks))
    try:
        raw = bytes(f.value for f in data if f.value is not None)
        if raw:
            parts.append("→ %r" % raw.decode("ascii", errors="replace")[:40])
    except Exception:
        pass
    return "  ".join(parts)
