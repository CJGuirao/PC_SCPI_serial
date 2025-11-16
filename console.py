import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import socket
import serial
import threading
import time
import logging
from datetime import datetime

class OWONSCPIConsole:
    def __init__(self, root):
        self.root = root
        self.root.title("OWON SCPI Console")
        self.root.geometry("800x600")
        
        self.connection = None
        self.connection_type = None
        self.is_connected = False
        self.command_history = []  # Store command history
        self.history_index = -1   # Current position in history
        
        self.setup_logging()
        self.create_widgets()
        
    def setup_logging(self):
        """Setup logging configuration"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler('scpi_console.log')
            ]
        )
        
    def create_widgets(self):
        """Create the GUI widgets"""
        # Main frame
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Connection frame
        conn_frame = ttk.LabelFrame(main_frame, text="Connection Settings", padding="10")
        conn_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Connection type
        ttk.Label(conn_frame, text="Connection Type:").grid(row=0, column=0, sticky=tk.W, padx=(0, 10))
        self.conn_type = tk.StringVar(value="lan")
        ttk.Radiobutton(conn_frame, text="LAN", variable=self.conn_type, value="lan").grid(row=0, column=1, sticky=tk.W)
        ttk.Radiobutton(conn_frame, text="USB", variable=self.conn_type, value="usb").grid(row=0, column=2, sticky=tk.W)
        
        # Connection details
        ttk.Label(conn_frame, text="IP Address / COM Port:").grid(row=1, column=0, sticky=tk.W, padx=(0, 10), pady=(10, 0))
        self.conn_address = ttk.Entry(conn_frame, width=20)
        self.conn_address.insert(0, "10.1.1.131")
        self.conn_address.grid(row=1, column=1, sticky=tk.W, pady=(10, 0))
        
        ttk.Label(conn_frame, text="Port / Baud Rate:").grid(row=1, column=2, sticky=tk.W, padx=(20, 10), pady=(10, 0))
        self.conn_port = ttk.Entry(conn_frame, width=10)
        self.conn_port.insert(0, "3000")
        self.conn_port.grid(row=1, column=3, sticky=tk.W, pady=(10, 0))
        
        # Connection buttons
        self.connect_btn = ttk.Button(conn_frame, text="Connect", command=self.toggle_connection)
        self.connect_btn.grid(row=1, column=4, padx=(20, 0), pady=(10, 0))
        
        # SCPI Mode button
        self.scpi_btn = ttk.Button(conn_frame, text="Enable SCPI Mode", command=self.enable_scpi_mode)
        self.scpi_btn.grid(row=1, column=5, padx=(10, 0), pady=(10, 0))
        
        # Status
        self.status_var = tk.StringVar(value="Disconnected")
        ttk.Label(conn_frame, textvariable=self.status_var).grid(row=2, column=0, columnspan=6, sticky=tk.W, pady=(10, 0))
        
        # Command frame
        cmd_frame = ttk.LabelFrame(main_frame, text="SCPI Commands", padding="10")
        cmd_frame.pack(fill=tk.BOTH, expand=True)
        
        # Quick commands
        quick_frame = ttk.Frame(cmd_frame)
        quick_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(quick_frame, text="Quick Commands:").pack(side=tk.LEFT)
        
        quick_commands = [
            ("*IDN?", "Identify"),
            ("*CLS", "Clear events"),
            ("*ESR?", "Error register"),
            (":MEASure:FREQuency?", "Freq"),
            (":MEASure:PKPK?", "Vpp"),
        ]
        
        for cmd, label in quick_commands:
            ttk.Button(quick_frame, text=label, 
                      command=lambda c=cmd: self.send_scpi_command(c)).pack(side=tk.LEFT, padx=(10, 0))
        
        # Command input
        input_frame = ttk.Frame(cmd_frame)
        input_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(input_frame, text="SCPI Command:").pack(side=tk.LEFT)
        self.cmd_entry = ttk.Entry(input_frame, width=50)
        self.cmd_entry.pack(side=tk.LEFT, padx=(10, 10), fill=tk.X, expand=True)
        self.cmd_entry.bind('<Return>', lambda e: self.send_scpi_command())
        # Bind arrow keys for history navigation
        self.cmd_entry.bind('<Up>', self.history_previous)
        self.cmd_entry.bind('<Down>', self.history_next)
        
        ttk.Button(input_frame, text="Send", command=self.send_scpi_command).pack(side=tk.LEFT)
        ttk.Button(input_frame, text="Clear", command=self.clear_output).pack(side=tk.LEFT, padx=(10, 0))
        
        # Output area
        output_frame = ttk.Frame(cmd_frame)
        output_frame.pack(fill=tk.BOTH, expand=True)
        
        self.output_text = scrolledtext.ScrolledText(output_frame, height=20, width=80)
        self.output_text.pack(fill=tk.BOTH, expand=True)
        
        # Add some common SCPI commands reference
        self.add_scpi_reference()
        
    def add_scpi_reference(self):
        """Add SCPI command reference to output"""
        reference = """
Common SCPI Commands:
*IDN?      - Identify instrument
*RST       - Reset to defaults
*CLS       - Clear status

Measurement Commands:
:MEASure:FREQuency?    - Measure frequency
:MEASure:PERiod?       - Measure period  
:MEASure:PKPK?         - Measure peak-to-peak voltage
:MEASure:MAX?          - Measure maximum voltage
:MEASure:MIN?          - Measure minimum voltage
:MEASure:AVERage?      - Measure average voltage

Channel Commands:
:CHANnel1:DISPlay ON|OFF   - Turn channel 1 on/off
:CHANnel1:SCALE <value>    - Set vertical scale (e.g., 1v, 2v, 5v)
:CHANnel1:OFFSet <value>   - Set vertical offset
:CHANnel1:COUPling DC|AC|GND - Set coupling

Timebase Commands:
:TIMebase:SCALE <value>    - Set horizontal scale (e.g., 1ms, 2ms, 5ms)
:TIMebase:HOFFset <value>  - Set horizontal offset

Trigger Commands:
:TRIGger:MODE AUTO|NORMal  - Set trigger mode
:TRIGger:TYPE EDGE|VIDeo   - Set trigger type
:TRIGger:LEVel <value>     - Set trigger level

Acquisition Commands:
:ACQuire:TYPE SAMPle|AVERage|PEAK - Set acquisition type
:ACQuire:AVERage <count>   - Set average count (4,16,64,128)

Type commands without colon for root level commands.
Use '?' at end for queries.
"""
        self.output_text.insert(tk.END, reference)
        self.output_text.see(tk.END)
        
    def toggle_connection(self):
        """Toggle connection to oscilloscope"""
        if not self.is_connected:
            self.connect_scope()
        else:
            self.disconnect_scope()
            
    def connect_scope(self):
        """Connect to oscilloscope"""
        address = self.conn_address.get().strip()
        port_baud = self.conn_port.get().strip()
        
        if not address:
            messagebox.showerror("Error", "Please enter IP address or COM port")
            return
            
        try:
            if self.conn_type.get() == "lan":
                port = int(port_baud) if port_baud else 3000
                success = self.connect_lan(address, port)
            else:
                baudrate = int(port_baud) if port_baud else 115200
                success = self.connect_usb(address, baudrate)
                
            if success:
                self.connect_btn.config(text="Disconnect")
                self.status_var.set(f"Connected via {self.conn_type.get().upper()}")
                self.log_message(f"Connected to {address}")
                
                # Test connection with IDN query
                self.send_scpi_command("*IDN?")
            else:
                messagebox.showerror("Connection Error", f"Failed to connect to {address}")
                
        except ValueError:
            messagebox.showerror("Error", "Invalid port or baud rate")
        except Exception as e:
            messagebox.showerror("Error", f"Connection failed: {str(e)}")
            
    def connect_lan(self, ip, port=3000):
        """Connect via LAN"""
        try:
            self.connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.connection.settimeout(5)
            self.connection.connect((ip, port))
            self.connection.settimeout(3)  # Set shorter timeout for normal operations
            
            # Clear any pending data and enter SCPI mode
            # self.send_raw_command("KLS")
            
            self.connection_type = 'lan'
            self.is_connected = True
            return True
            
        except Exception as e:
            self.log_message(f"LAN connection error: {str(e)}")
            if self.connection:
                self.connection.close()
                self.connection = None
            return False
            
    def connect_usb(self, com_port, baudrate=115200):
        """Connect via USB"""
        try:
            self.connection = serial.Serial(
                port=com_port,
                baudrate=baudrate,
                timeout=2,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE
            )
            
            self.connection_type = 'usb'
            self.is_connected = True
            return True
                
        except Exception as e:
            self.log_message(f"USB connection error: {str(e)}")
            if self.connection:
                self.connection.close()
                self.connection = None
            return False
            
    def enable_scpi_mode(self):
        """Enable SCPI mode on the oscilloscope"""
        if not self.is_connected:
            messagebox.showwarning("Not Connected", "Please connect to oscilloscope first")
            return
            
        self.log_message("> :SDSLSCPI# (Enable SCPI Mode)")
        
        # Send SCPI mode command in thread to avoid blocking GUI
        thread = threading.Thread(target=self._enable_scpi_thread)
        thread.daemon = True
        thread.start()
        
    def _enable_scpi_thread(self):
        """Thread for enabling SCPI mode"""
        try:
            if self.connection_type == 'lan':
                self.send_raw_command(":SDSLSCPI#")
                response = self.read_response(timeout=2)
                if response is not None:
                    self.root.after(0, self.log_message, f"< {response}")
                else:
                    self.root.after(0, self.log_message, "< No response to SCPI mode command")
            else:
                self.send_raw_command(":SDSLSCPIH")
                response = self.read_response(timeout=2)
                if response is not None:
                    self.root.after(0, self.log_message, f"< {response}")
                else:
                    self.root.after(0, self.log_message, "< No response to SCPI mode command")
                    
        except Exception as e:
            self.root.after(0, self.log_message, f"< Error enabling SCPI mode: {str(e)}")
            
    def disconnect_scope(self):
        """Disconnect from oscilloscope"""
        if self.connection:
            try:
                if self.connection_type == 'usb':
                    self.connection.close()
                else:
                    self.connection.close()
            except:
                pass
                
        self.connection = None
        self.is_connected = False
        self.connection_type = None
        self.connect_btn.config(text="Connect")
        self.status_var.set("Disconnected")
        self.log_message("Disconnected from oscilloscope")
        
    def send_scpi_command(self, command=None):
        """Send SCPI command to oscilloscope"""
        if not self.is_connected:
            messagebox.showwarning("Not Connected", "Please connect to oscilloscope first")
            return
            
        if command is None:
            command = self.cmd_entry.get().strip()
            
        if not command:
            return
            
        # Add to history
        self.add_to_history(command)
            
        # Clear entry if sending from entry field
        if command == self.cmd_entry.get().strip():
            self.cmd_entry.delete(0, tk.END)
            
        self.log_message(f"> {command}")
        
        # Send command in thread to avoid blocking GUI
        thread = threading.Thread(target=self._send_command_thread, args=(command,))
        thread.daemon = True
        thread.start()
        
    def add_to_history(self, command):
        """Add command to history buffer"""
        # Don't add duplicate consecutive commands
        if not self.command_history or command != self.command_history[-1]:
            self.command_history.append(command)
        
        # Limit history size (keep last 100 commands)
        if len(self.command_history) > 100:
            self.command_history.pop(0)
            
        # Reset history index
        self.history_index = -1
        
    def history_previous(self, event):
        """Navigate to previous command in history"""
        if not self.command_history:
            return
            
        if self.history_index == -1:
            # Store current text if we're starting navigation
            self.current_text = self.cmd_entry.get()
            self.history_index = len(self.command_history) - 1
        elif self.history_index > 0:
            self.history_index -= 1
            
        self.cmd_entry.delete(0, tk.END)
        self.cmd_entry.insert(0, self.command_history[self.history_index])
        
        return "break"  # Prevent default behavior
        
    def history_next(self, event):
        """Navigate to next command in history"""
        if not self.command_history or self.history_index == -1:
            return
            
        if self.history_index < len(self.command_history) - 1:
            self.history_index += 1
            self.cmd_entry.delete(0, tk.END)
            self.cmd_entry.insert(0, self.command_history[self.history_index])
        else:
            # Reached the end, restore original text
            self.history_index = -1
            self.cmd_entry.delete(0, tk.END)
            if hasattr(self, 'current_text'):
                self.cmd_entry.insert(0, self.current_text)
                
        return "break"  # Prevent default behavior
        
    def _send_command_thread(self, command):
        """Thread for sending commands"""
        try:
            if self.send_raw_command(command):
                # For queries, read response
                if command.strip().endswith('?'):
                    response = self.read_response()
                    if response is not None:
                        self.root.after(0, self.log_message, f"< {response}")
                    else:
                        self.root.after(0, self.log_message, "< No response")
                else:
                    self.root.after(0, self.log_message, "< Command sent")
            else:
                self.root.after(0, self.log_message, "< Failed to send command")
                
        except Exception as e:
            self.root.after(0, self.log_message, f"< Error: {str(e)}")
            
    def send_raw_command(self, command):
        """Send raw command to oscilloscope"""
        if not self.is_connected or not self.connection:
            return False
            
        try:
            if self.connection_type == 'usb':
                self.connection.write((command).encode())
            else:
                # For LAN, some commands might not need newline
                if command.endswith('?'):
                    self.connection.sendall((command).encode())
                else:
                    self.connection.sendall((command).encode())
            return True
        except Exception as e:
            self.log_message(f"Send error: {str(e)}")
            return False
            
    def read_response(self, timeout=3):
        """Read response from oscilloscope"""
        if not self.is_connected or not self.connection:
            return None
            
        try:
            if self.connection_type == 'usb':
                self.connection.timeout = timeout
                response = self.connection.readline().decode().strip()
            else:
                self.connection.settimeout(timeout)
                response = self.connection.recv(1024).decode().strip()
            return response
        except socket.timeout:
            return "Timeout - no response"
        except Exception as e:
            return f"Error reading: {str(e)}"
            
    def log_message(self, message):
        """Add message to output text with timestamp"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}\n"
        
        self.output_text.insert(tk.END, formatted_message)
        self.output_text.see(tk.END)
        
        # Also log to file
        logging.info(message)
        
    def clear_output(self):
        """Clear the output text area"""
        self.output_text.delete(1.0, tk.END)
        
    def on_closing(self):
        """Handle application closing"""
        if self.is_connected:
            self.disconnect_scope()
        self.root.destroy()

def main():
    root = tk.Tk()
    app = OWONSCPIConsole(root)
    
    # Handle window closing
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    
    root.mainloop()

if __name__ == "__main__":
    main()