"""The HDS dialect: channel nodes, trigger nodes, and what a reply means.

Verified against an HDS271, firmware V1.3.0:

* channels are addressed as ``:CH1:SCALe?`` -- never ``:CHANnel1:SCALe?``
* measurements answer per item only (``:MEASUrement:CH1:FREQuency?``);
  ``:MEASUrement:CH1?`` itself is silent
* replies come back in engineering notation (``5e-04``, ``5e+00``) and with loose
  enum spelling (``AUTo``), which the front panel's choice lists do not match, so
  replies are mapped back onto the lists the front panel uses
* the manual's ``:TRIGger:SINGle:EDGE:SLOPe`` and
  ``:TRIGger:SINGle:EDGE:COUPling`` do not exist and are actively harmful -- the
  scope answers nothing and the setting silently does not change -- so the node
  tables below come from write-then-read-back, not from the manual

The parsing and matching helpers are pure functions as well as classmethods, so
the numbers can be checked without an instrument in the picture.
"""

import re

#: Mnemonics the manual documents for the multimeter, mapped from what a UI might
#: call them.
DMM_NODES = {
    "VOLTAGE": "VOLTage", "VOLT": "VOLTage", "V": "VOLTage",
    "CURRENT": "CURRent", "CURR": "CURRent", "A": "CURRent", "AMP": "CURRent",
    "RESISTANCE": "RESistance", "RES": "RESistance", "OHM": "RESistance",
    "DIODE": "DIODe", "CONTINUITY": "CONTinuity", "CAPACITANCE": "CAPacitance",
}

#: The two the sub-type (AC/DC) query exists for.
DMM_TYPED = ("VOLTage", "CURRent")

#: What the series accepts for acquisition mode and capture depth.  The SDS
#: ladder in :mod:`modernlab.instrument.dialect.sds` is a different instrument's
#: and is deliberately NOT overwritten, since one controller serves both.
ACQUIRE_MODES = ("SAMPle", "PEAK")
HDS_MEMORY_DEPTHS = ("4K", "8K")

#: The measurement items answered per channel, in panel order.  ``RMS``, ``VAMP``
#: and the widths answer on this firmware too and are read where they are wanted
#: (see ``Measurements.verify_scale``); the panel's own list is these six.
HDS_MEASUREMENT_ITEMS = (
    ("Frequency", "FREQuency"),
    ("Period", "PERiod"),
    ("Vpp", "PKPK"),
    ("Vmax", "MAX"),
    ("Vmin", "MIN"),
    ("Vmean", "AVERage"),
    ("Vrms", "RMS"),
    ("Vamp", "VAMP"),
    ("Pwidth", "PWIDth"),
    ("Nwidth", "NWIDth"),
)

SI_FACTORS = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6}


def parse_scale(text, factors=SI_FACTORS):
    """'500us', '5v', '1ms', '5e-04' -> float in base units."""
    if text is None:
        return None
    match = re.match(r"^\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*([A-Za-z]*)\s*$", str(text))
    if not match:
        return None
    try:
        number = float(match.group(1))
    except ValueError:
        return None
    unit = match.group(2)
    if len(unit) > 1 and unit[0] in factors:
        number *= factors[unit[0]]
    return number


def match_scale(reply, choices):
    """'5e-04' -> '500us': the choice list entry the scope's reply equals."""
    wanted = parse_scale(reply)
    if wanted is None:
        return None
    for choice in choices:
        value = parse_scale(choice)
        if value is None:
            continue
        if abs(value - wanted) <= max(abs(wanted), 1e-12) * 1e-6:
            return choice
    return None


def match_choice(reply, choices):
    """'AUTo' -> 'AUTO': case-insensitive match against the choice list."""
    if reply in (None, ""):
        return None
    token = str(reply).strip().lower()
    for choice in choices:
        if str(choice).strip().lower() == token:
            return choice
    return None


class HdsDialect:
    """The HDS command nodes and the reply shapes, mixed onto the controller.

    Nothing here is a fallback: where the SDS dialect answers these questions
    differently the controller routes on the family and calls one base or the
    other, so no definition shadows another and no alias is needed to keep a
    fallback out of a loop.
    """


    #: The depth ladder the HDS block carried as an instance-visible list.  It is
    #: kept because it is what the panel reads back after a write; the choices the
    #: panel OFFERS are ``HDS_MEMORY_DEPTHS`` above, which is what the manual says.
    MEMORY_DEPTHS = ["4K", "1K", "10K", "100K", "1M", "10M"]  # HDS reports 4K

    #: The reply-normalising helpers are the module-level functions above; they are
    #: bound here as classmethods so ``scope.parse_scale(...)`` and
    #: ``OWONScopeController.parse_scale(...)`` keep resolving, and one copy of the
    #: arithmetic serves both spellings.
    parse_scale = classmethod(lambda cls, text: parse_scale(text))
    match_scale = classmethod(lambda cls, reply, choices: match_scale(reply, choices))
    match_choice = staticmethod(lambda reply, choices: match_choice(reply, choices))

    # -- channel commands ---------------------------------------------------
    def set_channel_display(self, channel, state):
        return self.send_command(f"{self._channel_node(channel, 'DISPlay')} {'ON' if state else 'OFF'}")

    def get_channel_display(self, channel):
        # the HDS271 does not answer this; None leaves the widget untouched
        return self.query(self._channel_node(channel, "DISPlay?"))

    def set_channel_coupling(self, channel, coupling):
        return self.send_command(f"{self._channel_node(channel, 'COUPling')} {coupling}")

    def get_channel_coupling(self, channel):
        reply = self.query(self._channel_node(channel, "COUPling?"))
        return self.match_choice(reply, self.COUPLING_MODES) or reply

    def set_channel_probe(self, channel, attenuation):
        result = self.send_command(f"{self._channel_node(channel, 'PROBe')} {attenuation}")
        self.mark_framing_settle()
        return result

    def get_channel_probe(self, channel):
        return self.query(self._channel_node(channel, "PROBe?"))

    def set_channel_scale(self, channel, scale):
        result = self.send_command(f"{self._channel_node(channel, 'SCALe')} {scale}")
        self.mark_framing_settle()
        return result

    def get_channel_scale(self, channel):
        reply = self.query(self._channel_node(channel, "SCALe?"))
        return self.match_scale(reply, self.VOLTAGE_SCALES) or reply

    def set_channel_offset(self, channel, offset):
        result = self.send_command(f"{self._channel_node(channel, 'OFFSet')} {offset}")
        self.mark_framing_settle()
        return result

    def get_channel_offset(self, channel):
        return self.query(self._channel_node(channel, "OFFSet?"))

    # -- timebase / acquisition --------------------------------------------
    def get_timebase_scale(self):
        reply = self.query(f"{self._cmd(':HORIzontal:SCALe?', ':TIMebase:SCALe?')}")
        return self.match_scale(reply, self.TIMEBASE_SCALES) or reply


    def get_trigger_mode(self):
        reply = self.query(":TRIGger:SINGle:SWEEp?" if self.is_hds else ":TRIGger:MODE?")
        return self.match_choice(reply, self.TRIGGER_MODES) or reply

    def get_trigger_source(self):
        reply = self.query(":TRIGger:SINGle:EDGE:SOURce?" if self.is_hds else ":TRIGger:SOURce?")
        return self.match_choice(reply, self.TRIGGER_SOURCES) or reply

    def get_acquire_type(self):
        reply = self.query(f"{self._cmd(':ACQuire:MODE?', ':ACQuire:TYPE?')}")
        return self.match_choice(reply, self.ACQ_TYPES) or reply

    def get_memory_depth_choice(self):
        """The reply matched against the depth ladder -- DEAD in the flat class.

        This definition came first in the old single class; the plain string read
        further down replaced it, so nothing has ever called this.  It is kept
        under its own name, and ``get_memory_depth`` is the one that ran.
        """
        reply = self.query(f"{self._cmd(':ACQuire:DEPMem?', ':ACQuire:MDEPth?')}")
        return self.match_choice(reply, self.MEMORY_DEPTHS) or reply

    # -- transport ---------------------------------------------------------

    def _channel_node(self, channel, node):
        """HDS uses :CH1:..., the SDS dialect uses :CHANnel1:..."""
        return f":CH{channel}:{node}"

    # -- trigger node tables -----------------------------------------------
    #: ``HDS_MEASUREMENT_ITEMS`` is the module-level tuple, bound in here as well
    #: so the class carries it the way the flat class did.
    HDS_MEASUREMENT_ITEMS = HDS_MEASUREMENT_ITEMS
    SI_FACTORS = SI_FACTORS
    #
    # The node forms below were established by write-then-read-back against a
    # live instrument, NOT taken from the manual.  Two manual nodes do not exist
    # on HDS271 firmware V1.3.0 and are actively harmful because the scope
    # answers nothing and the setting silently does not change:
    #
    #   manual says :TRIGger:SINGle:EDGE:SLOPe     real node :TRIGger:SINGle:EDGe
    #   manual says :TRIGger:SINGle:EDGE:COUPling  real node :TRIGger:SINGle:COUPling
    #
    # Everything else in the manual held up.  Each entry is a tuple of
    # candidates in preference order; :meth:`probe_dialect` can confirm the
    # first one that actually answers on an unknown model.

    HDS_DIALECT_HDS200 = {
        "source": (":TRIGger:SINGle:EDGE:SOURce", ":TRIGger:SINGle:SOURce"),
        "slope": (":TRIGger:SINGle:EDGe", ":TRIGger:SINGle:SLOPe", ":TRIGger:SINGle:EDGE:SLOPe"),
        "coupling": (":TRIGger:SINGle:COUPling", ":TRIGger:SINGle:EDGE:COUPling"),
        "level": (":TRIGger:SINGle:EDGE:LEVel", ":TRIGger:SINGle:EDGe:LEVel", ":TRIGger:SINGle:LEVel"),
        "sweep": (":TRIGger:SINGle:SWEEp", ":TRIGger:SINGle:MODE"),
        "holdoff": (":TRIGger:SINGle:HOLDoff",),
    }

    # No HDS300-series instrument was available to test against, and OWON
    # publishes no SCPI manual for it.  The HDS300 shares the HDS200 command
    # core in OWON's own PC software, so the verified HDS200 nodes are the
    # default and the deeper forms are kept as fallbacks.  Treat any HDS300
    # result as unverified until probe_dialect() has run on real hardware.
    HDS_DIALECT_HDS300 = dict(HDS_DIALECT_HDS200)
    HDS_DIALECT_VERIFIED = {"hds200": "HDS271 / V1.3.0", "hds300": None}

    @property
    def dialect(self):
        """Trigger node table for the connected family."""
        if self.series == "hds300":
            return self.HDS_DIALECT_HDS300
        return self.HDS_DIALECT_HDS200

    def trigger_node(self, name):
        """Preferred node for a trigger setting, honouring a probed override."""
        probed = getattr(self, "_probed_nodes", {}).get(name)
        if probed:
            return probed
        return self.dialect[name][0]

    def probe_dialect(self, names=None):
        """Ask the instrument which candidate node each setting really uses.

        Sends one query per candidate and keeps the first that answers.  This is
        the supported way to bring up an untested HDS200/HDS300 model; run it
        deliberately rather than on every connect, because a burst of commands
        the firmware does not implement can reset it off the USB bus.
        """
        if not self.is_hds:
            return {}
        probed = dict(getattr(self, "_probed_nodes", {}))
        for name in (names or list(self.dialect)):
            for node in self.dialect[name]:
                try:
                    reply = self.query(node + "?")
                except Exception:
                    reply = None
                if reply:
                    probed[name] = node
                    break
        self._probed_nodes = probed
        return probed

    # -- trigger settings ---------------------------------------------------
    #: The non-HDS branch calls the legacy dialect's implementation of the same
    #: name through these hooks, which the controller binds.  They keep the
    #: ``legacy_*`` spelling they had before the split, when they were aliases
    #: inside the one class; a base cannot reach the *other* base's same-named
    #: method otherwise, because ``self.set_trigger_slope`` here is this method.
    legacy_set_trigger_slope = None
    legacy_get_trigger_slope = None
    legacy_set_trigger_coupling = None
    legacy_get_trigger_coupling = None

    def set_trigger_slope(self, slope):
        if not self.is_hds:
            return self.legacy_set_trigger_slope(slope)
        # The scope echoes "RISe"; spell it the way the front panel accepts it.
        token = {"RISE": "RISe", "RISING": "RISe", "FALL": "FALL", "FALLING": "FALL"}.get(
            str(slope).strip().upper(), slope)
        return self.send_command(f"{self.trigger_node('slope')} {token}")

    def get_trigger_slope(self):
        if not self.is_hds:
            return self.legacy_get_trigger_slope()
        reply = self.query(self.trigger_node("slope") + "?")
        return self.match_choice(reply, self.TRIGGER_SLOPES) or reply

    def set_trigger_coupling(self, coupling):
        if not self.is_hds:
            return self.legacy_set_trigger_coupling(coupling)
        return self.send_command(f"{self.trigger_node('coupling')} {coupling}")

    def get_trigger_coupling(self):
        if not self.is_hds:
            return self.legacy_get_trigger_coupling()
        reply = self.query(self.trigger_node("coupling") + "?")
        return self.match_choice(reply, self.TRIGGER_COUPLING) or reply

    def get_trigger_sweep(self):
        """AUTO, NORMal or SINGle: 32 ms, cheaper than reading a whole header."""
        reply = self.query(":TRIGger:SINGle:SWEEp?")
        return self.match_choice(reply, self.TRIGGER_MODES) or reply

    def get_trigger_status(self):
        """What the trigger is doing - RUN, TRIG, STOP or WAIT - or None.

        Documented as :TRIGger:STATus? and answered on the HDS271, so it can be
        asked without capturing a frame; the same word also rides in the capture
        header as RUNSTATUS.
        """
        reply = self.query(":TRIGger:STATus?")
        return str(reply).strip() if reply else None

    # -- instrument shape ---------------------------------------------------

    def get_trigger_level_volts(self):
        """Trigger level in volts for the Y-axis cursor, or None."""
        if not self.is_hds:
            return None
        reply = self.query(self.trigger_node("level") + "?")
        if reply in (None, ""):
            return None
        return self.parse_scale(reply)

    def get_horizontal_position_seconds(self):
        """Horizontal position as reported by the instrument, in seconds."""
        reply = self.query(self._cmd(":HORIzontal:OFFset?", ":TIMebase:HOFFset?"))
        if reply in (None, ""):
            return None
        return self.parse_scale(reply)

    def get_memory_depth(self):
        """Points per trigger sample, as the header reports it ('4K'/'8K').

        This is the definition the flat class ended up with: written second, it
        replaced :meth:`get_memory_depth_choice` above, and it is the one
        ``get_memory_depth`` on the controller is bound to.
        """
        reply = self.query(":ACQuire:DEPMem?")
        return str(reply).strip() or None if reply else None
