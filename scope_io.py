"""One thread owns every call to the instrument.

Why this exists
---------------
Two things were making the window unanswerable while a capture was in flight.
The captures themselves already ran on a worker, but the framing watch, the
cursor readback and the multimeter read were called from the Tk callbacks, so
each of them froze the window for the length of a USB round trip - about 96 ms
for the framing watch, 32 ms a go for the rest. And because the endpoint takes
one owner at a time, every instrument-setting control was refused outright while
a capture ran ("Acquiring waveform... please wait"), which is half of every
second during live acquisition.

So all instrument I/O goes through the one worker here, in the order asked, and
the UI thread only submits work and applies results. A control pressed during a
capture is queued behind it instead of being refused, and the window never waits
on the instrument at all.

A thread rather than a process: the work is almost entirely waiting on USB
reports, which releases the GIL, so a thread overlaps exactly as a fork would -
while the widgets, the figure and the canvas can only be touched from the thread
that made them, so the drawing could not have moved out anyway.

Why the single owner is not merely tidy: two threads sharing one HID handle
interleave their 64-byte reads, so each receives the other's reply. Before this
module the capture ran on a worker thread while the framing watch and the cursor
readback ran from the Tk thread, against the same handle.

Coalescing
----------
A refresh call that has waited behind three captures is worth nothing, and worse,
it delays whatever is behind it. A newer request of a coalesced kind replaces an
older one rather than queueing behind it. Captures never coalesce - each one is a
frame the user asked for.

Priority, because a queue is still a queue
-----------------------------------------
Order alone was not enough: a cursor readback and a multimeter read submitted at
the end of one frame would still sit in front of the next capture, so every frame
would carry another 96 ms of refresh work ahead of it. Captures go first, a
control's own write behind them, and the calls that only refresh something last -
which is also the order the user would ask for, since the thing they are waiting
for is the frame.
"""
import logging
import queue
import threading


#: A frame the user is waiting for.
PRIORITY_CAPTURE = 0
#: Something the user just asked for: it must happen, and soon.
PRIORITY_COMMAND = 1
#: A readback that only keeps a display current. These coalesce and run last.
PRIORITY_REFRESH = 2


class ScopeIO:
    """A single-threaded executor for calls to one instrument."""

    def __init__(self, scope, name="scope-io"):
        """``scope`` is the controller, or a callable that returns the one in use.

        A callable is taken as a provider rather than as the scope itself - a test
        double for an instrument controller is not callable, an app's own method is -
        so the worker always talks to the controller the app holds right now. Wired
        to the object instead, a reconnect would leave it answering from an
        instrument that is no longer there, and the calls would look like they went
        nowhere at all.
        """
        self._provider = scope if callable(scope) else (lambda: scope)
        self.name = name
        self._queue = queue.PriorityQueue()
        self._results = queue.Queue()
        self._latest = {}                       # coalesced kind -> newest token
        self._lock = threading.Lock()
        self._thread = None
        self._token = 0
        self._sequence = 0
        self._stopping = False
        # Distinct from "not running": a worker that was never started may start
        # itself on the first submit, while one that has been stopped on purpose
        # must not, or a request racing the close would bring the thread back up.
        self._stopped = False
        # Counters worth showing, because they say whether the app is keeping up
        # with the instrument or quietly throwing work away.
        self.completed = 0
        self.failed = 0
        self.dropped = 0
        #: True while a call is actually running, so a caller can wait for quiet.
        self.busy = False

    @property
    def scope(self):
        """The controller to call, resolved at call time."""
        return self._provider()

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        """Start the worker. Idempotent, so a reconnect does not stack threads."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self._thread
            self._stopping = False
            self._stopped = False
            self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
            self._thread.start()
            return self._thread

    @property
    def running(self):
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def stop(self, timeout=2.0):
        """Stop taking work and end the thread.

        The sentinel sorts ahead of everything, so queued work is abandoned rather
        than waited for - a close must not sit behind three refresh reads - while
        the call already in flight is allowed to finish.

        The worker is a daemon, so a call that outlives this at interpreter exit is
        not a hang - but nothing may be waiting on it either, which is why a thread
        that ignores the timeout is reported rather than joined forever.
        """
        with self._lock:
            # Set even when no worker was ever started: stop means stopped, and a
            # request racing the close must not bring a thread up.
            self._stopped = True
            if self._thread is None:
                return True
            self._stopping = True
            thread = self._thread
        # Sorted ahead of everything, and a unique sequence keeps a tie from ever
        # reaching the rest of the tuple.
        self._queue.put((-1, 0, None, 0, None, False))
        thread.join(timeout)
        if thread.is_alive():
            logging.warning("scope io: %s did not stop within %.1fs", self.name, timeout)
            return False
        with self._lock:
            self._thread = None
        return True

    # ------------------------------------------------------------------ submitting
    def submit(self, kind, work, coalesce=False, priority=PRIORITY_COMMAND):
        """Queue a call. Returns its token, or None when the worker is not running.

        ``work`` is called with the scope as its only argument, on the worker thread.
        """
        if self._stopped:
            return None
        if not self.running:
            self.start()
        with self._lock:
            if self._stopping:
                return None
            self._token += 1
            token = self._token
            self._sequence += 1
            sequence = self._sequence
            if coalesce:
                self._latest[kind] = token
        self._queue.put((priority, sequence, kind, token, work, coalesce))
        return token

    def pending(self):
        """How many calls are queued, for a status line or a test."""
        return self._queue.qsize()

    def quiet(self):
        """True when there is nothing left to do AND nothing left to hand over.

        The results queue has to be counted: a finished call whose result has not
        been taken yet is still work, and a caller waiting for quiet that could not
        see it would return having missed the answer entirely - which is exactly what
        it looked like when the tests started asserting on calls that had run but
        whose results nobody had collected.
        """
        return (self._queue.qsize() == 0 and self._results.qsize() == 0
                and not self.busy)

    # ------------------------------------------------------------------ the worker
    def _loop(self):
        while True:
            item = self._queue.get()
            _priority, _sequence, kind, token, work, coalesce = item
            if kind is None:                     # the stop sentinel
                return
            if coalesce and self._latest.get(kind) != token:
                # A newer request of this kind is already waiting; this one has no
                # value left to add and it would only delay what is behind it.
                self.dropped += 1
                continue
            self.busy = True
            try:
                value = work(self.scope)
                error = None
            except Exception as exc:                                   # noqa: BLE001
                # One bad call must not end the worker: the next frame would then
                # never be asked for, and the panel would look wedged for reasons
                # nothing on screen could explain.
                value, error = None, exc
                self.failed += 1
            else:
                self.completed += 1
            finally:
                # Queued before the flag drops: otherwise there is a window in which
                # nothing is queued, nothing is running, and the result has not been
                # posted yet - and a caller waiting for quiet would miss it.
                self._results.put((kind, token, value, error))
                self.busy = False

    # ------------------------------------------------------------------ results
    def poll(self, handler, limit=32):
        """Hand finished calls to ``handler(kind, token, value, error)``.

        Called from the UI thread: everything here is already done, so this never
        touches the instrument and never blocks.
        """
        handled = 0
        while handled < limit:
            try:
                kind, token, value, error = self._results.get_nowait()
            except queue.Empty:
                break
            handled += 1
            try:
                handler(kind, token, value, error)
            except Exception as exc:                                   # noqa: BLE001
                # A handler that raises must not swallow the results behind it.
                logging.error("scope io: handling %s failed: %s", kind, exc)
        return handled

    def drain(self):
        """Throw away finished results, for a teardown that no longer cares."""
        while True:
            try:
                self._results.get_nowait()
            except queue.Empty:
                return
