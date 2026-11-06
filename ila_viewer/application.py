# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Composition root. New features are supplied through objects, not the launcher."""
import sys
from pathlib import Path
from PySide6.QtCore import QLibraryInfo, QTimer, QTranslator
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from .window import MainWindow


def install_translations(app):
    """Russian texts for Qt's own buttons and dialogs (OK, Cancel, file dialogs).

    The interface is Russian; without this, standard dialog buttons stay English.
    Returns the installed translators (kept alive by the caller).
    """
    installed = []
    folder = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    for name in ("qtbase_ru", "qt_ru"):
        translator = QTranslator(app)
        if translator.load(name, folder):
            app.installTranslator(translator)
            installed.append(translator)
            break  # qt_ru includes qtbase_ru; one is enough
    return installed


class WaveformApplication:
    def __init__(self, argv=None, window_factory=MainWindow):
        self.argv = list(sys.argv if argv is None else argv)
        self.qt = QApplication.instance() or QApplication(self.argv)
        self.translators = install_translations(self.qt)
        self.qt.setApplicationName("Waveform Reader")
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
