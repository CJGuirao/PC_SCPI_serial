"""What the instrument measures, and how a reading becomes a number.

An HDS reply carries two things the panel's face values do not: a range
qualifier (">4.46" means "at least 4.46") and, on the time nodes, picoseconds
instead of seconds.  Both are handled here, once, rather than in every caller.

The mixin sits on the controller and uses only ``self.query`` plus the family's
measurement items, so the dialect decides which nodes are asked and this decides
what the answers mean.
"""

from modernlab.instrument.dialect.hds import HDS_MEASUREMENT_ITEMS


class Measurements:
    """Readings, as the instrument states them and as numbers."""


    #: Measurement nodes whose replies are in picoseconds, not seconds, held in
    #: upper case because they are matched against an upper-cased node name.
    #: A 1 kHz square wave answers PERiod? with "1.0000e+09", i.e. 1 ms in ps;
    #: the same convention applies to the width measurements.
    HDS_PICOSECOND_ITEMS = ("PERIOD", "PWIDTH", "NWIDTH", "RISETIME", "FALLTIME")

    @classmethod
    def parse_measurement(cls, text, node=None):
        """' >4.4600e+00' -> 4.46, and '1.0000e+09' on a time node -> 1e-3 s.

        The instrument prefixes a value with '>' or '<' when it falls outside
        the range it guarantees.  The number is still the best answer it has, so
        the qualifier is stripped rather than the reading thrown away.  A
        non-numeric reply returns None; callers must not assume a number.
        """
        if text is None:
            return None
        token = str(text).strip().lstrip("><= ")
        try:
            value = float(token)
        except ValueError:
            return None
        if node and str(node).upper() in cls.HDS_PICOSECOND_ITEMS:
            value /= 1e12
        return value

    def get_measurements_numeric(self, channel=1):
        """Measurements as floats in volts / hertz / seconds.

        Use this rather than :meth:`get_all_measurements` when the values are to
        be computed with: the display-oriented method returns the instrument's
        raw strings, which carry qualifiers and picosecond time units.
        """
        if not self.is_hds:
            return {}
        return {label: value for label, value in
                ((label, self.parse_measurement(self.query(":MEASUrement:CH%s:%s?" % (channel, node)), node))
                 for label, node in self.HDS_MEASUREMENT_ITEMS)
                if value is not None}

    @staticmethod
    def is_qualified(text):
        """True when the instrument flagged a reading as outside its range.

        Such a number is a bound, not a value: '>4.46' means 'at least 4.46'.
        Callers comparing it against a decoded waveform must treat the result as
        inconclusive rather than as a mismatch.
        """
        return str(text).strip()[:1] in (">", "<") if text is not None else False

    def verify_scale(self, channel=1, tolerance=0.05):
        """Check the decoded volts axis against the instrument's own reading.

        The vertical scale reported by ``:CHn:SCALe?`` can disagree with the
        scale the acquisition is actually using: on HDS271 firmware V1.3.0 a
        scale WRITE is accepted and echoed without changing the captured gain,
        which would silently mis-scale the decoded volts.  Comparing the decoded
        RMS against the instrument's own Vrms catches that, and the reading's
        range qualifier decides whether the comparison means anything.

        Returns a dict with the numbers used and a verdict, never a bare bool,
        because "could not tell" is a real and common answer.
        """
        verdict = {"channel": channel, "volts_per_div": None, "decoded_vrms": None,
                   "instrument_vrms": None, "relative_error": None,
                   "conclusive": False, "consistent": False, "reason": ""}
        if not self.is_hds or not self.waveform.channels:
            verdict["reason"] = "no capture available; call download_waveform_data() first"
            return verdict

        entry = None
        for candidate in self.waveform.channels:
            if str(candidate.get("name", "")).upper() == "CH%s" % channel:
                entry = candidate
                break
        if entry is None:
            verdict["reason"] = "channel CH%s is not in the capture" % channel
            return verdict

        verdict["volts_per_div"] = entry.get("volts_per_div")
        volts = entry.get("waveform") or []
        if not volts:
            verdict["reason"] = "capture holds no samples for CH%s" % channel
            return verdict
        decoded = math.sqrt(sum(value * value for value in volts) / len(volts))
        verdict["decoded_vrms"] = decoded

        raw = self.query(":MEASUrement:CH%s:RMS?" % channel)
        instrument = self.parse_measurement(raw, "RMS")
        verdict["instrument_vrms"] = instrument
        if instrument is None:
            verdict["reason"] = "the instrument did not report Vrms"
            return verdict
        if self.is_qualified(raw):
            verdict["reason"] = ("the instrument flagged its Vrms as %s, so it is a bound "
                                 "rather than a value; comparison inconclusive" % str(raw).strip())
            return verdict
        if not decoded:
            verdict["reason"] = "decoded RMS is zero; nothing to compare"
            return verdict

        error = abs(decoded - instrument) / abs(instrument)
        verdict["relative_error"] = error
        verdict["conclusive"] = True
        verdict["consistent"] = error <= tolerance
        verdict["reason"] = "decoded RMS within %.1f%% of the instrument" % (tolerance * 100) \
            if verdict["consistent"] else \
            "decoded RMS differs from the instrument by %.1f%%" % (error * 100)
        return verdict
