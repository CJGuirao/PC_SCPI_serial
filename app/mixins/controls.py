"""Front-panel controls: channel, timebase, trigger and acquire writes."""

import tkinter as tk


class ControlsMixin:
    """Front-panel control writes for the OWON panel."""

    def set_channel_display(self, channel, state):
        if self._is_connected():
            self.tell_scope("CH%d display -> %s" % (channel, "ON" if state else "OFF"),
                            lambda scope, c=channel, s=state: scope.set_channel_display(c, s))

    def set_channel_scale(self, channel, scale):
        """Set the volts one row of the grid is worth - the software's display scale.

        Not a write to the instrument, and the measurement is why. With a 25 Vpp
        signal on screen: writing 10 V/div left the code span at 209 codes where a
        real gain change would have halved it to ~97, and writing 2 V/div left it at
        121 where a real change would have saturated it - while the instrument's own
        reading collapsed from 25.6 V to 17.76 V on an input that never moved. The
        write tells the instrument its label changed, not its gain, and it degrades
        what the instrument then reports.

        So the gain is set on the instrument's own keys, and this control is the
        software's display scale: 5 V/div means a row of the grid is 5 V, so a
        25.6 Vpp signal is drawn 5.1 rows tall. The amplitude comes from the capture,
        so it does not move with the control - only the row and the height do.
        """
        if not self._is_connected():
            return
        self._display_scale[channel] = scale
        self._display_scale_is_auto[channel] = False
        self.log("CH%d display scale: %s/div. One row of the grid is now that many "
                 "volts at the input; the amplitude still comes from the capture, so "
                 "it does not change with the control." % (channel, scale))
        self.plot_waveform()

    def set_channel_coupling(self, channel, coupling):
        if self._is_connected():
            self.tell_scope("CH%d coupling -> %s" % (channel, coupling),
                            lambda scope, c=channel, k=coupling: scope.set_channel_coupling(c, k))

    def set_channel_probe(self, channel, probe):
        """Record the probe in use. It does not scale this instrument's amplitudes.

        Worth knowing, and worth not "fixing": with a 1X probe on the input the
        instrument announced 10X and still read the 25 Vpp signal as 25.6 V, so its
        readings are the volts at the BNC whatever the label says. Scaling by the
        label would put every amplitude out by ten - which is one of the ways this
        panel has read the wrong number before.
        """
        if not self._is_connected():
            return
        self._display_probe[channel] = probe
        self.log("CH%d probe recorded as %s. The amplitudes do not change with it: "
                 "this instrument reads real volts at the BNC (a 1X-fed 25 V signal "
                 "read 25.6 V while it announced 10X), and the label does not scale "
                 "them." % (channel, probe))
        self.plot_waveform()

    def sync_entry(self, widget, text):
        """Put a readback into a live control without it turning into a write.

        A live control applies what it holds, so a readback has to land in it or
        the control shows something the instrument never said - and the knob then
        works from a stale value, which from the outside is a dead knob. The write
        is made with live application switched off, because the instrument is the
        one that just said this, and only when the text differs, which is also what
        keeps two of these from bouncing off each other.
        """
        if widget is None or widget.get() == text:
            return False
        self._sync_depth = getattr(self, "_sync_depth", 0) + 1
        try:
            widget.delete(0, tk.END)
            widget.insert(0, text)
        finally:
            self._sync_depth -= 1
        return True

    def set_channel_offset(self, channel, offset):
        """Move this trace up and down in the view, in volts at the input.

        Not a write to the instrument, for the reason the scale control is not one:
        a vertical write is accepted, not acted on, and leaves the instrument's own
        readings out of step with its gain. So the position moves the drawing - a
        row of the grid stays the volts/div it was, and the trace moves up by the
        volts given - while the instrument's own position is set on its keys.

        This used to call a method that does not exist, so every press raised
        inside a Tk callback: the control did nothing on screen and the sync behind
        it never ran, which is what "the panel stops answering" looks like from
        the outside.
        """
        if not self._is_connected():
            return
        try:
            value = float(offset)
        except (TypeError, ValueError):
            self.log("CH%d position: %r is not a number; the trace was left where it "
                     "is." % (channel, offset), "WARNING")
            return
        self._display_offset[channel] = value
        self.log("CH%d position: the trace moved %+.3g V; the grid did not. The "
                 "instrument's own vertical position is set on its front panel."
                 % (channel, value))
        self.plot_waveform()

    def set_timebase(self):
        if self._is_connected():
            wanted = self.timebase_scale.get()
            self.tell_scope("Timebase -> %s" % wanted,
                            lambda scope, value=wanted: scope.set_timebase_scale(value))

    def set_timebase_offset(self):
        if self._is_connected():
            self.scope.set_timebase_offset(self.timebase_offset.get())
            self.log(f"Timebase offset -> {self.timebase_offset.get()}")

    @staticmethod
    def timebase_position_text(seconds):
        """Seconds spelled the way the instrument accepts a time: '1.5ms', '-500us'."""
        if not seconds:
            return "0"
        magnitude = abs(seconds)
        for factor, suffix in ((1e-3, "ms"), (1e-6, "us"), (1e-9, "ns")):
            if magnitude >= factor:
                return "%g%s" % (seconds / factor, suffix)
        return "%gs" % seconds

    def set_timebase_position(self):
        """Apply the horizontal position, which the knob enters in divisions.

        Divisions are what a scope front panel uses, and they avoid the entry box
        showing a raw second figure like 0.0005. The conversion needs the current
        time/div, so the position is recomputed whenever the timebase changes.
        """
        if not self._is_connected():
            return
        try:
            divisions = float(self.timebase_offset.get())
        except ValueError:
            self.status_var.set("Horizontal position must be a number of divisions")
            return
        scale = self.scope.parse_scale(self.scope.get_timebase_scale() or "")
        if not scale:
            return
        seconds = divisions * scale
        self._horizontal_position = seconds
        text = self.timebase_position_text(seconds)
        self.tell_scope("Horizontal position -> %g div (%s)" % (divisions, text),
                        lambda scope, value=text: scope.set_timebase_offset(value))
        if self.scope.waveform.channels:
            self.plot_waveform()

    def set_trigger_mode(self):
        if self._is_connected():
            wanted = self.trigger_mode.get()
            self.tell_scope("Trigger mode -> %s" % wanted,
                            lambda scope, value=wanted: scope.set_trigger_mode(value))

    def set_trigger_source(self):
        if self._is_connected():
            wanted = self.trigger_source.get()
            self.tell_scope("Trigger source -> %s" % wanted,
                            lambda scope, value=wanted: getattr(
                                scope, "set_edge_trigger_source", scope.set_trigger_source)(value))

    def set_trigger_slope(self):
        if self._is_connected():
            wanted = self.trigger_slope.get()
            self.tell_scope("Trigger slope -> %s" % wanted,
                            lambda scope, value=wanted: getattr(
                                scope, "set_edge_trigger_slope", scope.set_trigger_slope)(value))

    def set_trigger_coupling(self):
        """The filter in front of the trigger: DC, AC, or a noise-rejecting HF/LF.

        The readback writes this control too, and this family's trigger coupling
        is its own list - DC, AC, HF, LF - not the channel coupling list it sits
        beside on the front panel.
        """
        if self._is_connected():
            wanted = self.trigger_coupling.get()
            self.tell_scope("Trigger coupling -> %s" % wanted,
                            lambda scope, value=wanted: getattr(
                                scope, "set_edge_trigger_coupling",
                                scope.set_trigger_coupling)(value))

    def set_trigger_level(self):
        if self._is_connected():
            value = self.trigger_level.get()
            try:
                numeric = float(value)
                value = int(numeric) if numeric.is_integer() else numeric
                self._trigger_level_v = float(numeric)
            except Exception:
                pass
            self.tell_scope("Trigger level -> %s" % self.trigger_level.get(),
                            lambda scope, value=value: getattr(
                                scope, "set_edge_trigger_level", scope.set_trigger_level)(value))
            if self.scope.waveform.channels:
                self.plot_waveform()

    def set_acquire_type(self):
        if self._is_connected():
            wanted = self.acq_type.get()
            self.tell_scope("Acquisition -> %s" % wanted,
                            lambda scope, value=wanted: scope.set_acquire_type(value))

    def set_acquire_average(self):
        if self._is_connected():
            wanted = self.acq_average.get()
            self.tell_scope("Average count -> %s" % wanted,
                            lambda scope, value=wanted: scope.set_acquire_average(value))
            self.log(f"Average count -> {self.acq_average.get()}")

    def set_memory_depth(self):
        if self._is_connected():
            wanted = self.mem_depth.get()
            self.tell_scope("Memory depth -> %s" % wanted,
                            lambda scope, value=wanted: scope.set_memory_depth(value))
            self.log(f"Memory depth -> {self.mem_depth.get()}")
