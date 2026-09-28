# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

import csv
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from ila_viewer.data import LoadCancelled, load_csv, parse_value
from ila_viewer.window import MainWindow
from ila_viewer.themes import STYLE
from ila_viewer.preferences import PreferencesStore


FIXTURE_CSV = Path(__file__).parent / "fixtures" / "waveform.csv"
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def capture(self, content):
        path = Path(self.directory.name) / "test.csv"
        path.write_text(content, encoding="utf-8-sig")
        capture = load_csv(path)
        self.addCleanup(capture.close)
        return capture

    def test_actual_capture_matches_every_csv_cell(self):
        path = FIXTURE_CSV
        capture = load_csv(path)
        self.addCleanup(capture.close)
        with path.open(newline="") as f:
            rows = list(csv.reader(f))[2:]
        self.assertEqual(capture.count, len(rows))
        for index, row in enumerate(rows):
            for col, sig in enumerate(capture.signals, 2):
                self.assertEqual(sig.value_at(index), int(row[col], 10 if col == 2 else 16))
        self.assertEqual([s.width for s in capture.signals], [1, 9, 9, 1, 1, 1, 1])

    def test_transitions_and_bidirectional_search(self):
        cap = self.capture("Sample in Buffer,bus[8:0],bit\nRadix - UNSIGNED,HEX,HEX\n0,001,0\n1,1,0\n2,0AC,1\n3,0ac,0\n4,001,1\n")
        sig = cap.signals[0]
        self.assertEqual(sig.starts.tolist(), [0, 2, 4])
        self.assertIsNone(sig.transition(0, -1))
        self.assertEqual(sig.transition(0, 1), 2)
        self.assertEqual(sig.transition(2, 1), 4)
        self.assertIsNone(sig.transition(4, 1))
        self.assertEqual(sig.transition(4, -1), 2)
        self.assertEqual(sig.search(1, 0, 1), (4, False))
        self.assertEqual(sig.search(1, 4, 1), (0, True))
        self.assertEqual(sig.search(1, 0, -1), (4, True))
        self.assertEqual(sig.search(172, 3, -1), (2, False))
        self.assertIsNone(sig.search(99, 0, 1))

    def test_formats_signed_wide_and_unknown(self):
        cap = self.capture("u[8:0],s[8:0],wide[79:0],unknown[3:0]\nRadix - HEX,SIGNED,HEX,HEX\n1FF,-1,FFFFFFFFFFFFFFFFFFFF,X\n100,-256,000000000000000000000,Z\n")
        u, s, wide, unknown = cap.signals
        self.assertEqual(u.format_value(u.value_at(0), "SIGNED"), "-1")
        self.assertEqual(s.format_value(s.value_at(0)), "1FF")
        self.assertEqual(u.query_value("-1", "SIGNED"), 511)
        self.assertEqual(s.query_value("0x1FF", "HEX"), -1)
        self.assertEqual(wide.value_at(0), (1 << 80) - 1)
        self.assertEqual(unknown.search("Z", 0, 1), (1, False))
        self.assertEqual(parse_value("0xAC", "HEX"), 172)
        self.assertEqual(parse_value("1010", "BINARY"), 10)
        with self.assertRaises(ValueError):
            u.query_value("200", "HEX")

    def test_multiple_windows_keep_row_order(self):
        cap = self.capture("Sample in Buffer,Sample in Window,b\nRadix - UNSIGNED,UNSIGNED,BINARY\n0,0,0\n1,1,1\n2,0,0\n3,1,1\n")
        self.assertEqual(cap.metadata["Sample in Window"].tolist(), [0, 1, 0, 1])
        self.assertEqual(cap.signals[0].starts.tolist(), [0, 1, 2, 3])

    def test_invalid_inputs_explain_location(self):
        for content, pattern in [
            ("a\n0\n", "Radix"),
            ("a\nRadix - HEX\n", "отсчётов"),
            ("a,b\nRadix - HEX,HEX\n0\n", "Строка 3"),
            ("a\nRadix - HEX\nhello\n", "Строка 3, a"),
            ("a[1:0]\nRadix - HEX\n4\n", "ширину"),
        ]:
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, pattern):
                self.capture(content)

    def test_cancel_and_disk_cleanup(self):
        path = Path(self.directory.name) / "cancel.csv"
        path.write_text("a\nRadix - HEX\n0\n")
        with self.assertRaises(LoadCancelled):
            load_csv(path, cancel=lambda: True)
        cap = load_csv(path)
        storage = Path(cap.storage.name)
        self.assertTrue(storage.exists())
        cap.close()
        self.assertFalse(storage.exists())

    def test_search_across_block_boundary(self):
        # More intervals than a search chunk: target occurs in the second chunk.
        cap = self.capture("a[19:0]\nRadix - UNSIGNED\n" + "\n".join(str(i) for i in range(262150)))
        self.assertEqual(cap.signals[0].search(262148, 0, 1), (262148, False))
        self.assertEqual(cap.signals[0].search(1, 262149, -1), (1, False))
        with self.assertRaises(LoadCancelled):
            cap.signals[0].search(1, 100, 1, lambda: True)


class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # Qt's offscreen Windows plugin does not discover system fonts itself.
        if os.name == "nt":
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/consola.ttf")
        cls.app.setFont(QFont("Segoe UI", 10))
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.window = MainWindow(preferences_store=PreferencesStore())
        self.window.resize(1200, 720)
        self.window.show()
        self.window.loaded(load_csv(FIXTURE_CSV))
        self.app.processEvents()
        self.addCleanup(self.window.close)

    def wait_for_worker(self):
        deadline = time.monotonic() + 10
        while self.window.worker and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertIsNone(self.window.worker)

    def test_navigation_search_zoom_filter_and_screenshot(self):
        v = self.window.view
        v.selected = 3  # rx_re
        v.set_cursor(0)
        QTest.keyClick(v, Qt.Key.Key_Right)
        self.assertEqual(v.cursor_a, 1)
        QTest.keyClick(v, Qt.Key.Key_Right)
        self.assertEqual(v.cursor_a, 2)
        QTest.keyClick(v, Qt.Key.Key_Left)
        self.assertEqual(v.cursor_a, 1)
        v.zoom(0.25)
        self.assertEqual(v.span, 256)
        v.set_cursor(1023)
        self.assertTrue(v.left <= 1023 < v.left + v.span)
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
                         QPoint(v.plot_left + 50, v.HEADER + 2 * v.ROW + 20))
        self.assertIsNotNone(v.cursor_b)
        v.zoom_cursors()
        self.assertLess(v.span, 1024)
        self.window.filter_edit.setText("rx_data")
        self.assertEqual(len(v.visible_signals), 1)
        v.set_cursor(0)
        self.window.search_edit.setText("0AC")
        self.window.find_value(1)
        self.wait_for_worker()
        self.assertEqual(v.current_signal.value_at(v.cursor_a), 172)
        self.window.filter_edit.clear()
        self.window.resize(640, 480)
        self.app.processEvents()
        self.assertLessEqual(self.window.width(), 640)
        self.assertGreater(v.verticalScrollBar().maximum(), 0)
        v.verticalScrollBar().setValue(v.verticalScrollBar().maximum())
        self.app.processEvents()
        self.window.resize(1280, 760)
        v.verticalScrollBar().setValue(0)
        v.set_cursor(506)
        v.set_cursor(532, True)
        v.set_range(460, 128)
        self.app.processEvents()
        folder = ARTIFACTS
        folder.mkdir(exist_ok=True)
        self.assertTrue(self.window.grab().save(str(folder / "waveform-reader.png")))

    def test_background_load_and_close(self):
        self.window.open_file(FIXTURE_CSV)
        self.wait_for_worker()
        self.assertEqual(self.window.capture.count, 1024)
        self.window.open_file(FIXTURE_CSV)
        self.window.close()
        self.wait_for_worker()
        self.assertIsNone(self.window.capture)

    def test_scrollbar_large_sample_counts(self):
        # Scrollbars use Qt's signed 32-bit range even when capture indices do not.
        self.window.capture.count = 5_000_000_000
        self.window.view.fit()
        self.assertEqual(self.window.view.horizontalScrollBar().maximum(), 0)
        self.window.view.set_range(0, 4_999_999_999)
        bar = self.window.view.horizontalScrollBar()
        self.assertLessEqual(bar.pageStep(), 2_000_000_000)
        self.window.view.set_range(0, 1000)
        bar.setValue(bar.maximum())
        self.assertEqual(self.window.view.left, 4_999_999_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
