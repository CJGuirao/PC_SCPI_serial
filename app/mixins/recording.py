"""Unattended recording: capture every frame to a folder."""

from modernlab.app.io_worker import PRIORITY_REFRESH
from modernlab import analysis
from modernlab.storage import export
from tkinter import messagebox
import os
import time


class RecordingMixin:
    """Unattended recording for the OWON panel."""

    def toggle_recording(self):
        """Start or stop writing every capture to the record folder."""
        if self.recording:
            self.recording = False
            self.record_label.set("%d file(s) written" % self._records if self._records else "idle")
            self.log("Recording stopped after %d capture(s)." % self._records)
            return
        folder = (getattr(self.setup, "record_folder", "") or "").strip()
        if not folder:
            messagebox.showinfo("Record", "Set a record folder in SETUP first:\n\n"
                                          "SETUP \u2192 Record folder")
            return
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            self.log("Cannot use %s: %s" % (folder, exc), "ERROR")
            messagebox.showerror("Record", "Cannot write to %s:\n%s" % (folder, exc))
            return
        self.recording = True
        self._records = 0
        self.record_label.set("to %s" % folder)
        self.log("Recording every capture to %s" % folder)

    def record_capture(self):
        """Write the capture that just arrived, if recording is on.

        One file per frame, named by the second it was taken in: the point of
        leaving it running is to have the frames afterwards, so a failure to write
        stops the recording rather than going quiet.
        """
        if not self.recording:
            return None
        channels = self.capture_channels()
        if not channels:
            return None
        folder = (getattr(self.setup, "record_folder", "") or "").strip()
        name = time.strftime("capture-%Y%m%d-%H%M%S", time.localtime())
        path = os.path.join(folder, "%s-%04d.csv" % (name, self._records))
        provenance = self.provenance_block()
        self._records += 1
        # Written on the worker, because the folder may be a network share and a
        # frame does not wait for a file. A write that fails still stops the
        # recording, which is the point of leaving it running unattended.
        self.ask_scope("record",
                       lambda scope: export.write_csv(path, channels, provenance),
                       priority=PRIORITY_REFRESH)
        return path

    def provenance_block(self):
        """The settings an exported capture is only checkable with."""
        waveform = getattr(self.scope, "waveform", None)
        channel = self.analysis_channel()
        options = getattr(self, "_fft_options", {})
        return export.provenance(
            model=getattr(waveform, "model", "") or getattr(self.scope, "model", ""),
            serial=getattr(self.scope, "serial_number", ""),
            firmware=getattr(self.scope, "firmware", ""),
            timebase_scale_s=getattr(waveform, "timebase_scale", None),
            sample_rate=getattr(waveform, "sample_rate", ""),
            point_interval_s=analysis.point_interval(channel) if channel else None,
            points=len(channel.get("waveform") or []) if channel else 0,
            window=options.get("window"),
            fft_format=options.get("format"),
            view=getattr(self, "_view", "time"),
            calibration_trim=getattr(self.setup, "calibration_trim", None),
            volts_per_code=getattr(self.scope, "_calibrated_volts_per_code", None),
            note="probe %s, coupling %s" % (channel.get("probe") if channel else "",
                                            channel.get("coupling") if channel else ""))
