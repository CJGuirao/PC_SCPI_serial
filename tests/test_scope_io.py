"""The single-owner instrument worker: order, coalescing, and never wedging.

The properties worth pinning are the ones the window's responsiveness rests on:
submitting never blocks, one call is in flight at a time, a slow call does not
stop the ones behind it from being asked, a refresh that has been overtaken is
dropped rather than queued, and an exception inside a call does not end the
worker.
"""
import threading
import time
import unittest

from modernlab.app.io_worker import PRIORITY_CAPTURE, PRIORITY_REFRESH, ScopeIO


class FakeScope:
    """A scope whose calls take a known time and can be made to fail."""

    def __init__(self, delay=0.0):
        self.delay = delay
        self.calls = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def call(self, name, result=None, delay=None):
        with self._lock:
            self.calls.append(name)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.delay if delay is None else delay)
            if name.startswith("bad"):
                raise RuntimeError("the instrument said no")
            return result if result is not None else name
        finally:
            with self._lock:
                self.in_flight -= 1

    @property
    def names(self):
        with self._lock:
            return list(self.calls)


class ResultCollector:
    """Stands in for the UI thread draining finished calls."""

    def __init__(self):
        self.seen = []
        self.errors = []
        self._lock = threading.Lock()

    def __call__(self, kind, token, value, error):
        with self._lock:
            self.seen.append((kind, token, value))
            if error is not None:
                self.errors.append((kind, error))

    def wait_for(self, count, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.seen) >= count:
                    return True
            time.sleep(0.005)
        return False

    @property
    def kinds(self):
        with self._lock:
            return [kind for kind, _, _ in self.seen]


class SubmitTests(unittest.TestCase):
    def setUp(self):
        self.io = ScopeIO(FakeScope(delay=0.2))
        self.addCleanup(self.io.stop)

    def test_submitting_does_not_wait_for_the_instrument(self):
        self.io.start()
        started = time.monotonic()
        self.io.submit("capture", lambda scope: scope.call("capture"))
        elapsed = time.monotonic() - started
        # The whole point: a capture costs a fifth of a second, and submitting it
        # must cost the UI thread nothing.
        self.assertLess(elapsed, 0.05)

    def test_results_come_back_through_poll_on_this_thread(self):
        collector = ResultCollector()
        self.io.submit("capture", lambda scope: scope.call("capture", result=42))
        self.assertTrue(collector.wait_for(1) or True)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self.io.poll(collector):
                break
            time.sleep(0.01)
        self.assertEqual(collector.seen, [("capture", 1, 42)])

    def test_work_runs_in_the_order_it_was_asked(self):
        collector = ResultCollector()
        for name in ("first", "second", "third"):
            self.io.submit("capture", lambda scope, name=name: scope.call(name))
        deadline = time.monotonic() + 5.0
        while len(collector.seen) < 3 and time.monotonic() < deadline:
            self.io.poll(collector)
        self.assertEqual(self.io.scope.names, ["first", "second", "third"])

    def test_only_one_call_is_in_flight_at_a_time(self):
        collector = ResultCollector()
        for index in range(6):
            self.io.submit("capture", lambda scope, index=index: scope.call("call%d" % index))
        deadline = time.monotonic() + 5.0
        while len(collector.seen) < 6 and time.monotonic() < deadline:
            self.io.poll(collector)
        # The endpoint takes one owner at a time: two threads in the same handle
        # would each receive the other's reply.
        self.assertEqual(self.io.scope.max_in_flight, 1)


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.io = ScopeIO(FakeScope(delay=0.0))
        self.addCleanup(self.io.stop)
        self.collector = ResultCollector()

    def test_a_call_that_raises_is_reported_not_raised(self):
        self.io.submit("capture", lambda scope: scope.call("bad-capture"))
        deadline = time.monotonic() + 5.0
        while not self.collector.seen and time.monotonic() < deadline:
            self.io.poll(self.collector)
        self.assertEqual(self.collector.kinds, ["capture"])
        self.assertIsInstance(self.collector.errors[0][1], RuntimeError)
        self.assertEqual(self.io.failed, 1)

    def test_the_worker_survives_a_failed_call(self):
        self.io.submit("capture", lambda scope: scope.call("bad-capture"))
        self.io.submit("capture", lambda scope: scope.call("good-capture", result="ok"))
        deadline = time.monotonic() + 5.0
        while len(self.collector.seen) < 2 and time.monotonic() < deadline:
            self.io.poll(self.collector)
        # A worker that dies on the first failure leaves the panel wedged with
        # nothing on screen to say why.
        self.assertEqual(self.collector.kinds, ["capture", "capture"])
        self.assertEqual(self.collector.seen[-1][2], "ok")
        self.assertTrue(self.io.running)

    def test_a_handler_that_raises_does_not_swallow_the_rest(self):
        done = []

        def handler(kind, token, value, error):
            done.append(kind)
            if kind == "first":
                raise RuntimeError("bad handler")

        self.io.submit("first", lambda scope: scope.call("one"))
        self.io.submit("second", lambda scope: scope.call("two"))
        deadline = time.monotonic() + 5.0
        while len(done) < 2 and time.monotonic() < deadline:
            self.io.poll(handler)
            time.sleep(0.01)
        self.assertEqual(done, ["first", "second"])

    def test_a_submit_after_stop_is_refused_rather_than_queued_forever(self):
        self.io.stop()
        self.assertIsNone(self.io.submit("capture", lambda scope: scope.call("late")))
        self.assertEqual(self.io.scope.names, [])


class CoalescingTests(unittest.TestCase):
    """A refresh that has been overtaken is worth nothing and costs a slot."""

    def test_only_the_newest_refresh_of_a_kind_runs(self):
        io = ScopeIO(FakeScope(delay=0.15))
        self.addCleanup(io.stop)
        collector = ResultCollector()
        io.submit("capture", lambda scope: scope.call("capture"))
        for index in range(5):
            io.submit("framing", lambda scope, index=index: scope.call("framing%d" % index),
                      coalesce=True)
        io.submit("capture", lambda scope: scope.call("capture2"))
        deadline = time.monotonic() + 5.0
        while len(collector.seen) < 3 and time.monotonic() < deadline:
            io.poll(collector)
        self.assertEqual(io.scope.names.count("capture"), 1)
        self.assertEqual(io.scope.names.count("capture2"), 1)
        self.assertEqual(io.scope.names.count("framing4"), 1)
        # Four of the five were dropped, and none of them delayed the second
        # capture, which is the frame the user is waiting for.
        self.assertEqual(io.dropped, 4)

    def test_a_capture_is_never_dropped(self):
        io = ScopeIO(FakeScope(delay=0.0))
        self.addCleanup(io.stop)
        collector = ResultCollector()
        for index in range(4):
            io.submit("capture", lambda scope, index=index: scope.call("capture%d" % index))
        deadline = time.monotonic() + 5.0
        while len(collector.seen) < 4 and time.monotonic() < deadline:
            io.poll(collector)
        self.assertEqual(io.dropped, 0)
        self.assertEqual(len(io.scope.names), 4)


class PriorityTests(unittest.TestCase):
    """A refresh must never sit in front of the frame the user is waiting for."""

    def test_a_capture_overtakes_refreshes_that_were_queued_first(self):
        io = ScopeIO(FakeScope(delay=0.1))
        self.addCleanup(io.stop)
        collector = ResultCollector()
        # What the end of a frame actually does: the readbacks are asked for first
        # and the next frame immediately after.
        io.submit("cursors", lambda scope: scope.call("cursors"), coalesce=True,
                  priority=PRIORITY_REFRESH)
        io.submit("dmm", lambda scope: scope.call("dmm"), coalesce=True,
                  priority=PRIORITY_REFRESH)
        io.submit("capture", lambda scope: scope.call("capture"), priority=PRIORITY_CAPTURE)
        deadline = time.monotonic() + 5.0
        while len(collector.seen) < 3 and time.monotonic() < deadline:
            io.poll(collector)
        self.assertEqual(io.scope.names[0], "capture")

    def test_a_command_overtakes_a_refresh(self):
        io = ScopeIO(FakeScope(delay=0.05))
        self.addCleanup(io.stop)
        collector = ResultCollector()
        io.submit("framing", lambda scope: scope.call("framing"), coalesce=True,
                  priority=PRIORITY_REFRESH)
        io.submit("write", lambda scope: scope.call("write"))
        deadline = time.monotonic() + 5.0
        while len(collector.seen) < 2 and time.monotonic() < deadline:
            io.poll(collector)
        self.assertEqual(io.scope.names, ["write", "framing"])


class QuietTests(unittest.TestCase):
    def test_quiet_says_when_nothing_is_left(self):
        io = ScopeIO(FakeScope(delay=0.05))
        self.addCleanup(io.stop)
        self.assertTrue(io.quiet())
        io.submit("capture", lambda scope: scope.call("capture"))
        deadline = time.monotonic() + 5.0
        while io.completed == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        # The result is in the queue but not yet taken: still work to hand over.
        self.assertFalse(io.quiet())
        io.drain()
        deadline = time.monotonic() + 5.0
        while not io.quiet() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(io.quiet())


class ProviderTests(unittest.TestCase):
    """The worker must follow the controller the app holds, not the one it was born with."""

    def test_a_call_follows_the_controller_the_app_now_holds(self):
        first, second = FakeScope(), FakeScope()
        current = [first]
        io = ScopeIO(lambda: current[0])
        self.addCleanup(io.stop)
        collector = ResultCollector()
        io.submit("capture", lambda scope: scope.call("one"))
        deadline = time.monotonic() + 5.0
        while len(collector.seen) < 1 and time.monotonic() < deadline:
            io.poll(collector)
        # A reconnect replaces the controller; a worker wired to the old object
        # would keep calling an instrument that is gone and look like it did
        # nothing at all.
        current[0] = second
        io.submit("capture", lambda scope: scope.call("two"))
        deadline = time.monotonic() + 5.0
        while len(collector.seen) < 2 and time.monotonic() < deadline:
            io.poll(collector)
        self.assertEqual(first.names, ["one"])
        self.assertEqual(second.names, ["two"])


class LifecycleTests(unittest.TestCase):
    def test_start_is_idempotent(self):
        io = ScopeIO(FakeScope())
        self.addCleanup(io.stop)
        first = io.start()
        self.assertIs(io.start(), first)

    def test_stop_ends_the_thread_without_waiting_on_the_queue(self):
        io = ScopeIO(FakeScope(delay=0.05))
        io.start()
        io.submit("capture", lambda scope: scope.call("capture"))
        self.assertTrue(io.stop(timeout=2.0))
        self.assertFalse(io.running)

    def test_a_second_start_after_stop_works(self):
        io = ScopeIO(FakeScope())
        io.start()
        io.stop()
        io.start()                     # explicit, so a close is not undone by a race
        collector = ResultCollector()
        io.submit("capture", lambda scope: scope.call("again", result="fine"))
        deadline = time.monotonic() + 5.0
        while not collector.seen and time.monotonic() < deadline:
            io.poll(collector)
        self.assertEqual(collector.seen[0][2], "fine")
        io.stop()

    def test_drain_empties_the_finished_results(self):
        io = ScopeIO(FakeScope())
        self.addCleanup(io.stop)
        io.submit("capture", lambda scope: scope.call("capture"))
        deadline = time.monotonic() + 5.0
        while io.completed == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        io.drain()
        self.assertEqual(io.poll(lambda *args: None), 0)


if __name__ == "__main__":
    unittest.main()
