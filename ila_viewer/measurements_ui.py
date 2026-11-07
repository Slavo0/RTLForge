"""Coalesced background measurements with capture-scoped result ownership."""
from PySide6.QtCore import QObject, QTimer, Signal

from .measurements import EdgeMeasurementService
from .workers import BackgroundTask


class EdgeMeasurementController(QObject):
    changed = Signal()

    def __init__(self, service=None, parent=None):
        super().__init__(parent)
        self.service = service or EdgeMeasurementService()
        self.worker = None
        self.enabled = True
        self.key = self.cached_key = self.pending = None
        self.cached = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(60)
        self.timer.timeout.connect(self._launch)

    def request(self, signal, start, end):
        if not self.enabled:
            return None
        if self.service.is_fast(signal):
            self._invalidate()
            return self.service.measure(signal, start, end)
        start, end = sorted((int(start), int(end)))
        key = (id(signal), start, end)
        if key == self.cached_key:
            self.key = key
            self.pending = None
            self.timer.stop()
            if self.worker:
                self.worker.requestInterruption()
            return self.cached
        if key != self.key:
            self.key = key
            self.pending = (key, signal, start, end)
            if self.worker:
                self.worker.requestInterruption()
            self.timer.start()
        return None

    def _invalidate(self):
        self.timer.stop()
        self.key = self.cached_key = self.pending = None
        self.cached = None
        if self.worker:
            self.worker.requestInterruption()

    def clear(self):
        """Join bounded scans before closing memory maps belonging to a capture."""
        self.enabled = False
        self._invalidate()
        if self.worker:
            task, self.worker = self.worker, None
            task.wait()
            task.deleteLater()

    def resume(self):
        self.enabled = True

    def _launch(self):
        if self.worker or self.pending is None:
            return
        key, signal, start, end = self.pending
        self.pending = None
        task = BackgroundTask(lambda t: self.service.measure(signal, start, end, t.isInterruptionRequested), self)
        self.worker = task
        task.finished.connect(lambda: self._finished(task, key))
        task.start()

    def _finished(self, task, key):
        if self.worker is not task:
            return  # clear() already joined and scheduled deletion
        self.worker = None
        if key == self.key and not task.cancelled and not task.error:
            self.cached_key, self.cached = key, task.result
            self.changed.emit()
        task.deleteLater()
        if self.pending and not self.timer.isActive():
            self._launch()
