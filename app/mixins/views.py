"""The views: time, FFT, math, XY, the cursors and what they measure."""

from modernlab.instrument.capture.waveform import HDS_HORIZONTAL_DIVISIONS
from modernlab.app.io_worker import PRIORITY_REFRESH
from modernlab import analysis
import numpy as np


class ViewsMixin:
    """Plot views for the OWON panel."""

    def clear_plot(self):
        self.ax.clear()
        self.ax.set_facecolor("#101719")
        self.ax.set_xlabel("Time")
        self.ax.set_ylabel("Voltage")
        self.ax.grid(True, alpha=0.25, color="#526060", linestyle=":")
        self.canvas.draw_idle()
        self._plot_cache = []

    def capture_channels(self):
        """The channels of whatever is being shown - a live capture or an open file."""
        return list(getattr(self.scope.waveform, "channels", []) or [])

    def analysis_channel(self, name=None):
        """The channel dict the analysis views work on, by name or by the panel's pick."""
        wanted = str(name or (self.meas_source.get() if hasattr(self, "meas_source") else "CH1")).upper()
        channels = self.capture_channels()
        for channel in channels:
            if str(channel.get("name", "")).upper() == wanted:
                return channel
        return channels[0] if channels else None

    def plot_waveform(self):
        """Draw the capture in whichever view is selected.

        The views are four readings of the same samples, so the framing controls
        are synced once, here, rather than inside each one - they describe the
        capture, not the drawing.
        """
        view = getattr(self, "_view", "time")
        try:
            if view == "fft":
                self.plot_fft()
            elif view == "math":
                self.plot_math()
            elif view == "xy":
                self.plot_xy()
            else:
                self.plot_time()
        finally:
            self.refresh_marker_lines()
            self.update_cursor_readout()
            self._plot_cache = self.capture_channels()
            self.sync_vertical_controls()

    def plot_fft(self):
        """The spectrum of one channel, with the limits it was computed under.

        The frequency axis comes from the capture's own sample interval, so a peak
        lands where it belongs. The Nyquist limit and the bin width are written on
        the plot, because a spectrum drawn from 20 us samples invites reading
        meaning into frequencies it never saw.
        """
        palette = self.palette()
        self.ax.set_facecolor(palette["face"])
        self.ax.grid(True, which="major", color=palette["grid"], alpha=0.35, linestyle=":")
        key = self.analysis_channel()
        options = getattr(self, "_fft_options", {})
        spectrum = analysis.fft_spectrum(key, window=options.get("window", "hanning"),
                                        fmt=options.get("format", "dBV"))
        if not spectrum:
            self.ax.text(0.5, 0.5, "No spectrum\nthis capture carries no sample interval",
                         transform=self.ax.transAxes, ha="center", va="center",
                         color=palette["text"], fontsize=11)
            self.ax.set_xlabel("Frequency", color=palette["text"], fontsize=9)
            self.ax.set_ylabel("", color=palette["text"], fontsize=9)
            self.canvas.draw_idle()
            return
        frequencies, values = analysis.display_spectrum(spectrum)
        self.ax.plot(frequencies, values, color=palette["trace1"], linewidth=1.2)
        if options.get("log"):
            self.ax.set_xscale("log")
            self.ax.set_xlim(max(spectrum["bin_hz"], 1.0), spectrum["nyquist"])
        for peak in analysis.spectrum_peaks(spectrum, count=int(options.get("peaks", 5))):
            self.ax.annotate(analysis.format_hz(peak["frequency"]),
                             xy=(peak["frequency"], peak["value"]),
                             xytext=(4, 4), textcoords="offset points",
                             color=palette["math"], fontsize=8, fontweight="bold")
        self.ax.set_xlabel("Frequency (Hz)", color=palette["text"], fontsize=9)
        self.ax.set_ylabel("Magnitude (%s)" % spectrum["unit"], color=palette["text"], fontsize=9)
        self.ax.set_title("%s \u2022 %s window \u2022 %s \u2022 Nyquist %s \u2022 %.4g Hz/bin"
                          % (str(key.get("name", "CH1")).upper(), spectrum["window"].capitalize(),
                             analysis.format_rate(spectrum["sample_rate"]),
                             analysis.format_hz(spectrum["nyquist"]), spectrum["bin_hz"]),
                          color=palette["text"], fontsize=8, loc="left")
        self.canvas.draw_idle()

    def plot_math(self):
        """One trace computed from two: the vendor's Mathematics panel, in our own frame."""
        palette = self.palette()
        self.ax.set_facecolor(palette["face"])
        self.ax.grid(True, which="major", color=palette["grid"], alpha=0.35, linestyle=":")
        options = getattr(self, "_math_options", {})
        first = self.analysis_channel()
        second = self.analysis_channel(options.get("second", "CH2"))
        op = options.get("op", "subtract")
        self.ax.set_xlabel("Time (s)", color=palette["text"], fontsize=9)
        self.ax.set_ylabel("Voltage (V)", color=palette["text"], fontsize=9)
        if first is None or (op != "invert" and second is None):
            self.ax.text(0.5, 0.5, "Nothing to compute: a channel is missing",
                         transform=self.ax.transAxes, ha="center", va="center",
                         color=palette["text"], fontsize=11)
            self.canvas.draw_idle()
            return
        try:
            computed = analysis.math_trace(analysis.samples(first),
                                           None if op == "invert" else analysis.samples(second), op)
        except ValueError as exc:
            # Two channels that came back with different lengths is a fact about the
            # capture, so it is reported as one rather than padded away.
            self.ax.text(0.5, 0.5, str(exc), transform=self.ax.transAxes, ha="center",
                         va="center", color="#ff9c5b", fontsize=10)
            self.canvas.draw_idle()
            return
        times = analysis.sample_times(first)
        label = analysis.math_label(op, str(first.get("name", "CH1")).upper(),
                                    str(second.get("name", "CH2")).upper())
        self.ax.plot(times[:computed.size], computed, color=palette["math"], linewidth=1.5, label=label)
        self.ax.legend(loc="upper right", fontsize=8, facecolor=palette["face"],
                       labelcolor=palette["text"], framealpha=0.85)
        self.ax.set_title("%s \u2022 %s" % (label, analysis.format_volts(
            float(max(computed) - min(computed)) if computed.size else 0.0) + " pk-pk"),
            color=palette["text"], fontsize=8, loc="left")
        self.canvas.draw_idle()

    def plot_xy(self):
        """CH1 against CH2: the Lissajous view, and the phase between them.

        It needs two channels at once, which is why it says so rather than drawing
        an empty frame when only one is available.
        """
        palette = self.palette()
        self.ax.set_facecolor(palette["face"])
        self.ax.grid(True, which="major", color=palette["grid"], alpha=0.35, linestyle=":")
        first = self.analysis_channel(getattr(self, "_xy_options", {}).get("x", "CH1"))
        second = self.analysis_channel(getattr(self, "_xy_options", {}).get("y", "CH2"))
        self.ax.set_xlabel("%s (V)" % str((first or {}).get("name", "CH1")).upper(),
                           color=palette["text"], fontsize=9)
        self.ax.set_ylabel("%s (V)" % str((second or {}).get("name", "CH2")).upper(),
                           color=palette["text"], fontsize=9)
        if first is None or second is None or first is second:
            self.ax.text(0.5, 0.5, "XY needs two channels: CH1 or CH2 is not available",
                         transform=self.ax.transAxes, ha="center", va="center",
                         color=palette["text"], fontsize=11)
            self.canvas.draw_idle()
            return
        xs, ys = analysis.xy_pairs(analysis.samples(first), analysis.samples(second))
        if xs.size == 0:
            self.ax.text(0.5, 0.5, "No samples", transform=self.ax.transAxes, ha="center",
                         va="center", color=palette["text"], fontsize=11)
            self.canvas.draw_idle()
            return
        self.ax.plot(xs, ys, color=palette["trace2"], linewidth=1.0)
        self.ax.set_title("XY \u2022 %d points" % xs.size, color=palette["text"],
                          fontsize=8, loc="left")
        self.canvas.draw_idle()

    def analysis_traces(self):
        """{(name): (times, volts)} exactly as the plot draws them.

        Cursors must read the trace the way it is shown, so the display ratio is
        applied here, once, and nowhere else.
        """
        traces = {}
        for channel in self.capture_channels():
            values = analysis.samples(channel)
            if values.size == 0:
                continue
            number = self.channel_number(channel)
            if number:
                values = values * self.display_ratio(number, channel)
            times = analysis.sample_times(channel)
            if times.size != values.size:
                times = np.arange(values.size, dtype=float)
            traces[str(channel.get("name", "CH1")).upper()] = (times, values)
        return traces

    def on_cursors_moved(self):
        self.update_cursor_readout()

    def update_cursor_readout(self):
        """Say what the markers measure, including the level of each trace at them."""
        cursors = getattr(self, "_cursors", {}) or {}
        reading = analysis.cursor_readings(self.analysis_traces(),
                                          vertical_times=(cursors.get("t1"), cursors.get("t2")),
                                          horizontal_volts=(cursors.get("v1"), cursors.get("v2")))
        self._cursor_reading = reading
        line = analysis.describe_cursors(reading)
        for name, entry in (reading.get("traces") or {}).items():
            if entry.get("at_t1") is not None and entry.get("at_t2") is not None:
                line += "   %s: %s \u2192 %s" % (name, analysis.format_volts(entry["at_t1"]),
                                                  analysis.format_volts(entry["at_t2"]))
        if hasattr(self, "cursor_text"):
            self.cursor_text.set(line)
        return reading

    def report_peaks(self):
        """Put the spectrum's peaks in the log, with the limits they were found under."""
        key = self.analysis_channel()
        options = getattr(self, "_fft_options", {})
        spectrum = analysis.fft_spectrum(key, window=options.get("window", "hanning"),
                                        fmt=options.get("format", "dBV"))
        self.log(analysis.describe_spectrum(spectrum))
        return spectrum

    def table_data(self):
        """Columns and rows for the data table: index, time, then a column per channel."""
        traces = self.analysis_traces()
        if not traces:
            return [], []
        names = list(traces)
        columns = ["index", "time_s"] + names
        rows = []
        for index in range(max(len(times) for times, _ in traces.values())):
            row = [index]
            row.append("%.9g" % (index * analysis.point_interval(self.analysis_channel())))
            for name in names:
                times, values = traces[name]
                row.append("%.6g" % values[index] if index < values.size else "")
            rows.append(row)
        return columns, rows

    def plot_time(self):
        channels = self.capture_channels()
        face = self.palette()

        palette = {
            "CH1": face["trace1"],
            "CH2": face["trace2"],
            "CH3": "#ff9c5b",
            "CH4": "#c792ea",
        }
        # Every channel shares one timebase, so the unit is chosen from the first
        # trace that has data and applied to all of them.
        factor, unit, span = 1.0, "s", None
        for channel in channels:
            samples = channel.get("waveform") or []
            interval = float(channel.get("point_interval", 0) or 0)
            if samples and interval:
                factor, unit = self.time_axis_units(interval * (len(samples) - 1))
                span = interval * (len(samples) - 1) * factor
                break

        plotted = False
        # The extent of what actually gets drawn, offsets included, so a trace pushed
        # off the grid can be reported rather than silently chopped at the edge.
        drawn_low, drawn_high = None, None
        # (name, colour, where this trace's zero sits, volts per row of the grid),
        # for the 0 V markers drawn with the graticule below.
        references = []
        per_channel_xy = []
        for channel in channels:
            name = str(channel.get("name", "CH1")).upper()
            color = palette.get(name, "#9cdc9c")
            y = np.asarray(channel.get("waveform", []), dtype=float)
            if y.size == 0:
                continue
            # The amplitudes are the instrument's own volts, already at the BNC:
            # nothing is folded in here. Keeping the call makes the one place a
            # convention could change explicit rather than scattered through the
            # drawing code.
            number = self.channel_number(channel)
            if number:
                y = y * self.display_ratio(number, channel)
                # The panel's vertical position, in the same volts the axis is
                # drawn in, so the trace moves and the grid does not.
                y = y + self.display_offset(number)
            x = np.arange(y.size, dtype=float) * float(channel.get("point_interval", 1.0) or 1.0) * factor
            per_channel_xy.append((name, color, x, y))
            plotted = True
            drawn_low = float(np.min(y)) if drawn_low is None else min(drawn_low, float(np.min(y)))
            drawn_high = float(np.max(y)) if drawn_high is None else max(drawn_high, float(np.max(y)))
            references.append((name, color,
                               self.display_offset(number) if number else 0.0,
                               self.display_scale_value(number, channel) if number else None))

        # The vertical frame comes from the instrument's own framing, so the trace
        # is drawn at the size the scope is showing it.
        framing = self.vertical_frame(channels) if plotted else None

        # The graticule is the instrument's screen: 12 columns wide, so the window
        # is 12 columns at the selected time/div. The capture says what the
        # timebase is, which keeps a square exactly one division; without it the
        # samples themselves decide the window.
        timebase = getattr(self.scope.waveform, "timebase_scale", None)
        window = span
        if isinstance(timebase, (int, float)) and timebase:
            window = float(timebase) * HDS_HORIZONTAL_DIVISIONS * factor

        if plotted and framing:
            # Said once per situation rather than once per frame: it names the setting
            # that would frame the signal, which is a fact about the capture, not news.
            note = self.off_scale_report(drawn_low, drawn_high, framing)
            if note != getattr(self, "_off_scale_note", None):
                self._off_scale_note = note
                if note:
                    self.status_var.set(note)
                    self.log(note)

        # --- Fast path: graticule unchanged → only swap the dynamic artists ---
        # The "frame key" captures everything the static frame depends on. When it
        # matches the previous frame we skip ax.clear() and the full graticule
        # redraw, which is the dominant drawing cost at ~1.5 fps.
        channel_names = tuple(name for name, *_ in per_channel_xy)
        # Include display offsets: a position change shifts the trace data,
        # so the fast path must re-draw the lines even though the graticule is
        # the same. Offsets are keyed by channel number from _display_offset.
        display_offsets = tuple(
            (name, round(self.display_offset(self.channel_number(ch)) or 0.0, 12))
            for name, ch in zip(channel_names,
                                [c for c in channels if c.get("waveform")])
        )
        frame_key = (
            framing.get("low") if framing else None,
            framing.get("high") if framing else None,
            framing.get("label") if framing else None,
            window,
            unit,
            channel_names,
            display_offsets,
            face.get("name", ""),
        )
        fast_path = (
            plotted
            and framing is not None
            and getattr(self, "_plot_frame_key", None) == frame_key
        )

        if fast_path:
            # Remove only the dynamic artists added last frame: traces, trigger
            # cursor, zero markers, cursor markers (refresh_marker_lines adds more).
            for artist in getattr(self, "_dynamic_artists", []):
                try:
                    artist.remove()
                except Exception:
                    pass
            self._dynamic_artists = []
            # Re-draw the traces onto the existing axes.
            for _name, color, x, y in per_channel_xy:
                line, = self.ax.plot(x, y, color=color, linewidth=1.4)
                self._dynamic_artists.append(line)
        else:
            # Slow path: full clear + graticule redraw.
            self.ax.clear()
            self.ax.set_facecolor(face["face"])
            self.ax.grid(True, alpha=0.25, color=face["grid"], linestyle=":")
            self.ax.set_ylabel("Voltage (V)", color=face["text"], fontsize=9)
            self.ax.set_xlabel(f"Time ({unit})")
            self._dynamic_artists = []

            if not plotted:
                self.ax.text(0.5, 0.5, "No waveform data", transform=self.ax.transAxes,
                             ha="center", va="center", color="#94a5a5")
            else:
                for _name, color, x, y in per_channel_xy:
                    line, = self.ax.plot(x, y, color=color, linewidth=1.4)
                    self._dynamic_artists.append(line)
                self.show_graticule(framing, window, unit, factor)
            self._plot_frame_key = frame_key

        self.draw_reference_lines(plotted)
        self.draw_zero_markers(references if plotted else [])
        self.canvas.draw_idle()
        self._plot_cache = channels
        self.sync_vertical_controls()

    def refresh_cursors(self):
        """Ask for the two values the plot annotates.

        These used to be USB queries (2 × ~128 ms = 256 ms per frame).
        Both values are now parsed from the capture header by parse_hds_capture
        and stored on scope.waveform, so this is a free read of an already-received
        value rather than a new USB round trip.  The worker call is kept so the
        result still arrives through the same on_instrument_result path.
        """
        self.ask_scope("cursors", self.read_cursor_values, coalesce=True,
                       priority=PRIORITY_REFRESH)

    @staticmethod
    def read_cursor_values(scope):
        """The two readings the plot annotates, from the capture header cache.

        The trigger level and horizontal position are parsed from the header
        by parse_hds_capture() on every frame and stored on scope.waveform.
        Reading them here costs zero USB round trips. If the cached values are
        absent (e.g. first frame before the header is parsed, or a non-HDS
        path), fall back to a live query so the display never goes blank.
        """
        waveform = getattr(scope, "waveform", None)
        level = getattr(waveform, "trigger_level_v", None)
        position = getattr(waveform, "horizontal_position_s", None)
        # Fall back to live queries only when the header cache is empty.
        if level is None:
            try:
                level = scope.get_trigger_level_volts()
            except Exception:
                level = None
        # horizontal position: HOFFSET encoding is unverified on this firmware
        # (raw integer ticks, not seconds), so only use the cached value when
        # it is None (= zero offset, the common case). Don't fall back to the
        # live query which also returns raw ticks in an unknown unit.
        return level, position

    def apply_cursor_readings(self, level, position):
        """Apply a readback. Only real numbers are kept, and each stands alone.

        A scope that answers with something unexpected must not take the plot
        down with it, so anything else leaves the previous cursor alone - and one
        bad value must not discard the other one, which is a reading the
        instrument did give.
        """
        self._trigger_level_v = level if isinstance(level, (int, float)) else self._trigger_level_v
        if isinstance(level, (int, float)):
            # The graph was already following this value; the knob must too. One
            # detent of a knob applies what the box holds, so a box left at its
            # starting 0 sent the instrument to 0 V the moment it was touched -
            # which is what a trigger knob that "does not work" looks like.
            self.sync_entry(getattr(self, "trigger_level", None), "%g" % level)
        if isinstance(position, (int, float)):
            self._horizontal_position = position

    def draw_reference_lines(self, plotted):
        """Trigger level on the Y axis, horizontal position above the X axis.

        The trigger level is a voltage the instrument reports, so it is drawn as
        a cursor across the plot. The horizontal position is only annotated:
        this firmware reports the position, but the captured samples were not
        observed to shift with it, and a line across the trace would imply a
        sample alignment that is not there.
        """
        # Remove artists from the previous call so fast-path frames do not
        # accumulate them.  ax.clear() already removes them on the slow path;
        # the try/except handles that case silently.
        for artist in getattr(self, "_ref_line_artists", []):
            try:
                artist.remove()
            except Exception:
                pass
        self._ref_line_artists = []

        if not plotted:
            return
        level = getattr(self, "_trigger_level_v", None)
        if isinstance(level, (int, float)):
            # A level outside the data's range is exactly when a scope must still
            # show it, so the Y limits are widened rather than the cursor dropped.
            low, high = self.ax.get_ylim()
            if level < low or level > high:
                span = (high - low) or 1.0
                self.ax.set_ylim(min(low, level - 0.05 * span), max(high, level + 0.05 * span))
            line = self.ax.axhline(level, color="#ff7b72", linestyle="--", linewidth=1.0, alpha=0.85)
            annot = self.ax.annotate("TRIG %g V" % level, xy=(0.0, level),
                             xycoords=("axes fraction", "data"),
                             xytext=(4, 0), textcoords="offset points",
                             color="#ff7b72", fontsize=8, va="center", ha="left",
                             fontweight="bold")
            self._ref_line_artists.extend([line, annot])
        position = getattr(self, "_horizontal_position", None)
        if isinstance(position, (int, float)) and position:
            self.ax.set_title("HPOS %s" % self.timebase_position_text(position),
                              color="#7bd6ff", fontsize=8, loc="right", pad=6)

    def draw_zero_markers(self, references):
        """A 0 V marker per enabled channel: the point that trace refers to.

        The waveform refers to this: a trace with no position applied has its zero
        on the middle row of the grid, and a trace that has been moved carries its
        zero with it. This is the only place the applied position is visible, since
        the grid deliberately does not move with it - and it is also what a peak is
        counted from, so a peak-to-peak read off the screen has a reference to
        start at.

        Drawn the way an instrument draws its ground reference: a thin dotted line
        across the graticule in the channel's own colour, labelled at the right
        edge where it does not compete with the trace leaving the frame. Nothing
        here touches the frame - the marker moves, the grid does not.
        """
        self._zero_marks = []
        # Remove artists from the previous call (fast path keeps old artists on
        # the axes; slow path already cleared them, so remove() silently fails).
        for artist in getattr(self, "_zero_mark_artists", []):
            try:
                artist.remove()
            except Exception:
                pass
        self._zero_mark_artists = []
        for name, colour, offset, volts_per_div in references:
            moved = abs(offset) > 1e-12
            line = self.ax.axhline(offset, color=colour, linewidth=0.9, alpha=0.7, zorder=1.6,
                            linestyle=(0, (1.0, 1.6)) if moved else (0, (1.0, 3.4)))
            label = "%s 0V" % name
            if moved:
                label += "  %+.3g V" % offset
                if volts_per_div:
                    label += "  (%+.2f div)" % (offset / volts_per_div)
            annot = self.ax.annotate(label, xy=(1.0, offset),
                             xycoords=("axes fraction", "data"),
                             xytext=(-5, 0), textcoords="offset points",
                             color=colour, fontsize=8, va="center", ha="right",
                             annotation_clip=False)
            self._zero_mark_artists.extend([line, annot])
            self._zero_marks.append({"name": name, "offset": offset, "label": label})
