# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Create documentation screenshots from a deterministic, synthetic capture."""

import csv
import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from ila_viewer.bits import BitExpansionService
from ila_viewer.data import load_csv
from ila_viewer.highlights import HighlightGroup
from ila_viewer.preferences import PreferencesStore
from ila_viewer.themes import STYLE
from ila_viewer.window import MainWindow


ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "docs" / "images"


class DocumentationScreenshots:
    """Own the sample, window and capture lifetime for reproducible screenshots."""

    def __init__(self, folder):
        self.folder = Path(folder)
        self.sample_path = self.folder / "demo_waveform.csv"
        self.preferences = PreferencesStore(self.folder / "preferences.json")
        self.window = MainWindow(preferences_store=self.preferences)

    def create_capture(self):
        header = ("Sample in Buffer", "Sample in Window", "TRIGGER", "clk",
                  "tx_valid", "tx_ready", "tx_we", "tx_data[7:0]",
                  "rx_valid", "rx_ready", "rx_data[7:0]", "state[2:0]")
        radices = ("UNSIGNED", "UNSIGNED", "UNSIGNED", "HEX", "HEX", "HEX",
                   "HEX", "HEX", "HEX", "HEX", "HEX", "HEX")
        with self.sample_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerow(("Radix - " + radices[0], *radices[1:]))
            for sample in range(288):
                block, step = divmod(sample, 24)
                base = 0xFE if block == 5 else (0x20 + block * 19) & 0xFF
                data = base if step < 8 else (base + 5) & 0xFF
                valid = int(4 <= step < 16)
                ready = int(not 4 <= step < 12)
                writer.writerow((sample, sample, int(sample == 132), sample % 2,
                                 valid, ready, valid and ready, f"{data:02X}",
                                 int(13 <= step < 20), int(step != 17),
                                 f"{(0x60 + block * 11 + int(step >= 17)):02X}",
                                 f"{(1 if valid and not ready else 2 if valid else 3 if step >= 16 else 0):01X}"))
        self.window.loaded(load_csv(self.sample_path))

    def add_highlights(self):
        capture = self.window.capture
        compiler = self.window.condition_compiler()
        period, phase = 2, 0
        examples = (
            ("Ожидание Ready", "tx_valid && !tx_ready", "#ffcf5b", 40, "simple"),
            ("Смена данных при stall", "tx_valid && !tx_ready -> tx_valid && tx_data != prev(tx_data)",
             "#f481b6", 53, "advanced"),
        )
        for name, expression, color, opacity, mode in examples:
            condition = (compiler.compile_pattern(expression) if mode == "advanced"
                         else compiler.compile(expression))
            service = (self.window.advanced_highlight_service if mode == "advanced"
                       else self.window.highlight_service)
            intervals = service.build(capture, condition, 0, capture.count, period, phase)
            if not intervals.count:
                raise RuntimeError(f"Условие не нашло участков: {expression}")
            self.window.highlights.add(HighlightGroup(name, expression, color, intervals,
                                                      0, capture.count, period, phase,
                                                      opacity=opacity, mode=mode))
        self.window.copy_period, self.window.copy_phase = period, phase

    @staticmethod
    def save(widget, filename):
        IMAGES.mkdir(parents=True, exist_ok=True)
        path = IMAGES / filename
        if not widget.grab().save(str(path)):
            raise RuntimeError(f"Не удалось сохранить {path}")
        print(path)

    def run(self, app):
        self.window.resize(1460, 850)
        self.window.show()
        self.create_capture()
        self.add_highlights()
        view = self.window.view
        bus = next(node for node in view.tree.root.children if node.label == "tx_data[7:0]")
        bits = BitExpansionService().build(bus.signal.source, self.window.capture.storage.name)
        self.window.capture.derived.append(bits)
        view.tree.add_bits(bus, bits.signals)
        view.refresh_rows(bus)
        view.name_width = 205
        view.value_width = 95
        view.set_range(94, 130)
        view.set_cursor(126, reveal=False)
        view.set_cursor(194, True, reveal=False)
        self.window.search_edit.setText("FE")
        app.processEvents()
        self.save(self.window, "waveform-reader.png")

        self.window.create_highlight(mode="advanced")
        advanced = self.window.active_editor
        advanced.name_edit.setText("Смена данных при stall")
        advanced.stage_edit.setPlainText("tx_valid && !tx_ready\n"
                                         "tx_valid && tx_data != prev(tx_data)")
        advanced.picker_panel.hide()
        advanced.picker_toggle.setText("Сигналы для условия ▾")
        app.processEvents()
        self.save(advanced, "advanced-highlight.png")
        advanced.reject()
        app.processEvents()

        view.selection.ids = {node.id for node in view.tree.root.children
                              if node.label in {"tx_data[7:0]", "rx_data[7:0]"}}
        self.window.copy_values()
        copy = self.window.active_editor
        copy.expression.setText("tx_valid && tx_ready && tx_we")
        copy.channels.setColumnWidth(1, 225)
        for row in range(2):
            copy.channel_radices[row].setCurrentIndex(copy.channel_radices[row].findData("HEX"))
            copy.channel_bits[row].setText("7-0")
        app.processEvents()
        self.save(copy, "conditional-copy.png")
        copy.reject()
        app.processEvents()
        self.window.close()


def main():
    app = QApplication.instance() or QApplication([])
    if os.name == "nt":
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/consola.ttf")
    app.setFont(QFont("Segoe UI", 10))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    with tempfile.TemporaryDirectory(prefix="ila_docs_") as folder:
        DocumentationScreenshots(folder).run(app)


if __name__ == "__main__":
    main()
