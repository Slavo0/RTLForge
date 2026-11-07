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

    def __init__(self, work, parent=None):
        super().__init__(parent)
        self.work = work
        self.result = None
        self.error = None
        self.cancelled = False

    def run(self):
        try:
            self.result = self.work(self)
        except LoadCancelled:
            self.cancelled = True
        except Exception as exc:
            self.error = str(exc)


