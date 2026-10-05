"""Trace framing: what the grid shows and where the trace sits in it."""

from modernlab.instrument.capture.waveform import HDS_HORIZONTAL_DIVISIONS
from modernlab.instrument.capture.waveform import HDS_VERTICAL_DIVISIONS
from modernlab.instrument.controller import OWONScopeController
from modernlab.instrument.capture.waveform import WaveformData
import numpy as np


class FramingMixin:
    """Trace framing for the OWON panel."""

    @staticmethod
    def time_axis_units(span_seconds):
        """The unit that keeps time-axis numbers readable, with its factor.

        The captures come back in seconds (20us per point at 500us/div), so a
        bare "Time" axis reads as 0.000..0.006 with no unit at all.
        """
        if span_seconds and span_seconds < 1e-3:
            return 1e6, "us"
        if span_seconds and span_seconds < 1.0:
            return 1e3, "ms"
        return 1.0, "s"

    def choice_values(self, widget):
        """The choices a selector widget offers, whatever Tk hands back."""
        if widget is None:
            return []
        values = widget.cget("values")
        if isinstance(values, str):
            values = values.replace("{", "").replace("}", "").split()
        return list(values)

    def nearest_choice(self, value, choices):
        """The choice-list entry closest to a value.

        A reported figure round-trips through the probe factor, so it lands
        beside the control's own units rather than exactly on them ('500mv', not
        '0.4999997').
        """
        best, best_gap = None, None
        for choice in choices:
            parsed = OWONScopeController.parse_scale(choice)
            if parsed is None:
                continue
            gap = abs(parsed - value)
            if best_gap is None or gap < best_gap:
                best, best_gap = choice, gap
        return best

    def framing_choice(self, value, choices):
        """The finest offered setting that still shows a capture decoded at ``value``.

        The capture's own scale comes from the instrument's readings and its codes:
        they span the eight rows of the screen, so one row is worth exactly the scale
        the decode came out at, and the trace fills the grid at that setting. Snapping
        that to the NEAREST offered setting can go DOWN, and a row finer than the
        capture is worth means the trace no longer fits the frame - it runs past the
        top of the grid, every fast edge is drawn chopped at the edge, and the flyback
        of a sawtooth reads as a spike standing over the ramp instead of a clean edge.

        So the setting has to be at least as coarse as the decode. This is the same
        arithmetic the user checks a reading with: a 25 Vpp signal needs 5 V/div to
        make 5 divisions of an eight-row grid.
        """
        best, best_value = None, None
        for choice in choices:
            parsed = OWONScopeController.parse_scale(choice)
            if parsed is None or parsed < value * (1.0 - 1e-9):
                continue
            if best_value is None or parsed < best_value:
                best, best_value = choice, parsed
        if best is None:
            # Nothing offered is coarse enough to hold the signal. Name the coarsest
            # there is, so the control and the axis still agree, and let the caller
            # say the trace will not fit at any setting rather than silently chop it.
            return self.nearest_choice(value, choices)
        return best

    def off_scale_report(self, drawn_low, drawn_high, framing):
        """What to say when the trace does not fit the grid, or None when it does.

        A trace drawn past the frame is chopped at the edge, which is indistinguishable
        from a signal that really does peak at the top row - so this names the setting
        that WOULD hold it. The instrument's own screen has the same eight rows, so a
        setting that frames it here frames it there too.
        """
        if not framing or drawn_low is None:
            return None
        if drawn_low >= framing.get("low") and drawn_high <= framing.get("high"):
            return None
        needed = max(abs(drawn_low), abs(drawn_high)) / (HDS_VERTICAL_DIVISIONS / 2.0)
        widget = getattr(self, "ch1_scale", None)
        choices = self.choice_values(widget) if widget is not None else []
        wanted = self.framing_choice(needed, choices) if choices else None
        shown = WaveformData._scale_to_float(wanted) if wanted else None
        return ("trace is off the grid at this setting (%+.1f..%+.1f V over %+.1f..%+.1f V)"
                "%s" % (drawn_low, drawn_high, framing.get("low", 0.0), framing.get("high", 0.0),
                        " - %s would frame it" % wanted if shown else ""))

    def sync_vertical_controls(self):
        """Follow a framing change made on the instrument.

        The capture carries the probe the instrument is using and the scale it was
        decoded at, so a change made on the instrument reaches these controls
        without another query. The volts/div control shows the DECODED scale, not
        the volts/div the instrument reports, and it only moves while the user has
        not pinned a value of their own on it.
        """
        for channel in getattr(self.scope.waveform, "channels", []) or []:
            name = str(channel.get("name", "")).upper()
            if not name.startswith("CH") or not name[2:].isdigit():
                continue
            number = int(name[2:])
            # The panel's volts/div is the software's display scale. Left on Auto it
            # follows the REPORTED volts/div from the instrument header (the same number
            # the hardware's own graticule uses), never true_volts_per_div (the decoded
            # amplitude scale), which is the signal's span over the code range and
            # diverges sharply for a small signal on a coarse scale.
            reported = channel.get("volts_per_div")
            decoded = channel.get("true_volts_per_div")
            scale_widget = getattr(self, "ch%d_scale" % number, None)
            if scale_widget is not None and self._display_scale_is_auto.get(number, True):
                scale_source = reported or decoded
                if scale_source:
                    choices = self.choice_values(scale_widget) or list(OWONScopeController.VOLTAGE_SCALES)
                    scale = self.nearest_choice(scale_source, choices)
                    if scale and scale_widget.get() != scale:
                        scale_widget.set(scale)
                        self.log("CH%d display scale auto: %s/div (instrument reports "
                                 "%.4g V/div)" % (number, scale, scale_source))
            probe_widget = getattr(self, "ch%d_probe" % number, None)
            if self._display_probe.get(number) is None and probe_widget is not None:
                # The instrument ANNOUNCES 10X on this bench while a 1X probe feeds it,
                # and reads the input as real volts anyway, so mirroring its label would
                # put a misleading number in the panel. X1 states the input as it is.
                for candidate in self.choice_values(probe_widget):
                    if WaveformData._probe_factor(candidate) == 1.0:
                        if probe_widget.get() != candidate:
                            probe_widget.set(candidate)
                            self.log("CH%d probe shown as %s - the instrument announces %s, "
                                     "but it reads real volts at the BNC either way, so the "
                                     "amplitudes do not depend on it"
                                     % (number, candidate, channel.get("attenuation") or "?"))
                        break
            self.follow_instrument_position(number, channel)

    def follow_instrument_position(self, number, channel):
        """Move this view when the instrument moves its own trace.

        The capture carries the instrument's vertical position in divisions of its
        volts/div, so a turn of the front-panel knob reaches the panel the same way
        a volts/div change reaches its own control. It is applied to the drawing as
        well, because the two screens are meant to agree - and only when the value
        CHANGES, so a position set here is not wiped by the readback that arrives
        with every frame.

        The instrument's position is shown in this view's own volts per division, so
        a row here means the same thing as a row there.

        NOTE: On HDS271 V1.3.0 the header OFFSET field is unreliable - it reported
        -55 divisions with no offset applied on the hardware screen (which would put
        the trace completely off screen at 500 mV/div, yet the hardware showed it
        correctly). The same firmware pattern as the volts/div SCALE label: the field
        is cosmetic and does not reflect the acquisition state. We therefore only
        apply the header's offset when it is within the physical screen range
        (+-HDS_VERTICAL_DIVISIONS/2 = +-4 divisions). Values outside that range are
        silently ignored - the user's own position control is still fully functional.
        """
        divisions = channel.get("vertical_offset_div")
        per_division = self.display_scale_value(number, channel)
        if not isinstance(divisions, (int, float)) or not per_division:
            return
        # Guard against the firmware's stale/garbage OFFSET values: the physical
        # screen is 8 rows, so a real position fits within +-8 divisions (being
        # generous). Anything beyond +-16 is certainly wrong and is ignored.
        if abs(divisions) > 16:
            return
        instrument = divisions * per_division
        if instrument == self._instrument_position.get(number):
            return
        self._instrument_position[number] = instrument
        self._display_offset[number] = instrument
        if self.sync_entry(getattr(self, "ch%d_offset" % number, None), "%g" % instrument):
            self.log("CH%d position %+.3g V: the instrument moved its own trace, and this "
                     "view follows it. %+.2f division%s of its screen."
                     % (number, instrument, divisions, "" if abs(divisions) == 1 else "s"))

    def framing_changed(self):
        """A framing change was seen on the instrument: follow it here.

        The panel's volts/div and probe controls mirror the instrument, so they
        must show what it now reports - otherwise the control that "does nothing"
        is this one, while the time base, drawn from the instrument's own label
        every frame, appears to work.
        """
        self.apply_framing_signature(getattr(self, "_framing_signature", None))
        self.sync_vertical_controls()
        self.refresh_after_framing_change()

    def apply_framing_signature(self, signature):
        """Move the mirror controls to the framing the instrument just reported.

        The capture that will carry the same values arrives up to a frame later,
        so the controls are moved from the cheap reads instead of waiting for it -
        a control that stays put for a second after the instrument has changed
        reads as a control that does not work.
        """
        values = {name: value for name, value
                  in zip(self.scope.FRAMING_NAMES, signature or [])}
        scale_reply = values.get("scale")
        scale_widget = getattr(self, "ch1_scale", None)
        if scale_reply and scale_widget is not None:
            choices = self.choice_values(scale_widget) or list(OWONScopeController.VOLTAGE_SCALES)
            if scale_reply not in choices:
                scale_reply = self.nearest_choice(
                    WaveformData._scale_to_float(scale_reply) or 0.0, choices)
            if scale_reply and scale_widget.get() != scale_reply:
                scale_widget.set(scale_reply)

        probe_reply = WaveformData._probe_factor(values.get("probe"))
        probe_widget = getattr(self, "ch1_probe", None)
        if probe_reply and probe_widget is not None:
            for candidate in self.choice_values(probe_widget):
                if WaveformData._probe_factor(candidate) == probe_reply:
                    if probe_widget.get() != candidate:
                        probe_widget.set(candidate)
                    break

    def refresh_after_framing_change(self):
        """Recapture so the display shows the framing the instrument now has.

        A volts/div, probe or position change moves the instrument's own screen at
        once, so the software must not keep drawing the old frame - and when the
        app is not live there would be no next capture to correct it.
        """
        # The live loop re-captures on its own, and the write dropped the cached
        # header, so its next frame picks the new framing up. With refresh paused
        # there is no next frame, so ask for one.
        if self.auto_refresh_var.get():
            return
        if getattr(self.scope.waveform, "channels", None):
            self.download_waveform()

    @staticmethod
    def channel_number(channel):
        """The channel number a capture entry belongs to, or None."""
        name = str(channel.get("name", "")).upper()
        return int(name[2:]) if name[:2] == "CH" and name[2:].isdigit() else None

    def captured_channel(self, number):
        """The capture entry for a channel number, or an empty one."""
        for entry in getattr(self.scope.waveform, "channels", []) or []:
            if self.channel_number(entry) == number:
                return entry
        return {}

    def display_probe_value(self, channel, entry=None):
        """The probe factor the trace is SHOWN in: the user's choice, else the instrument's.

        The capture carries the instrument's probe, so the live query is only the
        fallback - one fewer round trip on a path that runs on every redraw.
        """
        chosen = self._display_probe.get(channel)
        if chosen is not None:
            return WaveformData._probe_factor(chosen) or 1.0
        entry = entry if entry is not None else self.captured_channel(channel)
        instrument = WaveformData._probe_factor(entry.get("attenuation"))
        if instrument:
            return instrument
        return WaveformData._probe_factor(self.scope.get_channel_probe(channel)) or 1.0

    def display_offset(self, number):
        """This channel's vertical position in the view, in volts at the input.

        The panel's control, not the instrument's: it moves the drawing and leaves
        the volts per code and the grid alone, so a trace can be moved off a cursor
        without changing a single amplitude reading.
        """
        return (getattr(self, "_display_offset", {}) or {}).get(number, 0.0)

    def display_ratio(self, number, entry):
        """Displayed volts per volt read: always 1.0 on this instrument.

        The probe label does NOT scale its readings. Measured with a 1X probe on the
        input: the instrument announced 10X and read the 25 Vpp signal as 25.6 V, so
        its volts are the real volts at the BNC, and multiplying by the announced
        probe would have put every amplitude out by ten. This is here so the probe
        control has exactly one place to change its mind, and nothing else in the
        drawing code has to know.
        """
        return 1.0

    def display_scale_value(self, number, entry):
        """The volts one row of the grid is worth - straight, one division of it.

        The panel's control means exactly what it says: 5 V/div means the row is
        5 V, so a 25.6 Vpp signal is 5.1 divisions of the grid, at 10 V/div it is
        2.6, at 1 V/div it is 25.6. Nothing else is folded in - not the probe label,
        which does not scale this instrument's readings (a 1X-fed 25 V signal reads
        25.6 V while it announces 10X).

        On Auto the grid follows the REPORTED volts/div (what the front panel says
        and what the instrument honours for display framing), so the software's grid
        matches the hardware grid division for division. ``true_volts_per_div`` (the
        calibrated amplitude scale) stays for the amplitude decode and is shown in
        the axis label when it disagrees, but it is not the grid scale: for a 2.5 Vpp
        signal at 500 mV/div the signal spans ~16 of the 256 codes, giving a decoded
        amplitude scale of ~5 V/div - ten times the actual setting - which would shrink
        the trace to a sliver while the hardware shows it 5 divisions tall.
        """
        chosen = self._display_scale.get(number)
        if chosen:
            value = WaveformData._scale_to_float(chosen)
            if value:
                return value
        # Auto: reported volts/div first (the hardware's framing setting), then the
        # decoded amplitude scale as a fallback for the case where no header scale
        # arrived yet (first frame before the header is complete).
        reported = entry.get("volts_per_div")
        if reported:
            return float(reported)
        decoded = entry.get("true_volts_per_div")
        if decoded:
            widget = getattr(self, "ch%d_scale" % number, None)
            choices = self.choice_values(widget) if widget is not None else None
            if choices:
                # Frame it, do not merely round it: the nearest setting can be finer
                # than the capture, and then the trace is drawn taller than the grid.
                snapped = self.framing_choice(decoded, choices)
                value = WaveformData._scale_to_float(snapped) if snapped else None
                if value:
                    return value
            return decoded
        return None

    @staticmethod
    def frame_centre(entry, step=None):
        """The volts at the middle of the instrument's screen.

        The sample codes ARE the instrument's rows and the decode places the code
        midpoint at the middle of that screen, so the middle of a capture's own range
        is the middle of its screen. Centring the grid on 0 V instead only works while
        the signal straddles zero: a unipolar capture, whose instrument MIN reads
        0.0000e+00, then sits entirely in the top half of the frame and everything
        above the middle row is chopped off at the edge. That is what drew a
        sawtooth's flyback as a spike standing over the ramp while the instrument's own
        screen showed the same signal framed and clean.

        Quantised to an eighth of a row, so the extremes' own frame-to-frame jitter
        cannot make the grid drift while acquisition runs.
        """
        samples = [v for v in (entry.get("waveform") or [])
                   if isinstance(v, (int, float))]
        if not samples:
            return 0.0
        centre = (min(samples) + max(samples)) / 2.0
        if step:
            centre = round(centre / (step / 8.0)) * (step / 8.0)
        return centre

    def vertical_frame(self, channels):
        """The volts the trace is drawn against, in the software's own display scale.

        A row of the grid is the volts/div the panel's control is set to - or, on
        Auto, the scale the capture was decoded at, which comes from the
        instrument's own readings rather than from the volts/div it reports. The
        amplitude is decoded from the capture, so it does not move with the control:
        only the height of the trace and the value of a row do, and rows x volts-per-row
        comes back to the same signal either way.

        The frame does not follow the panel's position control: that control moves
        the trace across this frame, the way a position knob moves a trace across a
        graticule that is fixed to the screen.

        Returns {"low", "high", "step", "label"} or None when the capture carries
        no usable scale (raw codes, or a failed scale query), in which case the
        caller falls back to autoscaling.
        """
        spans, labels, steps = [], [], []
        for channel in channels:
            number = self.channel_number(channel)
            volts_per_div = self.display_scale_value(number, channel) if number else None
            samples = channel.get("waveform") or []
            if not volts_per_div or not samples or channel.get("units") != "V":
                continue
            half = (HDS_VERTICAL_DIVISIONS / 2.0) * volts_per_div
            # The window is the instrument's own screen, centred on the middle of the
            # capture rather than on 0 V: see frame_centre.
            middle = self.frame_centre(channel, volts_per_div)
            # The frame is the instrument's screen, so the position control does
            # NOT move it: the trace moves across a grid that stays where it is,
            # which is what makes a row on screen mean the volts/div it reads.
            # Adding the offset here moved the window with the trace, so the two
            # cancelled and the grid slid while the trace held still - a visual
            # reading taken against a sliding grid is worth nothing.
            spans.append((middle - half, middle + half))
            note = ""
            decoded = channel.get("true_volts_per_div")
            probe_in_use = self._display_probe.get(number)
            if probe_in_use:
                note += " %s" % probe_in_use
            # Note the decoded amplitude scale when it differs meaningfully from
            # the framing scale (e.g. a small signal on a coarse range) so the
            # user knows the trace amplitude is decoded independently of the grid.
            if decoded and abs(decoded - volts_per_div) > 0.25 * volts_per_div:
                note += " (decoded %.4g V/div)" % decoded
            labels.append("%s %.4g V/div%s" % (
                str(channel.get("name", "")).upper(), volts_per_div, note))
            steps.append(volts_per_div)
        if not spans:
            return None
        return {
            "low": min(low for low, _ in spans),
            "high": max(high for _, high in spans),
            "step": min(steps),
            "label": ", ".join(labels),
        }

    def show_vertical_frame(self, framing):
        """Draw the instrument's vertical frame, gridded in its divisions."""
        self.ax.set_ylim(framing["low"], framing["high"])
        step = framing.get("step")
        if step and step > 0:
            # Ticks where the instrument draws its divisions, so the axis reads in
            # the same units as the front panel. A frame too tall for that many
            # ticks keeps the automatic ones rather than a wall of labels.
            first = int(np.ceil(framing["low"] / step))
            last = int(np.floor(framing["high"] / step))
            ticks = [k * step for k in range(first, last + 1)]
            if 1 < len(ticks) <= 13:
                self.ax.set_yticks(ticks)
        self.ax.set_ylabel("Voltage (V) — " + framing["label"])

    def show_graticule(self, framing, window, unit, factor=1.0):
        """Draw the instrument's screen: 12 columns by 8 rows of real divisions.

        A square has to mean the same thing here as on the instrument, so the
        column width is the selected time/div and the row height the selected
        volts/div (at the probe tip, as the instrument labels it). The centre
        cross of the graticule is dotted a little more visibly than the rest, the
        way the instrument draws it.
        """
        if framing:
            self.show_vertical_frame(framing)
            low, high = framing["low"], framing["high"]
        else:
            # No usable scale (raw codes, or a failed scale query): show the data
            # rather than inventing a frame for it.
            self.ax.relim()
            self.ax.autoscale_view()
            low, high = self.ax.get_ylim()

        if window and window > 0:
            stride = window / HDS_HORIZONTAL_DIVISIONS
            self.ax.set_xlim(0.0, window)
            self.ax.set_xticks([k * stride for k in range(int(HDS_HORIZONTAL_DIVISIONS) + 1)])
            # The division is named in its own unit - "200 us/div", not the
            # "0.2 ms/div" the whole span's unit would give.
            stride_factor, stride_unit = self.time_axis_units(stride / (factor or 1.0))
            self.ax.set_xlabel("Time (%s) — %.4g %s/div"
                               % (unit, stride / (factor or 1.0) * stride_factor, stride_unit))

        self.ax.grid(True, which="major", linestyle=":", linewidth=0.7,
                     color="#46585a", alpha=0.9)
        # zorder keeps the cross under the trace, where the graticule belongs.
        if window and window > 0:
            self.ax.axvline(window / 2.0, linestyle=":", linewidth=1.0, color="#8ea3a4",
                            alpha=0.95, zorder=0.5)
        self.ax.axhline((low + high) / 2.0, linestyle=":", linewidth=1.0, color="#8ea3a4",
                        alpha=0.95, zorder=0.5)
