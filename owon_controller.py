import serial
import socket
import threading
import time
import struct
import logging
from datetime import datetime
from waveform_data import WaveformData


class OWONScopeController:
    """Controller for OWON SDS oscilloscope with full SCPI support"""
    
    # Voltage scale options from documentation
    VOLTAGE_SCALES = ["2mv", "5mv", "10mv", "20mv", "50mv", "100mv", 
                      "200mv", "500mv", "1v", "2v", "5v", "10v"]
    
    # Timebase scale options for SDS6062T from documentation
    TIMEBASE_SCALES = ["5ns", "10ns", "20ns", "50ns", "100ns", "200ns", "500ns",
                       "1us", "2us", "5us", "10us", "20us", "50us", "100us", 
                       "200us", "500us", "1ms", "2ms", "5ms", "10ms", "20ms", 
                       "50ms", "100ms", "200ms", "500ms", "1s", "2s", "5s", 
                       "10s", "20s", "50s", "100s"]
    
    # Acquisition types from documentation
    ACQ_TYPES = ["SAMPle", "AVERage", "PEAK"]
    
    # Average counts from documentation
    AVG_COUNTS = ["4", "16", "64", "128"]
    
    # Memory depth options from documentation
    MEMORY_DEPTHS = ["1K", "10K", "100K", "1M", "10M"]
    
    # Coupling modes from documentation
    COUPLING_MODES = ["AC", "DC", "GND"]
    
    # Probe attenuation from documentation
    PROBE_ATTEN = ["X1", "X10", "X100", "X1000"]
    
    # Trigger types
    TRIGGER_TYPES = ["SINGle", "ALTernate"]
    TRIGGER_MODES = ["AUTO", "NORMal", "SINGle"]
    TRIGGER_SOURCES = ["CH1", "CH2"]
    TRIGGER_SLOPES = ["RISE", "FALL"]
    TRIGGER_COUPLING = ["DC", "AC", "HF", "LF"]
    
    def __init__(self):
        self.connection = None
        self.connection_type = None
        self.is_connected = False
        self.waveform_data = WaveformData()
        self.lock = threading.Lock()
        
    def connect_lan(self, ip='10.1.1.131', port=3000):
        """Connect via LAN"""
        try:
            # self.connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.connection = socket.create_connection((ip, port), timeout = 3)
            self.connection.settimeout(5)
            
            # Enter SCPI mode
            self.send_command(":SDSLSCPI#")
            time.sleep(0.3)
            response = self.read_response(timeout=2)
            
            if not response:
                logging.debug("No response or timeout during SCPI handshake.")
                
            else:
                logging.debug(f"SCPI Handshake response: {response}")
            
            # self.send_command("KLM")
            
            self.is_connected = True
            self.connection_type = 'lan'
            logging.info("Successfully connected via LAN")
            return True
                
        except Exception as e:
            logging.error(f"LAN Connection error: {e}")
            return False
    
    def connect_usb(self, port='COM3', baudrate=115200):
        """Connect via USB serial"""
        try:
            self.connection = serial.Serial(port, baudrate, timeout=2)
            # Enter SCPI mode
            response = self.query(":SDSLSCPI#")
            
            self.is_connected = True
            self.connection_type = 'usb'
            logging.info("Successfully connected via USB")
            return True
        except Exception as e:
            logging.error(f"USB Connection error: {e}")
            return False
    
    def send_command(self, command):
        """Send command to oscilloscope"""
        if not self.is_connected or not self.connection:
            return False
            
        logging.debug(f"[SEND] {command}")
        try:
            with self.lock:
                if self.connection_type == 'usb':
                    self.connection.write((command).encode())
                else:
                    self.connection.sendall((command).encode())
            return True
        except Exception as e:
            logging.error(f"Send command error: {e}")
            return False

    def read_response(self, timeout=2):
        """Read response from oscilloscope"""
        if not self.is_connected or not self.connection:
            return None
            
        try:
            with self.lock:
                if self.connection_type == 'usb':
                    self.connection.timeout = timeout
                    response = self.connection.readline().decode().strip()
                else:
                    self.connection.settimeout(timeout)
                    response = self.connection.recv(1024).decode().strip()
            logging.debug(f"[RECV] {response}")
            return response
        except socket.timeout:
            logging.debug("Read timeout")
            return ""
        except Exception as e:
            logging.error(f"Read response error: {e}")
            return None
    
    def query(self, command):
        """Send query and return response"""
        if self.send_command(command):
            time.sleep(0.1)
            return self.read_response()
        return None
    
    # IEEE488.2 Common Commands
    def get_idn(self):
        """Get instrument identification (*IDN?)"""
        return self.query("*IDN?")
    
    def reset(self):
        """Reset oscilloscope (*RST)"""
        return self.send_command("*RST")
    
    def clear_status(self):
        """Clear status (*CLS)"""
        return self.send_command("*CLS")
    
    def operation_complete(self):
        """Query operation complete (*OPC?)"""
        return self.query("*OPC?")
    
    # Channel Commands
    def set_channel_display(self, channel, state):
        """Turn channel display on/off (:CHANnel<n>:DISPlay)"""
        state_str = "ON" if state else "OFF"
        return self.send_command(f":CHANnel{channel}:DISPlay {state_str}")
    
    def get_channel_display(self, channel):
        """Query channel display state"""
        return self.query(f":CHANnel{channel}:DISPlay?")
    
    def set_channel_coupling(self, channel, coupling):
        """Set channel coupling mode (:CHANnel<n>:COUPling)"""
        return self.send_command(f":CHANnel{channel}:COUPling {coupling}")
    
    def get_channel_coupling(self, channel):
        """Query channel coupling mode"""
        return self.query(f":CHANnel{channel}:COUPling?")
    
    def set_channel_probe(self, channel, attenuation):
        """Set probe attenuation (:CHANnel<n>:PROBe)"""
        return self.send_command(f":CHANnel{channel}:PROBe {attenuation}")
    
    def get_channel_probe(self, channel):
        """Query probe attenuation"""
        return self.query(f":CHANnel{channel}:PROBe?")
    
    def set_channel_scale(self, channel, scale):
        """Set channel vertical scale (:CHANnel<n>:SCALe)"""
        return self.send_command(f":CHANnel{channel}:SCALe {scale}")
    
    def get_channel_scale(self, channel):
        """Query channel vertical scale"""
        return self.query(f":CHANnel{channel}:SCALe?")
    
    def set_channel_offset(self, channel, offset):
        """Set channel vertical offset (:CHANnel<n>:OFFSet)"""
        return self.send_command(f":CHANnel{channel}:OFFSet {offset}")
    
    def get_channel_offset(self, channel):
        """Query channel vertical offset"""
        return self.query(f":CHANnel{channel}:OFFSet?")
    
    # Timebase Commands
    def set_timebase_scale(self, scale):
        """Set timebase scale (:TIMebase:SCALe)"""
        return self.send_command(f":TIMebase:SCALe {scale}")
    
    def get_timebase_scale(self):
        """Query timebase scale"""
        return self.query(":TIMebase:SCALe?")
    
    def set_timebase_offset(self, offset):
        """Set horizontal offset (:TIMebase:HOFFset)"""
        return self.send_command(f":TIMebase:HOFFset {offset}")
    
    def get_timebase_offset(self):
        """Query horizontal offset"""
        return self.query(":TIMebase:HOFFset?")
    
    # Acquisition Commands
    def set_acquire_type(self, acq_type):
        """Set acquisition type (:ACQuire:TYPE)"""
        return self.send_command(f":ACQuire:TYPE {acq_type}")
    
    def get_acquire_type(self):
        """Query acquisition type"""
        return self.query(":ACQuire:TYPE?")
    
    def set_acquire_average(self, count):
        """Set number of averages (:ACQuire:AVERage)"""
        return self.send_command(f":ACQuire:AVERage {count}")
    
    def get_acquire_average(self):
        """Query number of averages"""
        return self.query(":ACQuire:AVERage?")
    
    def set_memory_depth(self, depth):
        """Set memory depth (:ACQuire:MDEPth)"""
        return self.send_command(f":ACQuire:MDEPth {depth}")
    
    def get_memory_depth(self):
        """Query memory depth"""
        return self.query(":ACQuire:MDEPth?")
    
    # Trigger Commands
    def set_trigger_mode(self, mode):
        """Set trigger mode (:TRIGger:MODE)"""
        return self.send_command(f":TRIGger:MODE {mode}")
    
    def get_trigger_mode(self):
        """Query trigger mode"""
        return self.query(":TRIGger:MODE?")
    
    def set_trigger_type(self, trig_type):
        """Set trigger type (:TRIGger:TYPE)"""
        return self.send_command(f":TRIGger:TYPE {trig_type}")
    
    def get_trigger_type(self):
        """Query trigger type"""
        return self.query(":TRIGger:TYPE?")
    
    def set_edge_trigger_source(self, source):
        """Set edge trigger source (:TRIGger:SINGle:EDGE:SOURce)"""
        return self.send_command(f":TRIGger:SINGle:EDGE:SOURce {source}")
    
    def get_edge_trigger_source(self):
        """Query edge trigger source"""
        return self.query(":TRIGger:SINGle:EDGE:SOURce?")
    
    def set_edge_trigger_slope(self, slope):
        """Set edge trigger slope (:TRIGger:SINGle:EDGE:SLOPe)"""
        return self.send_command(f":TRIGger:SINGle:EDGE:SLOPe {slope}")
    
    def get_edge_trigger_slope(self):
        """Query edge trigger slope"""
        return self.query(":TRIGger:SINGle:EDGE:SLOPe?")
    
    def set_edge_trigger_level(self, level):
        """Set edge trigger level (:TRIGger:SINGle:EDGE:LEVel)"""
        return self.send_command(f":TRIGger:SINGle:EDGE:LEVel {level}")
    
    def get_edge_trigger_level(self):
        """Query edge trigger level"""
        return self.query(":TRIGger:SINGle:EDGE:LEVel?")
    
    def set_edge_trigger_coupling(self, coupling):
        """Set edge trigger coupling (:TRIGger:SINGle:EDGE:COUPling)"""
        return self.send_command(f":TRIGger:SINGle:EDGE:COUPling {coupling}")
    
    def get_edge_trigger_coupling(self):
        """Query edge trigger coupling"""
        return self.query(":TRIGger:SINGle:EDGE:COUPling?")
    
    # Measurement Commands
    def set_measure_source(self, source):
        """Set measurement source (:MEASure:SOURce)"""
        return self.send_command(f":MEASure:SOURce {source}")
    
    def measure_frequency(self):
        """Measure frequency (:MEASure:FREQuency?)"""
        return self.query(":MEASure:FREQuency?")
    
    def measure_period(self):
        """Measure period (:MEASure:PERiod?)"""
        return self.query(":MEASure:PERiod?")
    
    def measure_vpp(self):
        """Measure peak-to-peak voltage (:MEASure:PKPK?)"""
        return self.query(":MEASure:PKPK?")
    
    def measure_vmax(self):
        """Measure maximum voltage (:MEASure:MAX?)"""
        return self.query(":MEASure:MAX?")
    
    def measure_vmin(self):
        """Measure minimum voltage (:MEASure:MIN?)"""
        return self.query(":MEASure:MIN?")
    
    def measure_vavg(self):
        """Measure average voltage (:MEASure:AVERage?)"""
        return self.query(":MEASure:AVERage?")
    
    def measure_vrms(self):
        """Measure RMS voltage (:MEASure:CYCRms?)"""
        return self.query(":MEASure:CYCRms?")
    
    def measure_vtop(self):
        """Measure top voltage (:MEASure:VTOP?)"""
        return self.query(":MEASure:VTOP?")
    
    def measure_vbase(self):
        """Measure base voltage (:MEASure:VBASe?)"""
        return self.query(":MEASure:VBASe?")
    
    def measure_vamp(self):
        """Measure amplitude (:MEASure:VAMP?)"""
        return self.query(":MEASure:VAMP?")
    
    def measure_rise_time(self):
        """Measure rise time (:MEASure:RTime?)"""
        return self.query(":MEASure:RTime?")
    
    def measure_fall_time(self):
        """Measure fall time (:MEASure:FTime?)"""
        return self.query(":MEASure:FTime?")
    
    def measure_pos_width(self):
        """Measure positive pulse width (:MEASure:PWIDth?)"""
        return self.query(":MEASure:PWIDth?")
    
    def measure_neg_width(self):
        """Measure negative pulse width (:MEASure:NWIDth?)"""
        return self.query(":MEASure:NWIDth?")
    
    def measure_pos_duty(self):
        """Measure positive duty cycle (:MEASure:PDUTy?)"""
        return self.query(":MEASure:PDUTy?")
    
    def measure_neg_duty(self):
        """Measure negative duty cycle (:MEASure:NDUTy?)"""
        return self.query(":MEASure:NDUTy?")
    
    def measure_overshoot(self):
        """Measure overshoot (:MEASure:OVERshoot?)"""
        return self.query(":MEASure:OVERshoot?")
    
    def measure_preshoot(self):
        """Measure preshoot (:MEASure:PREShoot?)"""
        return self.query(":MEASure:PREShoot?")
    
    def add_measurement(self, item):
        """Add measurement item (:MEASure:ADD)"""
        return self.send_command(f":MEASure:ADD {item}")
    
    def delete_measurement(self, item):
        """Delete measurement item (:MEASure:DELete)"""
        return self.send_command(f":MEASure:DELete {item}")
    
    def get_all_measurements(self, channel=1):
        """Get comprehensive measurements"""
        self.set_measure_source(f"CH{channel}")
        time.sleep(0.1)
        
        measurements = {}
        measure_funcs = {
            'Frequency': self.measure_frequency,
            'Period': self.measure_period,
            'Vpp': self.measure_vpp,
            'Vmax': self.measure_vmax,
            'Vmin': self.measure_vmin,
            'Vavg': self.measure_vavg,
            'Vrms': self.measure_vrms,
            'Vtop': self.measure_vtop,
            'Vbase': self.measure_vbase,
            'Vamp': self.measure_vamp,
            'Rise Time': self.measure_rise_time,
            'Fall Time': self.measure_fall_time,
            'Pos Width': self.measure_pos_width,
            'Neg Width': self.measure_neg_width,
            'Pos Duty': self.measure_pos_duty,
            'Neg Duty': self.measure_neg_duty,
            'Overshoot': self.measure_overshoot,
            'Preshoot': self.measure_preshoot,
        }
        
        for name, func in measure_funcs.items():
            try:
                value = func()
                if value:
                    measurements[name] = value
                time.sleep(0.1)
            except:
                pass
        
        return measurements
    
    def download_waveform_data(self):
        """Download waveform data using OWON binary protocol"""
        if not self.is_connected:
            logging.error("Not connected to oscilloscope")
            return False
            
        try:
            # Request waveform data
            logging.info("Requesting waveform data...")
            self.send_command("*CLS")
            time.sleep(0.1)
            self.send_command("STARTMEMDEPTH")
            time.sleep(0.1)
            
            # Read header
            header_data = self.read_binary_data(12, timeout=5)
            if not header_data or len(header_data) < 12:
                logging.error("Failed to read response header")
                return False
                
            file_length = struct.unpack('<I', header_data[0:4])[0]
            flags = struct.unpack('<I', header_data[8:12])[0]
            
            logging.info(f"File length: {file_length}, Flags: {flags}")
            logging.info(f"File length (hex): 0x{file_length:08X}, Flags (hex): 0x{flags:08X}")
            
            if file_length > 0:
                waveform_data = self.read_binary_data(file_length, timeout=15)
                if logging.getLogger().isEnabledFor(logging.DEBUG):
                    debug_filename = f"data/waveform_debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bin"
                    try:
                        with open(debug_filename, "wb") as f:
                            f.write(waveform_data)
                        logging.debug(f"Saved raw waveform binary to {debug_filename}")
                    except Exception as e:
                        logging.error(f"Failed to save debug waveform binary: {e}")
                if waveform_data and len(waveform_data) >= file_length:
                    self.waveform_data.parse_bin_data(waveform_data, file_length)
                    logging.info("Successfully downloaded waveform data")
                    return True
                    
        except Exception as e:
            logging.error(f"Error downloading waveform data: {e}")
            
        return False
    
    def read_binary_data(self, expected_size, timeout=10):
        """Read binary data"""
        if not self.is_connected or not self.connection:
            return None
            
        try:
            with self.lock:
                if self.connection_type == 'usb':
                    self.connection.timeout = timeout
                    data = self.connection.read(expected_size)
                else:
                    self.connection.settimeout(timeout)
                    data = b''
                    remaining = expected_size
                    
                    while remaining > 0:
                        chunk_size = min(1024, remaining)
                        chunk = self.connection.recv(chunk_size)
                        if not chunk:
                            break
                        data += chunk
                        remaining -= len(chunk)
                        # time.sleep(0.1)
                        
            logging.info(f"Read {len(data)} bytes of binary data")
            return data
            
        except Exception as e:
            logging.error(f"Read binary data error: {e}")
            return None
    
    def disconnect(self):
        """Disconnect from oscilloscope"""
        self.is_connected = False
        if self.connection:
            try:
                self.connection.close()
            except:
                pass
