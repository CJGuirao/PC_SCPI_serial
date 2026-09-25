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


## HDS200 / HDS300 USB interface

Verified against an HDS271 running firmware V1.3.0. The instrument is a composite device,
VID 0x5345 / PID 0x1234:

| interface | class | endpoints | role |
|---|---|---|---|
| 0 | HID (0x03) | 0x81 IN interrupt, 0x01 OUT interrupt, 64-byte reports | ASCII SCPI |
| 1 | mass storage (0x08/0x06/0x50) | 0x82 IN bulk, 0x02 OUT bulk | 8 MB volume, no filesystem |

`hds_usb.HdsHidTransport` drives interface 0 through pyusb and the installed libusb0.sys
driver. This instrument has no COM port, and no driver change is needed.

Learned on hardware, all of it expensive:

* A write needs a multi-second timeout (500 ms times out); reads must stay short so that
  silence is distinguishable from slowness.
* A read whose size the firmware rejects stalls the endpoint (Errno 32). A halted pipe keeps
  timing out until CLEAR_FEATURE(HALT) is issued, and the halt survives even a fresh process.
  `HdsHidTransport.recover()` therefore runs on open and after two consecutive read failures;
  without it a single wrong-sized read requires unplugging the instrument.
* Channels use the short node: `:CH1:SCALe?`, not `:CHANnel1:SCALe?`.
* Measurements answer per item (`:MEASUrement:CH1:FREQuency?`); `:MEASUrement:CH1?` is silent.
* Replies are engineering notation (5e-04, 5e+00) with loose enum case (AUTo) and are
  normalised onto the UI choice lists by `owon_controller`.
* `:ACQuire:DEPMem` accepts 4K or 8K.
* The instrument's own Utility -> F4 -> USB menu setting does not change the USB descriptors;
  it reports `Oscilloscope MSC+HID` regardless of the selected mode.

### Waveform download is not supported on firmware V1.3.0

`:DATa:WAVe:SCReen:HEAD?` declares a 475-byte JSON header and `:DATa:WAVe:SCReen:CH1?` declares
600 bytes (300 points x 2 bytes, little-endian, per the SCPI manual), but the instrument returns
exactly one 64-byte report and then NAKs. Ruled out by experiment: reads larger than 64 bytes
time out with no data at all; repeating a query restarts it instead of continuing; HID GET_REPORT
stalls for every report type; SET_REPORT is accepted but inert; the legacy
START/STARTBIN/STARTBMP/STARTMEMDEPTH upload protocol is silent; the mass-storage volume carries
no filesystem; and paced 64-byte reads, empty OUT reports and report-ID tokens all yield nothing.
Control, status and measurement traffic is unaffected and verified.
