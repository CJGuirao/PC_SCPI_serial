"""Framing: letting a setting land, and what the instrument's framing is now.

A write that moves the frame is not in the next capture yet -- measured on an
HDS271 with a known 5.00 Vpp signal, a capture started 50 ms after a volts/div
write still returned the PREVIOUS setting's frame, and 150 ms was clean -- so
captures wait it out (:class:`FramingSettle`), and the capture header, which
carries the framing the decode is scaled by, is dropped on every write and
watched for a front-panel change (:class:`FramingWatch`).

The ladders are here too: the choices a value is snapped to, compared by ratio
rather than by difference, because 5ns sits as close to 10ns as 10s does to 20s.

Both mixins are written to sit on the controller and use only ``self.query`` /
``self.send_command`` / the family's own accessors, so neither knows which
dialect it is on.
"""

import logging
import math
import time

#: The timebase ladder for the SDS / legacy family, kept beside the scales it is
#: read with (:class:`modernlab.instrument.dialect.sds.SdsDialect`).
TIMEBASE_SCALES = ["5ns", "10ns", "20ns", "50ns", "100ns", "200ns", "500ns",
                   "1us", "2us", "5us", "10us", "20us", "50us", "100us",
                   "200us", "500us", "1ms", "2ms", "5ms", "10ms", "20ms",
                   "50ms", "100ms", "200ms", "500ms", "1s", "2s", "5s",
                   "10s", "20s", "50s", "100s"]

#: The volts/div ladder for the SDS / legacy family.
VOLTAGE_SCALES = ["2mv", "5mv", "10mv", "20mv", "50mv", "100mv",
                  "200mv", "500mv", "1v", "2v", "5v", "10v"]


class FramingSettle:
    """The pause between a framing write and the capture it must appear in.

    ``_framing_settle_until`` is created by the controller's ``__init__``, not
    here: it is per-instance state and this mixin is deliberately not its own
    object.  See :class:`~modernlab.instrument.controller.OWONScopeController`.
    """


    #: Seconds to let a framing write take effect before capturing. See above.
    FRAMING_SETTLE_SECONDS = 0.25

    def mark_framing_settle(self):
        """Note that the instrument's screen is about to change.

        Called by the writes that move it (volts/div, probe, offset, timebase).
        Without this a capture can redraw the old frame right after a change,
        which looks exactly like the control not working.
        """
        self._framing_settle_until = time.monotonic() + self.FRAMING_SETTLE_SECONDS

    def wait_for_framing_settle(self):
        """Sleep out whatever is left of a pending framing change."""
        remaining = self._framing_settle_until - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)


class FramingWatch:
    """What the instrument's framing is right now, watched on a schedule."""


    def invalidate_capture_header(self):
        """Drop the cached capture header, so the next capture re-reads the framing.

        The header carries the volts/div, probe and timebase the instrument is
        using. Any change of those - a write here, or a menu setting on the
        instrument - must not be decoded against the old copy.
        """
        self._cached_header = None

    #: The framing as three cheap reads, watched on a schedule.
    #:
    #: The capture header carries copies of these values but costs 479 bytes
    #: (~254 ms) against ~32 ms each, so the live loop watches these and only
    #: re-reads the header when one of them moves. Without that watch, a
    #: volts/div set from the instrument's own menu reaches the plot only on the
    #: header schedule - which is ~8 s at the live rate, and reads exactly like
    #: "the volts/div control is not working" while the time base, whose label the
    #: plot follows every frame, looks fine.
    #:
    #: They are read through the panel's own accessors, which apply the family's
    #: dialect and match the reply to the ladder: the raw ':CHANnel1:SCALe?' nodes
    #: answer nothing at all on an HDS instrument and would have made every poll
    #: look like a change.
    FRAMING_NAMES = ("scale", "probe", "timebase")

    def framing_signature(self):
        """What the instrument says its framing is right now, as a tuple.

        Returns None for any value that could not be read, so a dropped reply is
        reported as a difference and triggers a header re-read rather than being
        mistaken for "nothing changed".
        """
        readers = ((self.get_channel_scale, 1), (self.get_channel_probe, 1),
                   (self.get_timebase_scale, None))
        values = []
        for name, (reader, argument) in zip(self.FRAMING_NAMES, readers):
            try:
                reply = reader(argument) if argument is not None else reader()
                values.append(str(reply or "").strip())
            except Exception as exc:                              # noqa: BLE001
                logging.warning("Framing poll failed on %s: %s", name, exc)
                values.append(None)
        return tuple(values)


    def nearest_timebase(self, seconds_per_div):

        """The available time/div closest to a target, compared by ratio."""
        if not seconds_per_div or seconds_per_div <= 0:
            return None
        best = None
        for choice in self.TIMEBASE_SCALES:
            value = self.parse_scale(choice)
            if not value or value <= 0:
                continue
            if best is None or abs(math.log(value / seconds_per_div)) < abs(math.log(best[0] / seconds_per_div)):
                best = (value, choice)
        return best[1] if best else None


    def nearest_voltage_scale(self, volts_per_div):

        """The available volts/div closest to a target, compared by ratio."""
        if not volts_per_div or volts_per_div <= 0:
            return None
        best = None
        for choice in self.VOLTAGE_SCALES:
            value = self.parse_scale(choice)
            if not value or value <= 0:
                continue
            if best is None or abs(math.log(value / volts_per_div)) < abs(math.log(best[0] / volts_per_div)):
                best = (value, choice)
        return best[1] if best else None
