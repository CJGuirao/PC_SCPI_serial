# Architecture

One page, so a reader can take the project a part at a time. The aim of this
structure is that any one piece can be understood, and tested, without reading the
others.

## The rule everything follows

Dependencies point **one way, downwards**:

```
        main.py                 the entry point: starts the window
           |
   ui/panel.py                  what you see and click
           |
        app/                    the window's worker, its state, and App
           |
  instrument/   analysis/   storage/   settings/
           \         |          |         /
                nothing here imports a UI
```

Nothing under `modernlab/` imports `main`, `modern_lab`, `ui/` or `tkinter`. That is why
the analysis, capture and storage parts can be tested with no window and no instrument —
and why the whole suite (399 tests) runs headless.

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

## The controller, split by dialect

`owon_controller.py` (1710 lines, one class carrying the SDS definitions and then a
second block of HDS definitions under the same names) is now a nine-line shim:

```python
from modernlab.instrument.controller import OWONScopeController
```

so `main.py`, `console.py`, the tools and the tests keep importing it unchanged. The
work sits in `modernlab/instrument/`:

| module | owns |
|---|---|
| `dialect/sds.py` (`SdsDialect`) | the legacy SDS serial / LAN command set, its ladder and choice lists |
| `dialect/hds.py` (`HdsDialect`) | the HDS channel and trigger nodes, the trigger tables, `probe_dialect`, and the reply-normalising helpers |
| `measurements.py` (`Measurements`) | `parse_measurement` (qualifiers, picosecond time units), `get_measurements_numeric`, `verify_scale` |
| `framing.py` (`FramingSettle`, `FramingWatch`) | the settle window, the capture-header cache and `framing_signature`, and the ratio-compared ladders |
| `controller.py` (`OWONScopeController`) | the connection, the capture, and the one place that branches on family |

`OWONScopeController` mixes those bases. The transports are untouched: `transport/`
still owns the bytes, and the serial object, the LAN socket and the HID transport are
still just `self.connection`.

Two rules came out of the split, and both are the reason for it:

- **A name belongs to one family.** Where the families differ (`set_channel_scale`,
  `get_trigger_mode`, `download_waveform_data`, the four trigger helpers) the
  controller checks `self.is_hds` and calls the base that owns the node form. Nothing
  is defined twice in the inheritance graph, so nothing can shadow.
- **No fallback may resolve to itself.** Before, an HDS method written *after* its SDS
  namesake under the same name replaced it, so a fallback spelled
  `OWONScopeController.query(self, ...)` reached the override and recursed to death on
  every non-HDS instrument. The `legacy_*` aliases that papered over this are kept as
  the documented way to reach the legacy implementation, but they are no longer what
  keeps the SDS path alive.

`measurements.py` and `framing.py` are mixins rather than functions so the controller
stays one object for `io_worker.py` to own; they are written against `self.query` and
the family's accessors, and neither imports the other.

## The UI, split by what it is

The two big modules are now split as well. `main.py` is a launcher and the application
lives under `app/`, the window under `ui/`:

```
main.py            the launcher: opens a root window and starts the event loop (32 lines)
ui/
  widgets.py       Rotary, transport_icon and the palette they draw in
  dialogs.py       CaptureTableDialog, ScpiConsoleDialog, ReadoutDialog, SetupDialog
  panel.py         ModernLabUI: layout, live loop, framing, cursors, views
app/
  application.py   App(ModernLabUI): every instrument operation and the drawing
modern_lab.py      compatibility shim - re-exports the old names, see below
```

`ui/` owns what you see and click and knows nothing of instruments; `app/application.py`
owns the controller, the capture state and the drawing, and inherits `ModernLabUI` for
the layout and the helpers (`self.log`, `self.ask_scope`, `self.report_later`,
`self.status_var`, …). The inheritance is untouched by the move: `App` still has exactly
one base, and nothing is defined twice in that graph.

Two details are load-bearing rather than cosmetic:

- **`ASSETS` is anchored to the repository root**, not to the file it is written in.
  It used to be `Path(__file__).parent / "assets"`, which was correct while the constant
  lived at the root; moving it into `ui/` without `parent.parent` would have silently
  pointed every knob and button sprite at `ui/assets/`.
- **`modern_lab.py` is now a shim**, not a second copy: it re-exports the widgets, the
  dialogs, the panel and the constants so existing imports keep working. It also
  re-exports `threading`, because a test patches `modern_lab.threading.Thread` to prove
  a worker that cannot start releases the panel, and `ui.panel` uses the same module
  object, so the patch still reaches it. Likewise `app/application.py` resolves
  `attached_scopes` through the `main` namespace at call time, because that name was a
  `main` global before the split and patching `main.attached_scopes` has to keep working.

## Still to split

Nothing, for the moment. What was a 2645-line `main.py` and a 2081-line `modern_lab.py`
is now the five UI modules above plus a 32-line launcher, and the suite (399 tests) runs
headless against them. If `app/application.py` (2652 lines) is next, the natural seams
are the ones already marked in it: the connection, the vertical framing, the readback
and the plotting each stand alone.

`console.py` (the SCPI console dialog) and `scope_gui.py` (an earlier, unused second
GUI, now in `archive/`) are not part of this flow.
