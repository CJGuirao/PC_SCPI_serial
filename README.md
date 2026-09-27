# OWON HDS / SDS Oscilloscope Control

This project provides a Python-based GUI for OWON HDS200/HDS300 and SDS series oscilloscopes, with SCPI control over LAN or USB. It includes a modern front panel for instrument control, waveform acquisition, measurement, and export.

![OWON HDS Oscilloscope Control GUI](doc/screen.jpg)

## Features

**Acquisition and control**
- Connect to OWON HDS200/HDS300 or SDS oscilloscopes via LAN or USB
- SCPI command support for instrument configuration
- Download and parse waveform data (screen and deep memory)
- USB auto-detect for OWON serial devices (VID/PID 5345:1234), with a picker when
  more than one is attached
- Query and display measurements (frequency, voltage, timing, etc.)
- Auto-refresh and continuous monitoring, at an interval you choose
- Channel controls: display, scale, coupling, probe, vertical offset
- Timebase, trigger, acquisition, and memory depth controls
- A **SCPI console**, read-only until you allow writes, for talking to the
  instrument in its own language
- A **trigger and acquisition read-back panel**: what the instrument says it is
  doing, next to what the capture was actually drawn from
- A **multimeter mode picker**, limited to the functions the instrument answers
- **Software autoset**: frames the time axis from the measured frequency, using the
  one framing write this interface can actually make

**Reading the capture**
- Four views of the same samples: **time, FFT, maths, XY**
- **FFT** with the vendor software's windows (Rectangle, Hanning, Hamming,
  Blackman), in dBV, Vrms or volts, linear or log frequency, with the peaks
  labelled. The frequency axis is derived from the capture's own sample interval,
  never from the instrument's announced rate, and the Nyquist limit is printed on
  the plot
- **Maths traces**: CH1±CH2, CH1×CH2, either channel inverted
- **Measurement cursors**: two time markers giving ΔT, 1/ΔT and each trace's level
  at both, and two voltage markers giving ΔV. Drag them, or click to place them
- A **data table** view of the samples

**Keeping it**
- Save as CSV, JSON, Excel (`.xlsx`), PNG or PDF — every format carrying the
  settings that make its numbers checkable
- Open a saved capture and read it offline, in any of the views
- **Unattended recording**: one CSV per frame into a folder you choose
- A **SETUP** screen that saves which scope to talk to, the calibration, the
  monitoring interval, the plot palette and the FFT defaults
- Plot palettes: Dark, Light, and a Print palette for exported figures

## Requirements
- Python 3.7+
- tkinter
- numpy
- matplotlib
- pyserial
- pyusb (for the USB HID transport used by the HDS200/HDS300)
- Pillow

## Usage
1. Install dependencies: `pip install numpy matplotlib pyserial`
2. Run `main.py` to launch the GUI: `python main.py`
3. Connect to your oscilloscope and use the GUI to control, acquire, and save data.

## File Structure
- `main.py` - Main application and GUI
- `modern_lab.py` - The front panel itself: plot, drawers, dialogs
- `owon_controller.py` - SCPI and binary protocol controller
- `waveform_data.py` - Waveform data parser and vertical calibration
- `hds_usb.py` - USB HID transport and device enumeration
- `analysis.py` - Spectrum, maths, cursors, time axis (no UI, no instrument)
- `waveform_export.py` - CSV/JSON/XLSX/PNG/PDF export, and reading captures back
- `scope_setup.py` - Bench settings: which scope, and the calibration
- `calibrate_manual.py` - Derives the calibration trim from a live capture
- `scope_gui.py` - Additional GUI components
- `console.py` - SCPI console to talk with the scope
- `tests/` - The test suite (`python -m unittest discover -s tests -t .`)

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
