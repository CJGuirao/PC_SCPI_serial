from waveform_data import WaveformData
from owon_controller import OWONScopeController
from scope_gui import ScopeGUI

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import time
import numpy as np
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import logging
import json
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

class App:
    """Main application class"""
    
    def __init__(self, root):
        self.root = root
        self.root.title("OWON SDS Oscilloscope Control Center")
        self.root.geometry("1600x1000")
        
        self.scope = OWONScopeController()
        
        # Color scheme
        self.bg_color = "#2b2b2b"
        self.fg_color = "#ffffff"
        self.accent_color = "#4a90e2"
        
        # Configure style
        self.style = ttk.Style()
        self.style.theme_use('clam')
        
        # Matplotlib figure
        self.fig = Figure(figsize=(12, 6), facecolor='#1e1e1e')
        self.ax = self.fig.add_subplot(111, facecolor='#1e1e1e')
        self.canvas = None
        
        self.setup_gui()
        self.auto_refresh = False
        self.refresh_thread = None
        
    def setup_gui(self):
        """Setup enhanced GUI"""
        # Main container with grid
        main_container = ttk.Frame(self.root)
        main_container.pack(fill=tk.BOTH, expand=True)
        
        # Configure grid weights
        main_container.grid_columnconfigure(0, weight=1)
        main_container.grid_columnconfigure(1, weight=4)
        main_container.grid_columnconfigure(2, weight=1)
        main_container.grid_rowconfigure(0, weight=1)
        
        # Left panel - Controls
        left_panel = ttk.Frame(main_container)
        left_panel.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        
        # Right panel - Graph and measurements
        right_panel = ttk.Frame(main_container)
        right_panel.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)
        
         # Right panel - Graph and measurements
        right2_panel = ttk.Frame(main_container)
        right2_panel.grid(row=0, column=2, sticky="nsew", padx=5, pady=5)
        
        # Setup left panel sections
        self.setup_connection_panel(left_panel)
        self.setup_channel_controls(left_panel)
        self.setup_timebase_controls(left_panel)
        self.setup_trigger_controls(left_panel)
        self.setup_acquisition_controls(right2_panel)
        self.setup_measurement_controls(right2_panel)
        
        # Setup right panel
        self.setup_graph_panel(right_panel)
        self.setup_measurement_display(right_panel)
        
        # Status bar
        self.setup_status_bar()
        
    def setup_connection_panel(self, parent):
        """Setup connection controls"""
        conn_frame = ttk.LabelFrame(parent, text="🔌 Connection", padding=10)
        conn_frame.pack(fill=tk.X, pady=5)
        
        # Connection type
        ttk.Label(conn_frame, text="Type:").grid(row=0, column=0, sticky=tk.W)
        self.conn_type = tk.StringVar(value="lan")
        ttk.Radiobutton(conn_frame, text="LAN", variable=self.conn_type, 
                       value="lan").grid(row=0, column=1, sticky=tk.W)
        ttk.Radiobutton(conn_frame, text="USB", variable=self.conn_type, 
                       value="usb").grid(row=0, column=2, sticky=tk.W)
        
        # Address/Port
        ttk.Label(conn_frame, text="Address:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.conn_address = ttk.Entry(conn_frame, width=18)
        self.conn_address.insert(0, "10.1.1.131")
        self.conn_address.grid(row=1, column=1, columnspan=2, sticky=tk.W+tk.E, pady=5)
        
        # Connect button
        self.connect_btn = ttk.Button(conn_frame, text="Connect", 
                                     command=self.toggle_connection)
        self.connect_btn.grid(row=2, column=0, columnspan=3, sticky=tk.W+tk.E, pady=5)
        
        # Device info
        self.device_info = tk.StringVar(value="Not Connected")
        # ttk.Label(conn_frame, textvariable=self.device_info, 
        #          wraplength=200, justify=tk.LEFT).grid(row=3, column=0, 
        #                                                 columnspan=3, pady=5)
    
    def setup_channel_controls(self, parent):
        """Setup channel control panel"""
        ch_frame = ttk.LabelFrame(parent, text="📊 Channel Controls", padding=10)
        ch_frame.pack(fill=tk.X, pady=5)
        
        # Channel 1
        ttk.Label(ch_frame, text="CH1:", font=('Arial', 10, 'bold')).grid(row=0, column=0, sticky=tk.W)
        
        self.ch1_display = tk.BooleanVar(value=True)
        ttk.Checkbutton(ch_frame, text="Display", variable=self.ch1_display,
                       command=lambda: self.set_channel_display(1, self.ch1_display.get())).grid(row=0, column=1, sticky=tk.W)
        
        ttk.Label(ch_frame, text="Scale:").grid(row=1, column=0, sticky=tk.W)
        self.ch1_scale = ttk.Combobox(ch_frame, values=self.scope.VOLTAGE_SCALES, 
                                      width=10, state='readonly')
        self.ch1_scale.set("1v")
        self.ch1_scale.grid(row=1, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(ch_frame, text="Set", 
                  command=lambda: self.set_channel_scale(1, self.ch1_scale.get())).grid(row=1, column=2, padx=2)
        
        ttk.Label(ch_frame, text="Coupling:").grid(row=2, column=0, sticky=tk.W)
        self.ch1_coupling = ttk.Combobox(ch_frame, values=self.scope.COUPLING_MODES, 
                                         width=10, state='readonly')
        self.ch1_coupling.set("DC")
        self.ch1_coupling.grid(row=2, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(ch_frame, text="Set", 
                  command=lambda: self.set_channel_coupling(1, self.ch1_coupling.get())).grid(row=2, column=2, padx=2)
        
        ttk.Label(ch_frame, text="Probe:").grid(row=3, column=0, sticky=tk.W)
        self.ch1_probe = ttk.Combobox(ch_frame, values=self.scope.PROBE_ATTEN, 
                                      width=10, state='readonly')
        self.ch1_probe.set("X10")
        self.ch1_probe.grid(row=3, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(ch_frame, text="Set", 
                  command=lambda: self.set_channel_probe(1, self.ch1_probe.get())).grid(row=3, column=2, padx=2)
        
        ttk.Label(ch_frame, text="Offset:").grid(row=4, column=0, sticky=tk.W)
        self.ch1_offset = ttk.Entry(ch_frame, width=10)
        self.ch1_offset.insert(0, "0")
        self.ch1_offset.grid(row=4, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(ch_frame, text="Set", 
                  command=lambda: self.set_channel_offset(1, self.ch1_offset.get())).grid(row=4, column=2, padx=2)
        
        ttk.Separator(ch_frame, orient='horizontal').grid(row=5, column=0, columnspan=3, 
                                                          sticky=tk.W+tk.E, pady=5)
        
        # Channel 2
        ttk.Label(ch_frame, text="CH2:", font=('Arial', 10, 'bold')).grid(row=6, column=0, sticky=tk.W)
        
        self.ch2_display = tk.BooleanVar(value=False)
        ttk.Checkbutton(ch_frame, text="Display", variable=self.ch2_display,
                       command=lambda: self.set_channel_display(2, self.ch2_display.get())).grid(row=6, column=1, sticky=tk.W)
        
        ttk.Label(ch_frame, text="Scale:").grid(row=7, column=0, sticky=tk.W)
        self.ch2_scale = ttk.Combobox(ch_frame, values=self.scope.VOLTAGE_SCALES, 
                                      width=10, state='readonly')
        self.ch2_scale.set("1v")
        self.ch2_scale.grid(row=7, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(ch_frame, text="Set", 
                  command=lambda: self.set_channel_scale(2, self.ch2_scale.get())).grid(row=7, column=2, padx=2)
        
        ttk.Label(ch_frame, text="Coupling:").grid(row=8, column=0, sticky=tk.W)
        self.ch2_coupling = ttk.Combobox(ch_frame, values=self.scope.COUPLING_MODES, 
                                         width=10, state='readonly')
        self.ch2_coupling.set("DC")
        self.ch2_coupling.grid(row=8, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(ch_frame, text="Set", 
                  command=lambda: self.set_channel_coupling(2, self.ch2_coupling.get())).grid(row=8, column=2, padx=2)
        
        ttk.Label(ch_frame, text="Probe:").grid(row=9, column=0, sticky=tk.W)
        self.ch2_probe = ttk.Combobox(ch_frame, values=self.scope.PROBE_ATTEN, 
                                      width=10, state='readonly')
        self.ch2_probe.set("X10")
        self.ch2_probe.grid(row=9, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(ch_frame, text="Set", 
                  command=lambda: self.set_channel_probe(2, self.ch2_probe.get())).grid(row=9, column=2, padx=2)
        
        ttk.Label(ch_frame, text="Offset:").grid(row=10, column=0, sticky=tk.W)
        self.ch2_offset = ttk.Entry(ch_frame, width=10)
        self.ch2_offset.insert(0, "0")
        self.ch2_offset.grid(row=10, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(ch_frame, text="Set", 
                  command=lambda: self.set_channel_offset(2, self.ch2_offset.get())).grid(row=10, column=2, padx=2)
    
    def setup_timebase_controls(self, parent):
        """Setup timebase control panel"""
        time_frame = ttk.LabelFrame(parent, text="⏱️ Timebase", padding=10)
        time_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(time_frame, text="Scale:").grid(row=0, column=0, sticky=tk.W)
        self.timebase_scale = ttk.Combobox(time_frame, values=self.scope.TIMEBASE_SCALES, 
                                           width=10, state='readonly')
        self.timebase_scale.set("1ms")
        self.timebase_scale.grid(row=0, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(time_frame, text="Set", 
                  command=self.set_timebase).grid(row=0, column=2, padx=2)
        
        ttk.Label(time_frame, text="Offset (pix):").grid(row=1, column=0, sticky=tk.W)
        self.timebase_offset = ttk.Entry(time_frame, width=10)
        self.timebase_offset.insert(0, "0")
        self.timebase_offset.grid(row=1, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(time_frame, text="Set", 
                  command=self.set_timebase_offset).grid(row=1, column=2, padx=2)
    
    def setup_trigger_controls(self, parent):
        """Setup trigger control panel"""
        trig_frame = ttk.LabelFrame(parent, text="⚡ Trigger", padding=10)
        trig_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(trig_frame, text="Mode:").grid(row=0, column=0, sticky=tk.W)
        self.trigger_mode = ttk.Combobox(trig_frame, values=self.scope.TRIGGER_MODES, 
                                         width=10, state='readonly')
        self.trigger_mode.set("AUTO")
        self.trigger_mode.grid(row=0, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(trig_frame, text="Set", 
                  command=self.set_trigger_mode).grid(row=0, column=2, padx=2)
        
        ttk.Label(trig_frame, text="Source:").grid(row=1, column=0, sticky=tk.W)
        self.trigger_source = ttk.Combobox(trig_frame, values=self.scope.TRIGGER_SOURCES, 
                                           width=10, state='readonly')
        self.trigger_source.set("CH1")
        self.trigger_source.grid(row=1, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(trig_frame, text="Set", 
                  command=self.set_trigger_source).grid(row=1, column=2, padx=2)
        
        ttk.Label(trig_frame, text="Slope:").grid(row=2, column=0, sticky=tk.W)
        self.trigger_slope = ttk.Combobox(trig_frame, values=self.scope.TRIGGER_SLOPES, 
                                          width=10, state='readonly')
        self.trigger_slope.set("RISE")
        self.trigger_slope.grid(row=2, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(trig_frame, text="Set", 
                  command=self.set_trigger_slope).grid(row=2, column=2, padx=2)
        
        ttk.Label(trig_frame, text="Level (pix):").grid(row=3, column=0, sticky=tk.W)
        self.trigger_level = ttk.Entry(trig_frame, width=10)
        self.trigger_level.insert(0, "0")
        self.trigger_level.grid(row=3, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(trig_frame, text="Set", 
                  command=self.set_trigger_level).grid(row=3, column=2, padx=2)
    
    def setup_acquisition_controls(self, parent):
        """Setup acquisition control panel"""
        acq_frame = ttk.LabelFrame(parent, text="🎯 Acquisition", padding=10)
        acq_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(acq_frame, text="Type:").grid(row=0, column=0, sticky=tk.W)
        self.acq_type = ttk.Combobox(acq_frame, values=self.scope.ACQ_TYPES, 
                                     width=10, state='readonly')
        self.acq_type.set("SAMPle")
        self.acq_type.grid(row=0, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(acq_frame, text="Set", 
                  command=self.set_acquire_type).grid(row=0, column=2, padx=2)
        
        ttk.Label(acq_frame, text="Averages:").grid(row=1, column=0, sticky=tk.W)
        self.acq_average = ttk.Combobox(acq_frame, values=self.scope.AVG_COUNTS, 
                                        width=10, state='readonly')
        self.acq_average.set("4")
        self.acq_average.grid(row=1, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(acq_frame, text="Set", 
                  command=self.set_acquire_average).grid(row=1, column=2, padx=2)
        
        ttk.Label(acq_frame, text="Memory:").grid(row=2, column=0, sticky=tk.W)
        self.mem_depth = ttk.Combobox(acq_frame, values=self.scope.MEMORY_DEPTHS, 
                                      width=10, state='readonly')
        self.mem_depth.set("10K")
        self.mem_depth.grid(row=2, column=1, sticky=tk.W+tk.E, padx=2)
        ttk.Button(acq_frame, text="Set", 
                  command=self.set_memory_depth).grid(row=2, column=2, padx=2)
    
    def setup_measurement_controls(self, parent):
        """Setup measurement control panel"""
        meas_frame = ttk.LabelFrame(parent, text="📏 Measurements", padding=10)
        meas_frame.pack(fill=tk.X, pady=5)
        
        ttk.Label(meas_frame, text="Source:").grid(row=0, column=0, sticky=tk.W)
        self.meas_source = ttk.Combobox(meas_frame, values=["CH1", "CH2"], 
                                        width=10, state='readonly')
        self.meas_source.set("CH1")
        self.meas_source.grid(row=0, column=1, columnspan=2, sticky=tk.W+tk.E, padx=2)
        
        ttk.Button(meas_frame, text="Get All Measurements", 
                  command=self.get_measurements).grid(row=2, column=0, columnspan=3, 
                                                      sticky=tk.W+tk.E, pady=5)
        
        ttk.Button(meas_frame, text="Download Waveform", 
                  command=self.download_waveform).grid(row=1, column=0, columnspan=3, 
                                                       sticky=tk.W+tk.E, pady=2)
        
        self.auto_refresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(meas_frame, text="Auto Refresh (2s)", 
                       variable=self.auto_refresh_var,
                       command=self.toggle_auto_refresh).grid(row=3, column=0, 
                                                              columnspan=3, pady=5)
        
        ttk.Button(meas_frame, text="Reset Scope", 
                  command=self.reset_scope).grid(row=4, column=0, columnspan=3, 
                                                 sticky=tk.W+tk.E, pady=2)
        
        ttk.Button(meas_frame, text="Save Waveform", 
                  command=self.save_waveform).grid(row=5, column=0, columnspan=3, 
                                                   sticky=tk.W+tk.E, pady=2)
    
    def setup_graph_panel(self, parent):
        """Setup waveform graph panel"""
        graph_frame = ttk.LabelFrame(parent, text="📈 Waveform Display", padding=10)
        graph_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        # Configure matplotlib style
        self.ax.set_facecolor('#1e1e1e')
        self.ax.tick_params(colors='white', which='both')
        self.ax.spines['bottom'].set_color('white')
        self.ax.spines['top'].set_color('white')
        self.ax.spines['left'].set_color('white')
        self.ax.spines['right'].set_color('white')
        self.ax.set_xlabel("Time (ms)", color='white', fontsize=12)
        self.ax.set_ylabel("Voltage (V)", color='white', fontsize=12)
        self.ax.set_title("Oscilloscope Waveform", color='white', fontsize=14, fontweight='bold')
        self.ax.grid(True, alpha=0.3, color='gray', linestyle='--')
        
        # Create canvas
        self.canvas = FigureCanvasTkAgg(self.fig, master=graph_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        
        # Toolbar
        toolbar_frame = ttk.Frame(graph_frame)
        toolbar_frame.pack(fill=tk.X, pady=5)
        
        ttk.Button(toolbar_frame, text="Plot Waveform", 
                  command=self.plot_waveform).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar_frame, text="Clear Plot", 
                  command=self.clear_plot).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar_frame, text="Zoom In", 
                  command=self.zoom_in).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar_frame, text="Zoom Out", 
                  command=self.zoom_out).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar_frame, text="Auto Scale", 
                  command=self.auto_scale).pack(side=tk.LEFT, padx=2)
    
    def setup_measurement_display(self, parent):
        """Setup measurement display panel"""
        display_frame = ttk.LabelFrame(parent, text="📊 Measurement Results", padding=10)
        display_frame.pack(fill=tk.X, pady=5)
        
        # Create notebook for different views
        notebook = ttk.Notebook(display_frame)
        notebook.pack(fill=tk.BOTH, expand=True)
        
        # Measurements tab
        meas_tab = ttk.Frame(notebook)
        notebook.add(meas_tab, text="Measurements")
        
        # Add scrollbar
        meas_scroll = ttk.Scrollbar(meas_tab)
        meas_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.measurement_text = tk.Text(meas_tab, height=8, width=100, 
                                       yscrollcommand=meas_scroll.set,
                                       font=('Consolas', 10), bg='#1e1e1e', 
                                       fg='#00ff00', insertbackground='white')
        self.measurement_text.pack(fill=tk.BOTH, expand=True)
        meas_scroll.config(command=self.measurement_text.yview)
        
        # Console tab
        console_tab = ttk.Frame(notebook)
        notebook.add(console_tab, text="Console")
        
        console_scroll = ttk.Scrollbar(console_tab)
        console_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.console_text = tk.Text(console_tab, height=8, width=100, 
                                   yscrollcommand=console_scroll.set,
                                   font=('Consolas', 9), bg='#000000', 
                                   fg='#00ff00', insertbackground='white')
        self.console_text.pack(fill=tk.BOTH, expand=True)
        console_scroll.config(command=self.console_text.yview)
        
        # Log tab
        log_tab = ttk.Frame(notebook)
        notebook.add(log_tab, text="Log")
        
        log_scroll = ttk.Scrollbar(log_tab)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.log_text = tk.Text(log_tab, height=8, width=100, 
                               yscrollcommand=log_scroll.set,
                               font=('Consolas', 9), bg='#1e1e1e', 
                               fg='#ffffff', insertbackground='white')
        self.log_text.pack(fill=tk.BOTH, expand=True)
        log_scroll.config(command=self.log_text.yview)
    
    def setup_status_bar(self):
        """Setup status bar"""
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM)
        
        self.status_var = tk.StringVar(value="Ready | Disconnected")
        status_label = ttk.Label(status_frame, textvariable=self.status_var, 
                                relief=tk.SUNKEN, anchor=tk.W)
        status_label.pack(fill=tk.X, side=tk.LEFT, expand=True)
        
        # Add timestamp
        self.time_var = tk.StringVar()
        time_label = ttk.Label(status_frame, textvariable=self.time_var, 
                              relief=tk.SUNKEN, width=20)
        time_label.pack(side=tk.RIGHT)
        self.update_time()
    
    def update_time(self):
        """Update timestamp"""
        self.time_var.set(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        self.root.after(1000, self.update_time)
    
    def log(self, message, level="INFO"):
        """Log message to console and log tab"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_msg = f"[{timestamp}] [{level}] {message}\n"
        
        self.console_text.insert(tk.END, log_msg)
        self.console_text.see(tk.END)
        
        self.log_text.insert(tk.END, log_msg)
        self.log_text.see(tk.END)
    
    # Connection methods
    def toggle_connection(self):
        """Toggle oscilloscope connection"""
        if not self.scope.is_connected:
            self.connect_scope()
        else:
            self.disconnect_scope()
    
    def connect_scope(self):
        """Connect to oscilloscope"""
        address = self.conn_address.get()
        
        self.log(f"Connecting to oscilloscope via {self.conn_type.get().upper()}...")
        
        if self.conn_type.get() == "usb":
            success = self.scope.connect_usb(address)
        else:
            success = self.scope.connect_lan(address)
        
        if success:
            self.connect_btn.config(text="Disconnect")
            self.status_var.set(f"Connected via {self.conn_type.get().upper()} | {address}")
            self.log("Connected successfully!", "SUCCESS")
            
            # Get device info
            idn = self.scope.get_idn()
            if idn:
                self.device_info.set(f"Device: {idn}")
                self.log(f"Device ID: {idn}")
            
            # Query initial states
            self.query_all_states()
        else:
            messagebox.showerror("Connection Error", "Failed to connect to oscilloscope")
            self.log("Connection failed!", "ERROR")
    
    def disconnect_scope(self):
        """Disconnect from oscilloscope"""
        self.scope.disconnect()
        self.connect_btn.config(text="Connect")
        self.status_var.set("Ready | Disconnected")
        self.device_info.set("Not Connected")
        self.log("Disconnected from oscilloscope")
    
    def query_all_states(self):
        """Query all current oscilloscope states and update GUI option states"""
        try:
            # Query timebase
            tb_scale = self.scope.get_timebase_scale()
            if tb_scale:
                self.log(f"Current timebase scale: {tb_scale}")
                if tb_scale in self.scope.TIMEBASE_SCALES:
                    self.timebase_scale.set(tb_scale)
            # Query channels
            for ch in [1, 2]:
                ch_scale = self.scope.get_channel_scale(ch)
                ch_coupling = self.scope.get_channel_coupling(ch)
                ch_display = self.scope.get_channel_display(ch)
                ch_offset = self.scope.get_channel_offset(ch)
                if ch_scale:
                    self.log(f"CH{ch} scale: {ch_scale}")
                    if ch == 1 and ch_scale in self.scope.VOLTAGE_SCALES:
                        self.ch1_scale.set(ch_scale)
                    elif ch == 2 and ch_scale in self.scope.VOLTAGE_SCALES:
                        self.ch2_scale.set(ch_scale)
                if ch_coupling:
                    self.log(f"CH{ch} coupling: {ch_coupling}")
                    if ch == 1 and ch_coupling in self.scope.COUPLING_MODES:
                        self.ch1_coupling.set(ch_coupling)
                    elif ch == 2 and ch_coupling in self.scope.COUPLING_MODES:
                        self.ch2_coupling.set(ch_coupling)
                if ch_display:
                    display_state = (str(ch_display).strip().upper() == "ON")
                    if ch == 1:
                        self.ch1_display.set(display_state)
                    elif ch == 2:
                        self.ch2_display.set(display_state)
                if ch_offset is not None:
                    try:
                        offset_val = float(ch_offset)
                    except Exception:
                        offset_val = ch_offset
                    if ch == 1:
                        self.ch1_offset.delete(0, tk.END)
                        self.ch1_offset.insert(0, str(offset_val))
                    elif ch == 2:
                        self.ch2_offset.delete(0, tk.END)
                        self.ch2_offset.insert(0, str(offset_val))
        except Exception as e:
            self.log(f"Error querying states: {e}", "ERROR")
    
    # Channel control methods
    def set_channel_display(self, channel, state):
        """Set channel display state"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        state_str = "ON" if state else "OFF"
        self.scope.set_channel_display(channel, state_str)
        self.log(f"CH{channel} display: {state_str}")
    
    def set_channel_scale(self, channel, scale):
        """Set channel vertical scale"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        self.scope.set_channel_scale(channel, scale)
        self.log(f"CH{channel} scale set to: {scale}")
    
    def set_channel_coupling(self, channel, coupling):
        """Set channel coupling"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        self.scope.set_channel_coupling(channel, coupling)
        self.log(f"CH{channel} coupling set to: {coupling}")
    
    def set_channel_probe(self, channel, probe):
        """Set probe attenuation"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        self.scope.set_channel_probe(channel, probe)
        self.log(f"CH{channel} probe set to: {probe}")
    
    def set_channel_offset(self, channel, offset):
        """Set channel vertical offset"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        try:
            offset_val = float(offset)
            self.scope.set_channel_offset(channel, offset_val)
            self.log(f"CH{channel} offset set to: {offset_val}")
        except ValueError:
            messagebox.showerror("Invalid Input", "Offset must be a number")
    
    # Timebase control methods
    def set_timebase(self):
        """Set timebase scale"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        scale = self.timebase_scale.get()
        self.scope.set_timebase_scale(scale)
        self.log(f"Timebase scale set to: {scale}")
    
    def set_timebase_offset(self):
        """Set timebase offset"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        try:
            offset = int(self.timebase_offset.get())
            self.scope.set_timebase_offset(offset)
            self.log(f"Timebase offset set to: {offset} pixels")
        except ValueError:
            messagebox.showerror("Invalid Input", "Offset must be an integer")
    
    # Trigger control methods
    def set_trigger_mode(self):
        """Set trigger mode"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        mode = self.trigger_mode.get()
        self.scope.set_trigger_mode(mode)
        self.log(f"Trigger mode set to: {mode}")
    
    def set_trigger_source(self):
        """Set trigger source"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        source = self.trigger_source.get()
        self.scope.set_edge_trigger_source(source)
        self.log(f"Trigger source set to: {source}")
    
    def set_trigger_slope(self):
        """Set trigger slope"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        slope = self.trigger_slope.get()
        self.scope.set_edge_trigger_slope(slope)
        self.log(f"Trigger slope set to: {slope}")
    
    def set_trigger_level(self):
        """Set trigger level"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        try:
            level = int(self.trigger_level.get())
            self.scope.set_edge_trigger_level(level)
            self.log(f"Trigger level set to: {level} pixels")
        except ValueError:
            messagebox.showerror("Invalid Input", "Level must be an integer")
    
    # Acquisition control methods
    def set_acquire_type(self):
        """Set acquisition type"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        acq_type = self.acq_type.get()
        self.scope.set_acquire_type(acq_type)
        self.log(f"Acquisition type set to: {acq_type}")
    
    def set_acquire_average(self):
        """Set acquisition average count"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        avg = self.acq_average.get()
        self.scope.set_acquire_average(avg)
        self.log(f"Acquisition average set to: {avg}")
    
    def set_memory_depth(self):
        """Set memory depth"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        depth = self.mem_depth.get()
        self.scope.set_memory_depth(depth)
        self.log(f"Memory depth set to: {depth}")
    
    # Measurement methods
    def get_measurements(self):
        """Get all measurements"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        channel = int(self.meas_source.get().replace("CH", ""))
        self.log(f"Getting measurements for CH{channel}...")
        
        self.measurement_text.delete(1.0, tk.END)
        self.measurement_text.insert(tk.END, f"=== Measurements for CH{channel} ===\n\n")
        
        measurements = self.scope.get_all_measurements(channel)
        
        if measurements:
            for param, value in measurements.items():
                self.measurement_text.insert(tk.END, f"{param:15s}: {value}\n")
            self.log(f"Retrieved {len(measurements)} measurements")
        else:
            self.measurement_text.insert(tk.END, "No measurements available\n")
            self.log("No measurements retrieved", "WARNING")
    
    # Waveform methods
    def download_waveform(self):
        """Download waveform data"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        self.log("Downloading waveform data...")
        self.status_var.set("Downloading waveform...")
        self.root.update()
        
        success = self.scope.download_waveform_data()
        
        if success:
            self.log("Waveform downloaded successfully!", "SUCCESS")
            self.status_var.set(f"Connected | Waveform downloaded")
            
            # Display channel info
            for channel in self.scope.waveform_data.channels:
                info = (f"Channel {channel['name']}: {channel['num_points']} points, "
                       f"Voltage/point: {channel['voltage_per_point']:.3f} mV")
                self.measurement_text.insert(tk.END, f"\n{info}\n")
                self.log(info)
            
            # Auto-plot if enabled
            if self.auto_refresh_var.get():
                self.plot_waveform()
        else:
            self.log("Failed to download waveform data", "ERROR")
            messagebox.showerror("Download Error", "Failed to download waveform data")
    
    def plot_waveform(self):
        """Plot downloaded waveform"""
        if not self.scope.waveform_data.channels:
            messagebox.showwarning("No Data", "No waveform data available. Download first.")
            return
        
        self.ax.clear()
        plotted = False
        colors = ['#00ff00', '#ff6600', '#00ccff', '#ff00ff']
        
        for idx, channel in enumerate(self.scope.waveform_data.channels):
            if channel.get('waveform_data') and len(channel['waveform_data']) > 0:
                try:
                    # Convert to voltage
                    gain = 1.0
                    if channel['attenuation'] == 1:
                        gain = 10.0
                    voltage_data = np.array(channel['waveform_data']) * (channel['voltage_per_point'] * gain / 1000.0)
                    
                    # Calculate time axis
                    if channel['point_interval'] > 0:
                        time_data = np.arange(len(voltage_data)) * channel['point_interval'] * 1e-6
                    else:
                        time_data = np.arange(len(voltage_data))
                    
                    # Plot with appropriate color
                    color = colors[idx % len(colors)]
                    self.ax.plot(time_data * 1000, voltage_data, 
                               label=f"Channel {channel['name']}", 
                               linewidth=1.5, color=color, alpha=0.9)
                    plotted = True
                    
                    self.log(f"Plotted {len(voltage_data)} points for {channel['name']}")
                    
                except Exception as e:
                    self.log(f"Error plotting channel {channel['name']}: {e}", "ERROR")
        
        if plotted:
            self.ax.set_title("Oscilloscope Waveform", color='white', 
                            fontsize=14, fontweight='bold')
            self.ax.set_xlabel("Time (ms)", color='white', fontsize=12)
            self.ax.set_ylabel("Voltage (V)", color='white', fontsize=12)
            self.ax.grid(True, alpha=0.3, color='gray', linestyle='--')
            self.ax.legend(loc='upper right', facecolor='#2b2b2b', 
                         edgecolor='white', labelcolor='white')
            self.canvas.draw()
            self.log("Waveform plotted successfully")
        else:
            self.log("No valid waveform data to plot", "WARNING")
    
    def clear_plot(self):
        """Clear the plot"""
        self.ax.clear()
        self.ax.set_facecolor('#1e1e1e')
        self.ax.set_title("Oscilloscope Waveform", color='white', 
                        fontsize=14, fontweight='bold')
        self.ax.set_xlabel("Time (ms)", color='white', fontsize=12)
        self.ax.set_ylabel("Voltage (V)", color='white', fontsize=12)
        self.ax.grid(True, alpha=0.3, color='gray', linestyle='--')
        self.canvas.draw()
        self.log("Plot cleared")
    
    def zoom_in(self):
        """Zoom in on plot"""
        xlim = self.ax.get_xlim()
        ylim = self.ax.get_ylim()
        
        x_range = xlim[1] - xlim[0]
        y_range = ylim[1] - ylim[0]
        
        self.ax.set_xlim(xlim[0] + x_range * 0.1, xlim[1] - x_range * 0.1)
        self.ax.set_ylim(ylim[0] + y_range * 0.1, ylim[1] - y_range * 0.1)
        self.canvas.draw()
        self.log("Zoomed in")
    
    def zoom_out(self):
        """Zoom out on plot"""
        xlim = self.ax.get_xlim()
        ylim = self.ax.get_ylim()
        
        x_range = xlim[1] - xlim[0]
        y_range = ylim[1] - ylim[0]
        
        self.ax.set_xlim(xlim[0] - x_range * 0.1, xlim[1] + x_range * 0.1)
        self.ax.set_ylim(ylim[0] - y_range * 0.1, ylim[1] + y_range * 0.1)
        self.canvas.draw()
        self.log("Zoomed out")
    
    def auto_scale(self):
        """Auto-scale the plot"""
        self.ax.relim()
        self.ax.autoscale_view()
        self.canvas.draw()
        self.log("Auto-scaled plot")
    
    def save_waveform(self):
        """Save waveform data to file"""
        if not self.scope.waveform_data.channels:
            messagebox.showwarning("No Data", "No waveform data to save")
            return
        
        filename = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("JSON files", "*.json"), 
                      ("All files", "*.*")]
        )
        
        if not filename:
            return
        
        try:
            if filename.endswith('.json'):
                # Save as JSON
                data_to_save = []
                for channel in self.scope.waveform_data.channels:
                    channel_data = {
                        'name': channel['name'],
                        'num_points': channel['num_points'],
                        'voltage_per_point': channel['voltage_per_point'],
                        'point_interval': channel['point_interval'],
                        'waveform_data': channel['waveform_data']
                    }
                    data_to_save.append(channel_data)
                
                with open(filename, 'w') as f:
                    json.dump(data_to_save, f, indent=2)
            else:
                # Save as CSV
                with open(filename, 'w') as f:
                    # Write headers
                    f.write("Time (ms)")
                    for channel in self.scope.waveform_data.channels:
                        f.write(f",{channel['name']} (V)")
                    f.write("\n")
                    
                    # Write data
                    max_points = max(len(ch['waveform_data']) 
                                   for ch in self.scope.waveform_data.channels 
                                   if ch.get('waveform_data'))
                    
                    for i in range(max_points):
                        # Time column
                        if self.scope.waveform_data.channels[0]['point_interval'] > 0:
                            time_val = i * self.scope.waveform_data.channels[0]['point_interval'] * 1e-3
                        else:
                            time_val = i
                        f.write(f"{time_val:.6f}")
                        
                        # Voltage columns
                        for channel in self.scope.waveform_data.channels:
                            if i < len(channel['waveform_data']):
                                voltage = channel['waveform_data'][i] * (channel['voltage_per_point'] / 1000.0)
                                f.write(f",{voltage:.6f}")
                            else:
                                f.write(",")
                        f.write("\n")
            
            self.log(f"Waveform saved to: {filename}", "SUCCESS")
            messagebox.showinfo("Success", f"Waveform saved to:\n{filename}")
            
        except Exception as e:
            self.log(f"Error saving waveform: {e}", "ERROR")
            messagebox.showerror("Save Error", f"Failed to save waveform:\n{e}")
    
    def reset_scope(self):
        """Reset oscilloscope to default settings"""
        if not self.scope.is_connected:
            messagebox.showwarning("Not Connected", "Please connect first")
            return
        
        result = messagebox.askyesno("Confirm Reset", 
                                     "Reset oscilloscope to default settings?")
        if result:
            self.scope.reset()
            self.log("Oscilloscope reset to defaults", "WARNING")
            time.sleep(1)
            self.query_all_states()
    
    def toggle_auto_refresh(self):
        """Toggle auto-refresh mode"""
        if self.auto_refresh_var.get():
            self.auto_refresh = True
            self.refresh_thread = threading.Thread(target=self.auto_refresh_loop, daemon=True)
            self.refresh_thread.start()
            self.log("Auto-refresh enabled (2 second interval)")
        else:
            self.auto_refresh = False
            self.log("Auto-refresh disabled")
    
    def auto_refresh_loop(self):
        """Auto-refresh loop for continuous monitoring"""
        while self.auto_refresh and self.scope.is_connected:
            try:
                # Download and plot waveform
                success = self.scope.download_waveform_data()
                if success:
                    self.root.after(0, self.plot_waveform)
                
                time.sleep(2)
            except Exception as e:
                self.log(f"Auto-refresh error: {e}", "ERROR")
                break


def main():
    """Main application entry point"""
    root = tk.Tk()
    
    # Set window icon and style
    root.configure(bg='#2b2b2b')
    
    app = App(root)
    
    # Center window on screen
    root.update_idletasks()
    width = root.winfo_width()
    height = root.winfo_height()
    x = (root.winfo_screenwidth() // 2) - (width // 2)
    y = (root.winfo_screenheight() // 2) - (height // 2)
    root.geometry(f'{width}x{height}+{x}+{y}')
    
    root.mainloop()


if __name__ == "__main__":
    main()