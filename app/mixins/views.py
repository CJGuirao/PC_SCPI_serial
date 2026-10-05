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
        self.ax.clear()
        # Switching away from time view must force a full graticule rebuild
        # when time view is next drawn (the fast path would reuse stale artists).
        self._plot_frame_key = None
        self._dynamic_artists = []
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

        # Harmonic analysis + THD (when enabled in ANALYSE options).
        if options.get("thd"):
            ha = analysis.harmonic_analysis(spectrum)
            if ha:
                f0 = ha["fundamental"]["frequency"]
                # Mark fundamental with a triangle.
                self.ax.annotate(
                    "F  %s" % analysis.format_hz(f0),
                    xy=(f0, ha["fundamental"]["value"]),
                    xytext=(6, 8), textcoords="offset points",
                    color="#ffd166", fontsize=8, fontweight="bold",
                    arrowprops=dict(arrowstyle="-", color="#ffd166", lw=0.8))
                # Mark each found harmonic.
                for h in ha["harmonics"]:
                    if not h["found"]:
                        continue
                    self.ax.annotate(
                        "%dH" % h["n"],
                        xy=(h["frequency"], h["value"]),
                        xytext=(4, 4), textcoords="offset points",
                        color="#ff7b72", fontsize=7, fontweight="bold")
                # THD box in the upper-right corner.
                thd_text = "THD = %.2f %%  (%.1f dB)" % (ha["thd_pct"], ha["thd_db"])
                self.ax.text(0.98, 0.96, thd_text,
                             transform=self.ax.transAxes,
                             ha="right", va="top",
                             fontsize=9, fontweight="bold",
                             color="#ffd166",
                             bbox=dict(boxstyle="round,pad=0.3",
                                       facecolor="#1a2020", edgecolor="#ffd166",
                                       alpha=0.85))
        self.ax.set_xlabel("Frequency (Hz)", color=palette["text"], fontsize=9)
        self.ax.set_ylabel("Magnitude (%s)" % spectrum["unit"], color=palette["text"], fontsize=9)
        self.ax.set_title("%s \u2022 %s window \u2022 %s \u2022 Nyquist %s \u2022 %.4g Hz/bin"
                          % (str(key.get("name", "CH1")).upper(), spectrum["window"].capitalize(),
                             analysis.format_rate(spectrum["sample_rate"]),
                             analysis.format_hz(spectrum["nyquist"]), spectrum["bin_hz"]),
                          color=palette["text"], fontsize=8, loc="left")
        # Do NOT call canvas.draw_idle() here — plot_waveform's finally block
        # adds cursor artists after this returns, and calls draw_idle() itself.
        # Calling it here causes a double-render with autoscale re-enabled between.

    def plot_math(self):
        """One trace computed from two: the vendor's Mathematics panel, in our own frame."""
        self.ax.clear()
        self._plot_frame_key = None
        self._dynamic_artists = []
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
            return
        try:
            dt = analysis.point_interval(first)
            sec_samples = None if op in ("invert", "abs", "integrate",
                                         "differentiate", "square") else analysis.samples(second)
            computed = analysis.math_trace_extended(
                analysis.samples(first), op, sec_samples, dt=dt)
        except ValueError as exc:
            # Two channels that came back with different lengths is a fact about the
            # capture, so it is reported as one rather than padded away.
            self.ax.text(0.5, 0.5, str(exc), transform=self.ax.transAxes, ha="center",
                         va="center", color="#ff9c5b", fontsize=10)
            return
        times = analysis.sample_times(first)
        label = analysis.math_label_extended(op, str(first.get("name", "CH1")).upper(),
                                    str((second or {}).get("name", "CH2")).upper())
        self.ax.plot(times[:computed.size], computed, color=palette["math"], linewidth=1.5, label=label)
        self.ax.legend(loc="upper right", fontsize=8, facecolor=palette["face"],
                       labelcolor=palette["text"], framealpha=0.85)
        self.ax.set_title("%s \u2022 %s" % (label, analysis.format_volts(
            float(max(computed) - min(computed)) if computed.size else 0.0) + " pk-pk"),
            color=palette["text"], fontsize=8, loc="left")

    def plot_xy(self):
        """CH1 against CH2: the Lissajous view, and the phase between them.

        It needs two channels at once, which is why it says so rather than drawing
        an empty frame when only one is available.
        """
        self.ax.clear()
        self._plot_frame_key = None
        self._dynamic_artists = []
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
            return
        xs, ys = analysis.xy_pairs(analysis.samples(first), analysis.samples(second))
        if xs.size == 0:
            self.ax.text(0.5, 0.5, "No samples", transform=self.ax.transAxes, ha="center",
                         va="center", color=palette["text"], fontsize=11)
            return
        self.ax.plot(xs, ys, color=palette["trace2"], linewidth=1.0)
        self.ax.set_title("XY \u2022 %d points" % xs.size, color=palette["text"],
                          fontsize=8, loc="left")

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
        if getattr(self, "_view", "time") == "fft":
            self._update_fft_cursor_readout(cursors)
            return {}
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

    def _update_fft_cursor_readout(self, cursors):
        """Cursor readout for FFT view: frequency + magnitude at each marker."""
        import numpy as np
        f1 = cursors.get("t1")
        f2 = cursors.get("t2")
        m1 = cursors.get("v1")
        m2 = cursors.get("v2")
        if f1 is None and f2 is None and m1 is None and m2 is None:
            if hasattr(self, "cursor_text"):
                self.cursor_text.set("Cursors: none placed.")
            return
        channel = self.analysis_channel()
        options = getattr(self, "_fft_options", {})
        spectrum = analysis.fft_spectrum(channel,
                                         window=options.get("window", "hanning"),
                                         fmt=options.get("format", "dBV"))
        unit = (spectrum or {}).get("unit", "dBV") if spectrum else "dBV"
        parts = []
        # Frequency cursors (vertical lines F1, F2)
        def mag_at(freq):
            if spectrum is None:
                return None
            freqs = spectrum["frequencies"]
            vals = spectrum["values"]
            if freqs is None or vals is None or len(freqs) == 0:
                return None
            idx = int(np.argmin(np.abs(freqs - freq)))
            return float(vals[idx])
        if f1 is not None:
            mag = mag_at(f1)
            s = "F1 %s" % analysis.format_hz(f1)
            if mag is not None:
                s += " (%.3g %s)" % (mag, unit)
            parts.append(s)
        if f2 is not None:
            mag = mag_at(f2)
            s = "F2 %s" % analysis.format_hz(f2)
            if mag is not None:
                s += " (%.3g %s)" % (mag, unit)
            parts.append(s)
        if f1 is not None and f2 is not None:
            delta = abs(f2 - f1)
            parts.append("\u0394F %s" % analysis.format_hz(delta))
            if delta > 0:
                parts.append("1/\u0394F %s" % analysis.format_seconds(1.0 / delta))
        # Magnitude cursors (horizontal lines M1, M2)
        if m1 is not None:
            parts.append("M1 %.3g %s" % (m1, unit))
        if m2 is not None:
            parts.append("M2 %.3g %s" % (m2, unit))
        if m1 is not None and m2 is not None:
            parts.append("\u0394M %.3g %s" % (abs(m2 - m1), unit))
        line = "   ".join(parts) if parts else "Cursors: none placed."
        if hasattr(self, "cursor_text"):
            self.cursor_text.set(line)

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
            number = self.channel_number(channel)
            if number:
                y = y * self.display_ratio(number, channel)
                y = y + self.display_offset(number)
            x = np.arange(y.size, dtype=float) * float(channel.get("point_interval", 1.0) or 1.0) * factor

            # Software averaging: blend with the ring buffer if depth > 1.
            depth = getattr(self, "_avg_depth", 1)
            if depth > 1:
                from collections import deque
                buf = getattr(self, "_avg_buf", {})
                if name not in buf:
                    buf[name] = deque(maxlen=depth)
                buf[name].append(y.copy())
                self._avg_buf = buf
                if len(buf[name]) > 1:
                    y = np.mean(np.stack(list(buf[name])), axis=0)

            # Feed persistence buffer with the pre-average live frame.
            self._push_persist(name, y)

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
            getattr(self, "_zoom_xlim", None),
            getattr(self, "_zoom_ylim", None),
            getattr(self, "_ref_trace", None) is not None,
            getattr(self, "_avg_depth", 1),
            getattr(self, "_persist_depth", 0),
        )
        fast_path = (
            plotted
            and framing is not None
            and getattr(self, "_plot_frame_key", None) == frame_key
            and getattr(self, "_persist_depth", 0) == 0   # persistence always redraws
            and getattr(self, "_mask", None) is None        # mask always redraws
        )

        if fast_path:
            # Update the existing Line2D objects in-place via set_data().
            # This avoids any add/remove of artists which marks the axes stale
            # and triggers matplotlib's autoscale, overriding the ylim set by
            # show_graticule().
            lines = getattr(self, "_dynamic_artists", [])
            for i, (_name, color, x, y) in enumerate(per_channel_xy):
                if i < len(lines):
                    lines[i].set_data(x, y)
                else:
                    # More channels than last frame — append a new line.
                    line, = self.ax.plot(x, y, color=color, linewidth=1.4)
                    lines.append(line)
            # Hide any leftover lines from a previous frame with more channels.
            for j in range(len(per_channel_xy), len(lines)):
                lines[j].set_visible(False)
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
                self.draw_persist_traces(per_channel_xy)
                # Pass/Fail mask (first channel only).
                if per_channel_xy and getattr(self, "_mask", None) is not None:
                    _nm, _col, mx, my = per_channel_xy[0]
                    self.draw_mask(mx, my)
                self.show_graticule(framing, window, unit, factor)
                # Apply zoom limits if the user has dragged a rectangle.
                xlim = getattr(self, "_zoom_xlim", None)
                ylim = getattr(self, "_zoom_ylim", None)
                if xlim:
                    self.ax.set_xlim(*xlim)
                if ylim:
                    self.ax.set_ylim(*ylim)
            self._plot_frame_key = frame_key

        self.draw_ref_trace()
        self.draw_protocol_frames()
        self.draw_reference_lines(plotted)
        self.draw_zero_markers(references if plotted else [])
        self.draw_measurement_overlay(channels)
        # Lock limits now — draw_reference_lines and draw_zero_markers add artists
        # (axhline, annotate) which mark the axes stale.  Without this,
        # draw_idle() re-runs autoscale and overrides the ylim set by show_graticule.
        self.ax.autoscale(False)
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

    # ------------------------------------------------------------------
    # Live measurement overlay on time plot
    def draw_measurement_overlay(self, channels):
        """Update the Tk overlay label with live measurements.

        The label is a plain tk.Label placed over the canvas with place().
        It never touches matplotlib, so it cannot affect axes limits or
        trigger autoscale.  The label text is updated every frame via
        label.configure(text=...) — instant, no redraw cost.
        """
        label = getattr(self, "_overlay_label", None)
        if label is None:
            return

        if not getattr(self, "_meas_overlay_on", True):
            label.place_forget()
            return

        if not channels:
            label.place_forget()
            return

        channel = channels[0]
        try:
            meas = analysis.waveform_measurements(channel)
        except Exception:
            label.place_forget()
            return
        if not meas:
            label.place_forget()
            return

        # Which slots to show — user-configurable, default to the most useful 6.
        slots = getattr(self, "_meas_slots",
                        ["freq", "period", "vpp", "vrms", "duty", "rise"])

        SLOT_MAP = {
            "freq":       ("Freq",  meas.get("frequency"),    analysis.format_hz),
            "period":     ("Per",   meas.get("period"),       analysis.format_seconds),
            "vpp":        ("Vpp",   meas.get("vpp"),          analysis.format_volts),
            "vmax":       ("Vmax",  meas.get("vmax"),         analysis.format_volts),
            "vmin":       ("Vmin",  meas.get("vmin"),         analysis.format_volts),
            "vmean":      ("Mean",  meas.get("vmean"),        analysis.format_volts),
            "vrms":       ("RMS",   meas.get("vrms"),         analysis.format_volts),
            "vamp":       ("Vamp",  meas.get("vamp"),         analysis.format_volts),
            "vtop":       ("Vtop",  meas.get("vtop"),         analysis.format_volts),
            "vbase":      ("Vbase", meas.get("vbase"),        analysis.format_volts),
            "duty":       ("Duty",  meas.get("duty_pct"),     lambda v: "%.1f%%" % v),
            "rise":       ("Rise",  meas.get("rise_time"),    analysis.format_seconds),
            "fall":       ("Fall",  meas.get("fall_time"),    analysis.format_seconds),
            "width+":     ("+Wid",  meas.get("width_pos"),    analysis.format_seconds),
            "width-":     ("-Wid",  meas.get("width_neg"),    analysis.format_seconds),
            "overshoot":  ("Ovr",   meas.get("overshoot_pct"),lambda v: "%.1f%%" % v),
            "undershoot": ("Und",   meas.get("undershoot_pct"),lambda v: "%.1f%%" % v),
        }

        parts = []
        for slot in slots:
            entry = SLOT_MAP.get(slot)
            if entry is None:
                continue
            lbl, value, fmt = entry
            if value is None:
                parts.append("%-6s  ---" % lbl)
            else:
                try:
                    parts.append("%-6s  %s" % (lbl, fmt(value)))
                except Exception:
                    parts.append("%-6s  ?" % lbl)

        if not parts:
            label.place_forget()
            return

        label.configure(text="\n".join(parts))

        # Position in the top-right corner of the canvas, inset by 8 px.
        try:
            cw = self.canvas.get_tk_widget().winfo_width()
            label.update_idletasks()
            lw = label.winfo_reqwidth()
            label.place(x=cw - lw - 8, y=8, anchor="nw")
        except Exception:
            label.place(relx=1.0, rely=0.0, x=-8, y=8, anchor="ne")

    # ------------------------------------------------------------------
    # Software averaging
    def set_avg_depth(self, depth):
        """Set how many frames to average. 1 = off."""
        from collections import deque
        self._avg_depth = max(1, int(depth))
        # Clear the buffer whenever depth changes so stale frames don't bleed in.
        self._avg_buf = {}
        self.log("Averaging: %d frame%s" % (self._avg_depth,
                                             "" if self._avg_depth == 1 else "s"))

    def clear_avg_buf(self):
        """Discard the averaging ring buffer (e.g. after a settings change)."""
        self._avg_buf = {}
        self.log("Averaging buffer cleared.")

    # ------------------------------------------------------------------
    # Reference trace overlay
    def save_ref_trace(self):
        """Save the current live trace as the reference overlay."""
        channels = self.capture_channels()
        for channel in channels:
            y = np.asarray(channel.get("waveform", []), dtype=float)
            if y.size == 0:
                continue
            name = str(channel.get("name", "CH1")).upper()
            number = self.channel_number(channel)
            if number:
                y = y * self.display_ratio(number, channel)
                y = y + self.display_offset(number)
            factor, _unit, _span = 1.0, "s", None
            interval = float(channel.get("point_interval", 0) or 0)
            if interval and y.size:
                factor, _unit = self.time_axis_units(interval * (y.size - 1))
            x = np.arange(y.size, dtype=float) * interval * factor
            label = "%s  %s" % (name, analysis.format_volts(float(np.max(y) - np.min(y))) + " pk-pk")
            self._ref_trace = (x, y, label)
            if hasattr(self, "ref_label"):
                self.ref_label.set(label)
            self.log("Reference saved: %s" % label)
            # Only save the first channel as reference.
            break
        self._plot_frame_key = None
        self.plot_waveform()

    def clear_ref_trace(self):
        """Remove the reference overlay."""
        self._ref_trace = None
        if hasattr(self, "ref_label"):
            self.ref_label.set("none")
        self._plot_frame_key = None
        self.plot_waveform()

    def draw_ref_trace(self):
        """Draw the saved reference trace behind the live trace (dimmed, dashed)."""
        ref = getattr(self, "_ref_trace", None)
        if ref is None:
            return
        x, y, label = ref
        if not hasattr(self, "_ref_trace_artists"):
            self._ref_trace_artists = []
        for a in self._ref_trace_artists:
            try:
                a.remove()
            except Exception:
                pass
        self._ref_trace_artists = []
        line, = self.ax.plot(x, y, color="#7a8a7a", linewidth=1.0,
                              linestyle="--", alpha=0.55, zorder=1)
        annot = self.ax.annotate("REF", xy=(x[-1], y[-1]),
                                  xycoords="data", fontsize=7,
                                  color="#7a8a7a", alpha=0.75,
                                  xytext=(3, 0), textcoords="offset points",
                                  va="center")
        self._ref_trace_artists = [line, annot]

    # ------------------------------------------------------------------
    # Persistence (digital phosphor)
    def set_persist_depth(self, depth):
        """Set how many old frames to ghost behind the live trace. 0 = off."""
        from collections import deque
        self._persist_depth = max(0, int(depth))
        if self._persist_depth == 0:
            self._persist_buf = {}
        self.log("Persistence: %d frame%s" % (
            self._persist_depth, "" if self._persist_depth == 1 else "s"))

    def clear_persist_buf(self):
        """Discard all ghosted frames."""
        self._persist_buf = {}
        self._plot_frame_key = None
        self.plot_waveform()

    def _push_persist(self, channel_name, y):
        """Add the current frame to the persistence buffer for one channel."""
        from collections import deque
        depth = getattr(self, "_persist_depth", 0)
        if depth == 0:
            return
        buf = getattr(self, "_persist_buf", {})
        if channel_name not in buf:
            buf[channel_name] = deque(maxlen=depth)
        buf[channel_name].append(y.copy())
        self._persist_buf = buf

    def draw_persist_traces(self, per_channel_xy):
        """Draw dimmed ghost traces from the persistence buffer."""
        depth = getattr(self, "_persist_depth", 0)
        if depth == 0:
            return
        buf = getattr(self, "_persist_buf", {})
        if not buf:
            return
        palette = {
            "CH1": "#9cdc9c", "CH2": "#7bd6ff",
        }
        for name, _color, x, _y in per_channel_xy:
            frames = buf.get(name)
            if not frames:
                continue
            color = palette.get(name, "#9cdc9c")
            n = len(frames)
            for i, old_y in enumerate(frames):
                # Oldest frame → most transparent; newest → least.
                alpha = 0.08 + 0.30 * (i / max(n - 1, 1))
                if old_y.size != x.size:
                    continue
                self.ax.plot(x, old_y, color=color, linewidth=0.8,
                             alpha=alpha, zorder=0)

    # ------------------------------------------------------------------
    # Zoom
    def zoom_out(self):
        """Reset the zoom to the full capture window."""
        self._zoom_xlim = None
        self._zoom_ylim = None
        self._plot_frame_key = None
        if hasattr(self, "_zoom_btn"):
            self._zoom_btn.configure(state="disabled")
        self.plot_waveform()

    # ------------------------------------------------------------------
    # Protocol decode
    def run_protocol_decode(self):
        """Decode the current capture with the selected protocol and annotate the plot."""
        from modernlab.decode import uart, spi, i2c, can

        channels = self.capture_channels()
        if not channels:
            self.log("Protocol decode: no capture available.", "WARNING")
            return

        # Collect the first two channel arrays.
        ch_data = []
        for ch in channels[:2]:
            y = np.asarray(ch.get("waveform", []), dtype=float)
            if y.size == 0:
                continue
            number = self.channel_number(ch)
            if number:
                y = y * self.display_ratio(number, ch)
                y = y + self.display_offset(number)
            interval = float(ch.get("point_interval", 0) or 0)
            factor, _unit = self.time_axis_units(interval * (y.size - 1)) if interval and y.size else (1.0, "s")
            x = np.arange(y.size, dtype=float) * interval * factor
            ch_data.append((str(ch.get("name", "CH1")).upper(), x, y))

        if not ch_data:
            self.log("Protocol decode: channel data is empty.", "WARNING")
            return

        protocol = getattr(self, "proto_var", None)
        protocol = protocol.get() if protocol else "UART"

        baud_str = getattr(self, "proto_baud_var", None)
        baud_str = (baud_str.get() if baud_str else "auto").strip().lower()
        baud = None if baud_str in ("", "auto") else int(float(baud_str))

        thr_str = getattr(self, "proto_thr_var", None)
        thr_str = (thr_str.get() if thr_str else "auto").strip().lower()
        threshold = None if thr_str in ("", "auto") else float(thr_str)

        invert = bool(getattr(self, "proto_invert", None) and self.proto_invert.get())

        name0, x0, y0 = ch_data[0]

        try:
            if protocol == "UART":
                frames = uart.decode(x0, y0, baud=baud, threshold=threshold,
                                     invert=invert, channel=name0)
                summary = uart.describe(frames)
            elif protocol == "SPI":
                if len(ch_data) < 2:
                    frames = spi.decode(x0, y0, threshold=threshold, channel_mosi=name0)
                else:
                    _, x1, y1 = ch_data[1]
                    frames = spi.decode(x0, y0, x1, y1, threshold=threshold,
                                        channel_mosi=name0, channel_miso=ch_data[1][0])
                summary = spi.describe(frames)
            elif protocol == "I2C":
                if len(ch_data) < 2:
                    frames = []; summary = "I2C needs two channels (SCL + SDA)."
                else:
                    _, x1, y1 = ch_data[1]
                    frames = i2c.decode(x0, y0, x1, y1, threshold=threshold)
                    summary = i2c.describe(frames)
            elif protocol == "CAN":
                frames = can.decode(x0, y0, bit_rate=baud, threshold=threshold,
                                    channel=name0)
                summary = can.describe(frames)
            else:
                frames = []; summary = "Unknown protocol."
        except Exception as exc:
            self.log("Protocol decode error: %s" % exc, "ERROR")
            if hasattr(self, "proto_result_var"):
                self.proto_result_var.set("Error: %s" % exc)
            return

        self._proto_frames = frames
        if hasattr(self, "proto_result_var"):
            self.proto_result_var.set(summary)
        self.log(summary)
        self._plot_frame_key = None
        self.plot_waveform()

    def clear_protocol_decode(self):
        """Remove protocol decode annotations from the plot."""
        self._proto_frames = []
        for a in getattr(self, "_proto_artists", []):
            try:
                a.remove()
            except Exception:
                pass
        self._proto_artists = []
        if hasattr(self, "proto_result_var"):
            self.proto_result_var.set("")
        self._plot_frame_key = None
        self.plot_waveform()

    def draw_protocol_frames(self):
        """Draw decoded protocol frames as annotated spans on the time plot."""
        frames = getattr(self, "_proto_frames", [])
        artists = getattr(self, "_proto_artists", [])
        for a in artists:
            try:
                a.remove()
            except Exception:
                pass
        self._proto_artists = []
        if not frames:
            return

        # Colour map per frame kind.
        KIND_COLORS = {
            "data":    "#7bd6ff",
            "address": "#ffd166",
            "start":   "#9cdc9c",
            "stop":    "#9cdc9c",
            "ack":     "#9cdc9c",
            "nak":     "#ff7b72",
            "error":   "#ff7b72",
            "id":      "#ffd166",
            "control": "#c792ea",
            "crc":     "#888",
            "eof":     "#555",
            "sof":     "#9cdc9c",
            "clock":   "#555",
        }
        ylim = self.ax.get_ylim()
        y_lo, y_hi = ylim
        span_h = (y_hi - y_lo) * 0.08   # annotation band at the bottom

        for frame in frames:
            color = KIND_COLORS.get(frame.kind, "#7bd6ff")
            # Span the width of the frame.
            if frame.t_start < frame.t_end:
                span = self.ax.axvspan(frame.t_start, frame.t_end,
                                       ymin=0.0, ymax=0.08,
                                       color=color, alpha=0.35, zorder=2)
                self._proto_artists.append(span)
            # Label above the span.
            label = frame.label or frame.kind
            if len(label) > 12:
                label = label[:11] + "…"
            t_mid = (frame.t_start + frame.t_end) / 2.0 if frame.t_end > frame.t_start else frame.t_start
            ann = self.ax.annotate(
                label,
                xy=(t_mid, y_lo + span_h * 1.1),
                fontsize=6.5, color=color,
                ha="center", va="bottom", rotation=90 if frame.t_end - frame.t_start < 1e-6 else 0,
                annotation_clip=True,
            )
            self._proto_artists.append(ann)

    # ------------------------------------------------------------------
    # Pass / Fail mask
    def save_mask(self, tolerance_pct=10.0):
        """Build an envelope from the current capture and store it as the mask.

        The upper band is the trace + tolerance% of Vpp; the lower band is
        the trace - that same margin.  tolerance_pct comes from the UI spinner.
        """
        channels = self.capture_channels()
        for channel in channels:
            y = np.asarray(channel.get("waveform", []), dtype=float)
            if y.size == 0:
                continue
            number = self.channel_number(channel)
            if number:
                y = y * self.display_ratio(number, channel)
                y = y + self.display_offset(number)
            interval = float(channel.get("point_interval", 0) or 0)
            factor, _unit = self.time_axis_units(interval * (y.size - 1)) if interval and y.size else (1.0, "s")
            x = np.arange(y.size, dtype=float) * interval * factor
            margin = (np.max(y) - np.min(y)) * tolerance_pct / 100.0
            self._mask = (x, y - margin, y + margin)
            self._mask_pass = 0
            self._mask_fail = 0
            self._plot_frame_key = None
            vpp = float(np.max(y) - np.min(y))
            self.log("Mask saved: ±%.1f %% (±%s) of %.3g V pk-pk" % (
                tolerance_pct, analysis.format_volts(margin), vpp))
            if hasattr(self, "mask_status_var"):
                self.mask_status_var.set("Mask active  P:0  F:0")
            self.plot_waveform()
            break

    def clear_mask(self):
        """Remove the pass/fail mask."""
        self._mask = None
        self._mask_pass = 0
        self._mask_fail = 0
        self._plot_frame_key = None
        self.log("Mask cleared.")
        if hasattr(self, "mask_status_var"):
            self.mask_status_var.set("No mask")
        self.plot_waveform()

    def test_mask(self, x, y):
        """Test a (x, y) trace against the stored mask. Returns True=pass."""
        mask = getattr(self, "_mask", None)
        if mask is None:
            return True
        mx, y_lo, y_hi = mask
        # Interpolate the mask to the trace's x positions.
        y_lo_i = np.interp(x, mx, y_lo, left=y_lo[0], right=y_lo[-1])
        y_hi_i = np.interp(x, mx, y_hi, left=y_hi[0], right=y_hi[-1])
        return bool(np.all(y >= y_lo_i) and np.all(y <= y_hi_i))

    def draw_mask(self, x, y):
        """Draw the mask envelope and colour the trace based on pass/fail."""
        mask = getattr(self, "_mask", None)
        if mask is None:
            return
        mx, y_lo, y_hi = mask
        # Interpolate to trace x axis.
        y_lo_i = np.interp(x, mx, y_lo, left=y_lo[0], right=y_lo[-1])
        y_hi_i = np.interp(x, mx, y_hi, left=y_hi[0], right=y_hi[-1])
        passed = bool(np.all(y >= y_lo_i) and np.all(y <= y_hi_i))

        # Update counters.
        if passed:
            self._mask_pass = getattr(self, "_mask_pass", 0) + 1
        else:
            self._mask_fail = getattr(self, "_mask_fail", 0) + 1

        # Draw the envelope as a filled band.
        self.ax.fill_between(x, y_lo_i, y_hi_i,
                             alpha=0.12, color="#9cdc9c", zorder=1)
        self.ax.plot(x, y_lo_i, color="#9cdc9c", linewidth=0.7,
                     linestyle="--", alpha=0.6, zorder=2)
        self.ax.plot(x, y_hi_i, color="#9cdc9c", linewidth=0.7,
                     linestyle="--", alpha=0.6, zorder=2)

        # Status badge.
        p = getattr(self, "_mask_pass", 0)
        f = getattr(self, "_mask_fail", 0)
        badge_color = "#9cdc9c" if passed else "#ff7b72"
        badge_text = "PASS  P:%d  F:%d" % (p, f) if passed else "FAIL  P:%d  F:%d" % (p, f)
        self.ax.text(0.02, 0.97, badge_text,
                     transform=self.ax.transAxes,
                     ha="left", va="top", fontsize=9, fontweight="bold",
                     color=badge_color,
                     bbox=dict(boxstyle="round,pad=0.3",
                               facecolor="#101719", edgecolor=badge_color,
                               alpha=0.9))
        if hasattr(self, "mask_status_var"):
            self.mask_status_var.set(badge_text)
