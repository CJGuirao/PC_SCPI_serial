# OWON HDS / SDS Oscilloscope Control

This project provides a Python-based GUI for OWON HDS200/HDS300 and SDS series oscilloscopes, with SCPI control over LAN or USB. It includes a modern front panel for instrument control, waveform acquisition, measurement, and export.

![OWON HDS Oscilloscope Control GUI](doc/screen.jpg)

## Features
- Connect to OWON HDS200/HDS300 or SDS oscilloscopes via LAN or USB
- SCPI command support for instrument configuration
- Download and parse waveform data (screen and deep memory)
- Waveform plotting with matplotlib
- Query and display measurements (frequency, voltage, timing, etc.)
- USB auto-detect for OWON serial devices (VID/PID 5345:1234)
- Save waveform data as CSV or JSON
- Auto-refresh and continuous monitoring (SLOW)
- Channel controls: display, scale, coupling, probe, vertical offset
- Timebase, trigger, acquisition, and memory depth controls

## Requirements
- Python 3.7+
- tkinter
- numpy
- matplotlib
- pyserial

## Usage
1. Install dependencies: `pip install numpy matplotlib pyserial`
2. Run `main.py` to launch the GUI: `python main.py`
3. Connect to your oscilloscope and use the GUI to control, acquire, and save data.

## File Structure
- `main.py` - Main application and GUI
- `owon_controller.py` - SCPI and binary protocol controller
- `waveform_data.py` - Waveform data parser
- `scope_gui.py` - Additional GUI components
- `console.py` - SCPI console to talk with the scope

## Notes
- I have tested it on a SDS6202 oscilloscope through LAN. To be able to ping it I had to change the default MAC address. **I have not tested it with newer Owon scopes!**.
- **The SCPI implementation in Owon is buggy**, with little or outdated documentation. The connection often times out and then I have to reconnect.
- LLM have been used to help build this app. Mostly Deepseek and Copilot.

