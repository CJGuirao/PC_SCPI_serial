"""Tests for the auto-reconnect watchdog (issue #2).

The watchdog is three methods on ModernLabUI:
  finish_capture     - entry point (existing tests cover the success path;
                       we add the failure / watchdog paths)
  _attempt_reconnect - state machine: halt-clear -> reconnect -> give up
  _finish_reconnect  - result handler called back on the Tk thread

We test these without a real Tk window or instrument by binding the unbound
methods directly onto a plain stub object that holds only the attributes the
watchdog reads and writes.  No Tk mainloop is needed.
"""
import tkinter as tk
import types
import unittest
from unittest.mock import MagicMock

import modern_lab


def _make_stub():
    """Minimal stub that the three watchdog methods can run against."""
    root = tk.Tk()
    root.withdraw()

    scope = MagicMock()
    scope.is_hds = True
    scope.waveform = MagicMock()
    scope.waveform.channels = []
    scope.reconnect.return_value = (True, "OWON,HDS271,test,V1.3.0")
    scope.get_idn.return_value = "OWON,HDS271,test,V1.3.0"
    transport = MagicMock()
    transport.recover.return_value = True
    scope.connection = transport

    s = type("Stub", (), {})()
    s.root = root
    s.scope = scope
    s.auto_refresh_var = tk.BooleanVar(value=True)
    s._capture_failures = 0
    s._closing = False
    s._busy = False
    s._live_period = None
    s._live_frames = 0
    s._live_started = None
    s._live_timer = None
    s._deliveries = {}
    s.capture_state = tk.StringVar()
    s.status_var = tk.StringVar()
    s.device_info = tk.StringVar()
    s.log = MagicMock()
    s.update_live_button = MagicMock()
    s.download_waveform = MagicMock()
    s.report_later = MagicMock()
    s.plot_waveform = MagicMock()
    s.refresh_cursors = MagicMock()
    s.record_capture = MagicMock()
    s.report_auto_frame = MagicMock()
    s.update_state_indicator = MagicMock()
    s.toggle_live = MagicMock()
    s.live_gap_ms = MagicMock(return_value=0)

    # Bind the three watchdog methods from the real class.
    s._attempt_reconnect = types.MethodType(modern_lab.ModernLabUI._attempt_reconnect, s)
    s._finish_reconnect  = types.MethodType(modern_lab.ModernLabUI._finish_reconnect,  s)
    s.finish_capture     = types.MethodType(modern_lab.ModernLabUI.finish_capture,      s)
    s._RECONNECT_GIVE_UP = modern_lab.ModernLabUI._RECONNECT_GIVE_UP

    return s, root, scope


class WatchdogTests(unittest.TestCase):

    def setUp(self):
        self.s, self.root, self.scope = _make_stub()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # _attempt_reconnect state machine

    def test_first_failure_requests_halt_clear(self):
        self.s._capture_failures = 1
        self.s._attempt_reconnect()
        self.assertTrue(self.s.report_later.called)
        self.assertEqual(self.s.report_later.call_args[0][0], "reconnect")
        self.assertIn("RECOVERING", self.s.capture_state.get())

    def test_second_failure_requests_full_reconnect(self):
        self.s._capture_failures = 2
        self.s._attempt_reconnect()
        self.assertTrue(self.s.report_later.called)
        self.assertEqual(self.s.report_later.call_args[0][0], "reconnect")
        self.assertIn("RECONNECTING", self.s.capture_state.get())

    def test_give_up_after_max_failures_stops_live(self):
        self.s._capture_failures = self.s._RECONNECT_GIVE_UP
        self.s._attempt_reconnect()
        self.s.report_later.assert_not_called()
        self.assertFalse(self.s.auto_refresh_var.get())
        self.assertIn("DISCONNECTED", self.s.capture_state.get())

    def test_give_up_logs_actionable_message(self):
        self.s._capture_failures = self.s._RECONNECT_GIVE_UP
        self.s._attempt_reconnect()
        logged = " ".join(str(a) for call in self.s.log.call_args_list for a in call[0])
        self.assertIn("Connect", logged)

    # ------------------------------------------------------------------
    # _finish_reconnect

    def test_successful_full_reconnect_resumes_live(self):
        self.s._capture_failures = 2
        self.s._finish_reconnect((True, "OWON,HDS271,test,V1.3.0"))
        self.assertEqual(self.s._capture_failures, 0)
        self.assertIn("LIVE", self.s.capture_state.get())
        self.assertTrue(self.s.download_waveform.called)

    def test_successful_halt_clear_resumes_live(self):
        # halt-clear path returns a plain non-empty string (the IDN)
        self.s._capture_failures = 1
        self.s._finish_reconnect("OWON,HDS271,test,V1.3.0")
        self.assertEqual(self.s._capture_failures, 0)
        self.assertIn("LIVE", self.s.capture_state.get())
        self.assertTrue(self.s.download_waveform.called)

    def test_failed_tuple_increments_and_retries(self):
        self.s._capture_failures = 2
        self.s._attempt_reconnect = MagicMock()
        self.s._finish_reconnect((False, ""))
        self.assertEqual(self.s._capture_failures, 3)
        self.s._attempt_reconnect.assert_called_once()

    def test_error_increments_and_retries(self):
        self.s._capture_failures = 2
        self.s._attempt_reconnect = MagicMock()
        self.s._finish_reconnect(None, error=RuntimeError("pipe broken"))
        self.assertEqual(self.s._capture_failures, 3)
        self.s._attempt_reconnect.assert_called_once()

    def test_reconnect_resets_live_period_and_frames(self):
        self.s._capture_failures = 2
        self.s._live_period = 1.5
        self.s._live_frames = 42
        self.s._finish_reconnect((True, "OWON,HDS271,test,V1.3.0"))
        self.assertIsNone(self.s._live_period)
        self.assertEqual(self.s._live_frames, 0)

    def test_reconnect_invalidates_capture_header(self):
        self.s._capture_failures = 2
        self.s._finish_reconnect((True, "OWON,HDS271,test,V1.3.0"))
        self.scope.invalidate_capture_header.assert_called_once()

    def test_reconnect_updates_device_info_label(self):
        self.s._capture_failures = 2
        self.s._finish_reconnect((True, "OWON,HDS271,test,V1.3.0"))
        self.assertEqual(self.s.device_info.get(), "OWON,HDS271,test,V1.3.0")

    def test_reconnect_with_empty_idn_keeps_old_device_info(self):
        self.s._capture_failures = 2
        self.s.device_info.set("OWON,HDS271,old,V1.3.0")
        self.s._finish_reconnect((True, ""))
        # empty idn: guard `if idn:` skips the set, old label is preserved
        self.assertEqual(self.s.device_info.get(), "OWON,HDS271,old,V1.3.0")

    # ------------------------------------------------------------------
    # finish_capture integration

    def test_success_resets_failure_counter(self):
        self.s._capture_failures = 3
        self.s.finish_capture(True, None)
        self.assertEqual(self.s._capture_failures, 0)

    def test_failure_while_live_calls_watchdog_not_toggle(self):
        self.s._attempt_reconnect = MagicMock()
        self.s.finish_capture(False, "pipe error")
        self.s._attempt_reconnect.assert_called_once()
        self.s.toggle_live.assert_not_called()

    def test_failure_while_not_live_does_not_call_watchdog(self):
        self.s.auto_refresh_var.set(False)
        self.s._attempt_reconnect = MagicMock()
        self.s.finish_capture(False, "pipe error")
        self.s._attempt_reconnect.assert_not_called()
        self.assertIn("FAILED", self.s.capture_state.get())

    def test_each_live_failure_increments_counter(self):
        self.s._attempt_reconnect = MagicMock()
        self.s.finish_capture(False, "err1")
        self.assertEqual(self.s._capture_failures, 1)
        self.s.finish_capture(False, "err2")
        self.assertEqual(self.s._capture_failures, 2)


if __name__ == "__main__":
    unittest.main()


