"""CAN bus decoder (Classical CAN, 11-bit and 29-bit identifiers).

Single-wire (CAN-H or the differential signal after a probe).
Only needs one channel — no need for CAN-L.

Usage::

    from modernlab.decode.can import decode
    frames = decode(times, volts, bit_rate=500000)
"""
import numpy as np
from .common import Frame, threshold_crossings, sample_at, auto_threshold

# Standard CAN bit rates (bits/s).
STANDARD_BIT_RATES = [10000, 20000, 50000, 100000, 125000, 250000, 500000,
                       800000, 1000000]


def decode(times, volts, bit_rate=None, threshold=None,
           extended_id=False, channel="CAN"):
    """Decode a CAN signal.

    Parameters
    ----------
    times, volts : array-like
        The capture.  Connect the scope to CAN-H (dominant = HIGH) or the
        differentially-probed signal (dominant = HIGH > threshold).
    bit_rate : int or None
        Bits per second.  When None the decoder estimates from the shortest pulse.
    threshold : float or None
        Dominant/recessive threshold.  When None, auto-detected.
    extended_id : bool
        When True, look for 29-bit identifiers (CAN extended frame).
        When False (default), always use 11-bit base frames.

    Returns
    -------
    list of Frame
        Kinds: "sof", "id", "control", "data", "crc", "eof", "error".
    """
    t = np.asarray(times, dtype=float)
    v = np.asarray(volts, dtype=float)
    if t.size < 8:
        return []

    thr = threshold if threshold is not None else auto_threshold(v)

    # CAN is NRZ with bit stuffing: a sequence of 5 identical bits is followed
    # by a complementary stuff bit.  We work at the bit level.
    if bit_rate is None:
        # Estimate from the shortest dominant (LOW) pulse.
        from .common import estimate_baud
        bit_rate = estimate_baud(t, v, thr)
    if not bit_rate:
        return [Frame("CAN", "error", float(t[0]), float(t[-1]),
                      label="Cannot estimate bit rate", error="no_rate",
                      channel=channel)]

    bit_time = 1.0 / bit_rate

    def sample_bit(t_centre):
        return int(sample_at(t, v, t_centre, thr))  # 1=recessive, 0=dominant

    # CAN SOF: a single dominant (0) bit after a period of recessive (1) idle.
    rising, falling = threshold_crossings(t, v, thr)

    frames = []
    min_idle = bit_time * 9.0    # IFS=3 + EOF=7 = 10 recessive; use 9 for interpolation slack

    # Find SOF candidates: falling edges preceded by at least 11 bits of idle.
    last_recessive_start = float(t[0])
    sof_candidates = []
    prev_edge = float(t[0])
    # The line may start in recessive; the first falling edge from idle is an SOF.
    for t_fall in falling:
        if t_fall - prev_edge >= min_idle:
            sof_candidates.append(t_fall)
        prev_edge = t_fall

    if not sof_candidates:
        return [Frame("CAN", "error", float(t[0]), float(t[-1]),
                      label="No SOF found (no dominant bit after idle)", error="no_sof",
                      channel=channel)]

    for t_sof in sof_candidates:
        # SOF centre.
        t_c = t_sof + bit_time * 0.5
        if t_c > t[-1]:
            break
        if sample_bit(t_c) != 0:
            continue  # not dominant

        tc = t_sof  # running clock base
        frames.append(Frame("CAN", "sof", t_sof, t_sof + bit_time,
                            label="SOF", channel=channel))
        tc += bit_time

        # Read bits with de-stuffing.
        # last_bit/consecutive state persists ACROSS calls to read_bits_destuffed
        # within one frame, because stuffing spans field boundaries.
        _stuff_state = [1, 1]   # [last_bit, consecutive]; SOF was dominant(0), after
                                # which the ID begins in recessive or dominant.

        def read_bits_destuffed(n):
            """Read n data bits, skipping stuff bits. Returns (list_of_bits, t_after)."""
            nonlocal tc
            bits = []
            while len(bits) < n:
                if tc + bit_time * 0.5 > t[-1]:
                    return bits, tc
                b = sample_bit(tc + bit_time * 0.5)
                tc += bit_time
                last_bit, consecutive = _stuff_state
                if consecutive >= 5 and b != last_bit:
                    # Stuff bit — discard and reset consecutive count.
                    _stuff_state[0] = b
                    _stuff_state[1] = 1
                    continue
                if b == last_bit:
                    _stuff_state[1] = consecutive + 1
                else:
                    _stuff_state[0] = b
                    _stuff_state[1] = 1
                bits.append(b)
            return bits, tc

        # Identifier: 11 bits (base) or 29 bits (extended).
        id_len = 29 if extended_id else 11
        id_bits, _ = read_bits_destuffed(id_len)
        if len(id_bits) < id_len:
            frames.append(Frame("CAN", "error", t_sof, tc,
                                label="Truncated ID", error="short", channel=channel))
            continue

        can_id = 0
        for b in id_bits:
            can_id = (can_id << 1) | b

        t_id_end = tc
        frames.append(Frame("CAN", "id", t_sof + bit_time, t_id_end,
                            value=can_id,
                            label="ID 0x%03X" % can_id if not extended_id
                                  else "ID 0x%08X" % can_id,
                            channel=channel))

        # RTR / SRR / IDE bits.
        ctrl_bits, _ = read_bits_destuffed(7)   # RTR, IDE/r1, r0, DLC (4 bits)
        if len(ctrl_bits) < 7:
            continue
        rtr = ctrl_bits[0]
        dlc = sum(b << (3 - i) for i, b in enumerate(ctrl_bits[3:7]))
        dlc = min(dlc, 8)  # DLC > 8 is illegal but we clamp to avoid runaway.

        frames.append(Frame("CAN", "control", t_id_end, tc,
                            label="DLC=%d %s" % (dlc, "RTR" if rtr else "DATA"),
                            channel=channel, fields={"dlc": dlc, "rtr": rtr}))

        if not rtr:
            # Data bytes.
            for byte_idx in range(dlc):
                t_b_start = tc
                byte_bits, _ = read_bits_destuffed(8)
                if len(byte_bits) < 8:
                    break
                # CAN sends MSB first.
                byte_val = sum(b << (7 - i) for i, b in enumerate(byte_bits))
                ch = chr(byte_val) if 0x20 <= byte_val <= 0x7e else "."
                frames.append(Frame("CAN", "data", t_b_start, tc,
                                    value=byte_val,
                                    label="0x%02X '%s'" % (byte_val, ch),
                                    channel=channel))

        # CRC (15 bits) + delimiter.
        crc_bits, _ = read_bits_destuffed(15)
        if len(crc_bits) == 15:
            crc_val = sum(b << (14 - i) for i, b in enumerate(crc_bits))
            frames.append(Frame("CAN", "crc", tc - bit_time * 15, tc,
                                value=crc_val, label="CRC 0x%04X" % crc_val,
                                channel=channel))

        # ACK slot + EOF (7 recessive bits).
        frames.append(Frame("CAN", "eof", tc, tc + bit_time * 10,
                            label="ACK+EOF", channel=channel))

    return frames


def describe(frames):
    """One-line summary."""
    if not frames:
        return "CAN: no frames decoded."
    ids = [f for f in frames if f.kind == "id"]
    data = [f for f in frames if f.kind == "data"]
    errs = [f for f in frames if f.kind == "error"]
    parts = ["CAN: %d frame%s" % (len(ids), "" if len(ids) == 1 else "s")]
    if ids:
        parts.append("IDs: %s" % " ".join(f.label for f in ids[:4]))
    if data:
        try:
            raw = bytes(f.value for f in data if f.value is not None)
            parts.append("data %r" % raw.decode("ascii", errors="replace")[:40])
        except Exception:
            pass
    if errs:
        parts.append("%d error%s" % (len(errs), "" if len(errs) == 1 else "s"))
    return "  ".join(parts)
