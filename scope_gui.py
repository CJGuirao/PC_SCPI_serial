import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import threading
import time
import json
from datetime import datetime
from owon_controller import OWONScopeController

class ScopeGUI:
    """Enhanced GUI for OWON Oscilloscope"""
    def __init__(self, master):
        self.master = master
        master.title("OWON Oscilloscope Control")

        # Initialize the scope controller
        self.scope = OWONScopeController()

        # Create the GUI components
        self.create_widgets()

    def create_widgets(self):
        """Create and grid the GUI components."""
        # ...existing widget creation code...

    def on_connect(self):
        """Handle the connect button click."""
        # ...existing connect handling code...

    def on_disconnect(self):
        """Handle the disconnect button click."""
        # ...existing disconnect handling code...

    def update_plot(self, data):
        """Update the plot with new data."""
        # ...existing plot updating code...

    def show_message(self, title, message):
        """Show a message box with the given title and message."""
        messagebox.showinfo(title, message)

    def get_save_path(self):
        """Open a file dialog to get the save path from the user."""
        return filedialog.asksaveasfilename(defaultextension=".json",
                                              filetypes=[("JSON files", "*.json"),
                                                         ("All files", "*.*")])

    def save_data(self, data):
        """Save the given data to a file."""
        path = self.get_save_path()
        if path:
            with open(path, 'w') as f:
                json.dump(data, f, indent=4)
            self.show_message("Data Saved", f"Data successfully saved to {path}")

    def load_data(self):
        """Load data from a file and update the plot."""
        path = filedialog.askopenfilename(filetypes=[("JSON files", "*.json"),
                                                      ("All files", "*.*")])
        if path:
            with open(path, 'r') as f:
                data = json.load(f)
            self.update_plot(data)
            self.show_message("Data Loaded", "Data successfully loaded from file")

    def start_acquisition(self):
        """Start data acquisition from the oscilloscope."""
        # ...existing acquisition starting code...

    def stop_acquisition(self):
        """Stop data acquisition from the oscilloscope."""
        # ...existing acquisition stopping code...

    def run(self):
        """Run the GUI main loop."""
        self.master.mainloop()