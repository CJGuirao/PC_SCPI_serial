"""Files and console: capture files, waveform export, SCPI console."""

from modernlab.storage import export
from tkinter import filedialog
from tkinter import messagebox
import os
import tkinter as tk


class FilesMixin:
    """Capture files and the SCPI console for the OWON panel."""

    def load_capture_file(self, path):
        """Show a saved capture in place of a live one.

        The live loop is stopped first: it re-plots from the instrument every
        second, so a file opened underneath it would disappear before it could be
        read.
        """
        try:
            settings, channels = export.read_capture(path)
        except Exception as exc:
            self.log("Could not open %s: %s" % (path, exc), "ERROR")
            messagebox.showerror("Open capture", "Could not open that file:\n%s" % exc)
            return None
        if not channels:
            messagebox.showwarning("Open capture", "That file has no samples in it.")
            return None
        if self.auto_refresh_var.get():
            self.toggle_live()
        loaded = export.LoadedCapture(channels, settings, source=os.path.basename(path))
        self.scope.waveform = loaded
        self._loaded_capture = loaded
        self.capture_state.set("FILE \u2022 %s" % os.path.basename(path))
        self.device_info.set(loaded.describe())
        self.log("Opened %s (%s)" % (path, loaded.describe()))
        self.plot_waveform()
        return loaded

    def run_scpi_command(self, text, allow_writes):
        """Send one command to the instrument and hand back what it said.

        A write is refused unless it was asked for explicitly: this console sends
        whatever is typed, and a write on this unit can leave the instrument in a
        state where later captures report values that were never on the input.
        """
        if not self._is_connected():
            return "not connected"
        command = str(text or "").strip()
        if not command:
            return "nothing to send"
        is_query = "?" in command
        if not is_query and not allow_writes:
            return "refused: that looks like a write. Tick 'Allow writes' if you mean it."
        # A command is a round trip - 32 ms for a text query, far more for a capture
        # - so it goes to the worker and the console prints the reply when it lands.
        # Sent from here it froze the window for as long as the instrument took.
        work = (lambda scope: scope.query(command)) if is_query else \
               (lambda scope: scope.send_command(command))
        self.report_later("console", work,
                          lambda value, error, sent=command: self.deliver_scpi_reply(sent, value, error))
        return "sent: %s" % command

    def console_reply(self, text):
        """Hand a late reply to the console, if one is still open."""
        sink = getattr(self, "_console_sink", None)
        if sink is None:
            return
        try:
            sink(text)
        except tk.TclError:
            self._console_sink = None

    def deliver_scpi_reply(self, command, reply, error=None):
        """Print what the instrument said, or why there is nothing to print."""
        if error is not None:
            self.log("SCPI %s failed: %s" % (command, error), "ERROR")
            self.console_reply("SCPI %s failed: %s" % (command, error))
            return
        answer = "" if reply is None else str(reply).strip()
        self.log("SCPI %s \u2192 %s" % (command, answer or "(no reply)"))
        self.console_reply(answer or "(no reply)")

    def save_plot_image(self):
        """Save the current plot as a timestamped PNG in the record folder (or home).

        No dialog — one click, one file.  The filename embeds the date and time
        so repeated saves never clobber each other.
        """
        import time, os
        folder = (getattr(self.setup, "record_folder", "") or "").strip()
        if not folder:
            folder = os.path.expanduser("~")
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(folder, "scope-%s.png" % stamp)
        try:
            # Include cursor readout as a caption when cursors are placed.
            cursor_line = ""
            if hasattr(self, "cursor_text"):
                cursor_line = self.cursor_text.get()
            # Add the caption as a suptitle below the plot.
            if cursor_line and "none placed" not in cursor_line.lower():
                self.fig.suptitle(cursor_line, fontsize=7,
                                  color="#9cf0c9", y=0.01, va="bottom")
            self.fig.savefig(path, dpi=150, bbox_inches="tight",
                             facecolor=self.fig.get_facecolor())
            self.fig.suptitle("")        # clear the temporary caption
            self.canvas.draw_idle()
        except Exception as exc:
            self.log("Image export failed: %s" % exc, "ERROR")
            messagebox.showerror("Export image", "Could not save image:\n%s" % exc)
            return
        self.log("Plot saved: %s" % path)
        self.status_var.set("Saved: %s" % os.path.basename(path))

    def save_waveform(self):
        """Write the capture in whichever format the chosen filename asks for.

        Every format carries its provenance where it has room: a file of volts with
        no record of the scale, probe and calibration that produced them is a file
        nobody can check later.
        """
        channels = self.capture_channels()
        if not channels:
            messagebox.showinfo("Save capture", "No capture to save. Take one first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save capture",
            defaultextension=".csv",
            filetypes=[("CSV - samples", "*.csv"), ("JSON - samples and settings", "*.json"),
                       ("Excel workbook", "*.xlsx"), ("PNG image of the plot", "*.png"),
                       ("PDF of the plot", "*.pdf"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            described = export.export_capture(path, channels,
                                                      self.provenance_block(), figure=self.fig)
        except Exception as exc:
            self.log("Save failed: %s" % exc, "ERROR")
            messagebox.showerror("Save capture", "Could not write that file:\n%s" % exc)
            return
        self.log("Saved %s" % described)
        messagebox.showinfo("Save capture", "Saved:\n%s\n\n%s" % (path, described))
