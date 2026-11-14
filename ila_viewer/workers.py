# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Thread boundary for cancellable operations."""
from PySide6.QtCore import QThread, Signal
from .data import LoadCancelled


def dispose_result(result):
    """Release unclaimed task output before its capture storage is removed."""
    if isinstance(result, (list, tuple)):
        for item in result:
            dispose_result(item)
    elif hasattr(result, "close"):
        result.close()


class BackgroundTask(QThread):
    progress = Signal(int, int)
    phase = Signal(object, object)  # (progress.Phase, amount): a step whose total is unknown

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self.work = work
        self.result = None
        self.error = None
        self.cancelled = False

    def report(self, percent, amount, phase=None):
        """Progress callback for capture sources: ``progress(percent, rows)`` or a named phase."""
        if phase is None:
            self.progress.emit(percent, amount)
        else:
            self.phase.emit(phase, amount)

    def run(self):
        try:
            self.result = self.work(self)
        except LoadCancelled:
            self.cancelled = True
        except Exception as exc:
            self.error = str(exc)


