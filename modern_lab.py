"""Compatibility shim for the old single-file panel.

The panel was split into ui/ (widgets, dialogs, the base panel) and the application
into app/application.py. This module re-exports what used to live here, so existing
imports - the tests, the tools, anything outside the repository - keep working
unchanged. New code should import from ui.panel, ui.widgets or ui.dialogs directly.
"""

import threading

from modernlab.app.io_worker import PRIORITY_CAPTURE, PRIORITY_COMMAND, PRIORITY_REFRESH
from ui.widgets import (
    ASSETS, CYAN, HOLD_AMBER, INK, LIVE_GREEN, PANEL, PAUSE_RED, SCREEN, YELLOW,
    Rotary, transport_icon,
)
from ui.panel import (
    CONNECTED_NAME, CURSOR_GRAB_PIXELS, CURSOR_TARGETS, LIVE_CALIBRATE_EVERY,
    LIVE_FRAMING_EVERY, LIVE_GAP_MS, LIVE_HEADER_EVERY, PALETTES, UNCONNECTED_NAME,
    VIEWS, VIEW_LABELS, ModernLabUI,
)
from ui.dialogs import CaptureTableDialog, ReadoutDialog, ScpiConsoleDialog, SetupDialog

__all__ = [
    "threading",
    "ASSETS", "CYAN", "HOLD_AMBER", "INK", "LIVE_GREEN", "PANEL", "PAUSE_RED", "SCREEN",
    "YELLOW", "Rotary", "transport_icon",
    "CONNECTED_NAME", "CURSOR_GRAB_PIXELS", "CURSOR_TARGETS", "LIVE_CALIBRATE_EVERY",
    "LIVE_FRAMING_EVERY", "LIVE_GAP_MS", "LIVE_HEADER_EVERY", "PALETTES",
    "UNCONNECTED_NAME", "VIEWS", "VIEW_LABELS", "ModernLabUI",
    "CaptureTableDialog", "ReadoutDialog", "ScpiConsoleDialog", "SetupDialog",
    "PRIORITY_CAPTURE", "PRIORITY_COMMAND", "PRIORITY_REFRESH",
]
