# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Composition root. New features are supplied through objects, not the launcher."""
import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from .window import MainWindow


class WaveformApplication:
    def __init__(self, argv=None, window_factory=MainWindow):
        self.argv = list(sys.argv if argv is None else argv)
        self.qt = QApplication.instance() or QApplication(self.argv)
        self.qt.setApplicationName("ILA Waveform Reader")
        self.qt.setFont(QFont("Segoe UI", 10))
        self.qt.setStyle("Fusion")
        self.window = window_factory()

    def run(self):
        self.window.show()
        initial = Path(self.argv[1]) if len(self.argv) > 1 else Path(__file__).resolve().parents[1] / "waveform.csv"
        if initial.is_file():
            QTimer.singleShot(0, lambda: self.window.open_file(initial))
        return self.qt.exec()


def main():
    sys.exit(WaveformApplication().run())
