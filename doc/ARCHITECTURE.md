# Architecture

One page, so a reader can take the project a part at a time. The aim of this
structure is that any one piece can be understood, and tested, without reading the
others.

## The rule everything follows

Dependencies point **one way, downwards**:

```
        main.py                 the entry point: starts the window
           |
   ui / modern_lab.py           what you see and click
           |
        app/                    the window's worker and its state
           |
  instrument/   analysis/   storage/   settings/
           \         |          |         /
                nothing here imports a UI
```

Nothing under `modernlab/` imports `main`, `modern_lab` or `tkinter`. That is why the
analysis, capture and storage parts can be tested with no window and no instrument —
and why the whole suite (362 tests) runs headless.

## The parts

| package | owns | must not |
|---|---|---|
| `modernlab/instrument/transport/` | moving bytes over a link: USB HID today | know any SCPI command |
| `modernlab/instrument/capture/` | turning a payload into volts: the wrapped codes, the header's framing, the calibration | talk to a device |
| `modernlab/analysis/` | numbers from samples: spectrum, maths, cursors, time axis | touch a file, a device or a widget |
| `modernlab/storage/` | CSV, JSON, XLSX, PNG, PDF out; captures back in | know where the numbers came from |
| `modernlab/settings/` | the bench: which scope, the calibration remembered for it | draw anything |
| `modernlab/app/` | the single owner of the instrument (`io_worker.py`) | run on the UI thread |

Note the direction inside `instrument/`: `transport` knows about bytes, `capture`
knows about volts, and neither knows about the other. A new link (serial, LAN) is a
new file in `transport/`; a new instrument family is a new dialect beside the
controller, not a change to the decode.

## How a number gets on screen

```
instrument ──bytes──▶ transport ──payload──▶ capture.decode ──volts──▶ plot
     ▲                                             │
     └────────── SCPI writes (timebase only) ◀─────┘  calibration reads the
                                                      instrument's own readings
```

The calibration is the interesting edge: `capture` needs four numbers (MIN, MAX,
PKPK, mean) that only the instrument can give, so `controller.calibrate_capture()`
fetches them and hands them in. The decode never reaches for the device itself.

## Where the rules came from

Each of these is a comment in the code and a test in `tests/`, rather than folklore:

- Sample codes are **absolute** and **wrap** at 0/255 — see `capture/waveform.py`.
- The volts/div **label is cosmetic**; the scale comes from the instrument's readings.
- **Volts/div and vertical position cannot be written** over USB; the timebase can.
- The **frame is part of the measurement**: centred on the capture, never finer than
  the decode, and an oversized trace is reported rather than silently chopped.

## Still to split

This pass moved the leaves. Three modules are still doing too much, in the order
worth tackling:

1. `owon_controller.py` (1710 lines) — transport, dialect, framing, measurements and
   capture orchestration in one class. Target: `instrument/dialect/{hds,sds}.py`,
   `instrument/measurements.py`, `instrument/framing.py`, `instrument/controller.py`.
2. `modern_lab.py` (1911 lines) — panel, widgets and four dialogs. Target:
   `ui/widgets/`, `ui/panels/`, `ui/dialogs/`, `ui/window.py`.
3. `main.py` (2267 lines) — the application logic. Target: `app/application.py` plus
   one module per panel, with `main.py` reduced to a launcher.

Until then each of those is still a single file, and this page should not pretend
otherwise: what moved is the leaf layer, and the suite is the proof it moved intact.

`console.py` (the SCPI console dialog) and `scope_gui.py` (an earlier, unused second
GUI, now in `archive/`) are not part of this flow.
