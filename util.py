# SPDX-License-Identifier: GPL-3.0-or-later
"""Run work off the GUI thread and hand results back to it."""
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Qt, Signal, Slot

_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="migfin")
_dispatcher = None


class _Dispatcher(QObject):
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _run(self, fn):
        fn()


def init_dispatcher():
    """Call once, on the GUI thread, after QApplication exists."""
    global _dispatcher
    _dispatcher = _Dispatcher()


def to_gui(fn):
    """Schedule fn() on the GUI thread. Safe to call from any thread."""
    if _dispatcher is not None:
        _dispatcher.call.emit(fn)


def run_async(fn, callback):
    """Run fn() on a worker thread, then callback(result, error) on the GUI thread."""

    def work():
        try:
            result, error = fn(), None
        except Exception as exc:  # reported to the UI, never swallowed silently
            result, error = None, exc
        to_gui(lambda: callback(result, error))

    try:
        _pool.submit(work)
    except RuntimeError:
        pass  # pool already shut down during exit


def shutdown_pool():
    _pool.shutdown(wait=False, cancel_futures=True)
