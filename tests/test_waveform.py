# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

import csv
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from ila_viewer.data import LoadCancelled, load_csv, parse_value
from ila_viewer.window import MainWindow
from ila_viewer.themes import STYLE
from ila_viewer.preferences import PreferencesStore
from ila_viewer.sources import AutoCaptureSource, WlfCaptureSource, QuestaWlfConverter, WlfExportService
from ila_viewer.vcd import VcdCaptureReader
from ila_viewer.tree import SignalTree


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

    def test_vcd_event_index_preserves_simulator_time_and_aliases(self):
        vcd = ("$timescale 1 ns $end\n$scope module top $end\n"
               "$var wire 8 ! data [7:0] $end\n$var wire 1 \" ready $end\n"
               "$var wire 1 \" ready_alias $end\n$upscope $end\n"
               "$enddefinitions $end\n$dumpvars\nb00000001 !\n0\"\n$end\n"
               "#0\n#10\nb00000010 !\n1\"\n#100\n0\"\n")
        path = Path(self.directory.name) / "simulation.vcd"
        path.write_text(vcd, encoding="utf-8")
        capture = VcdCaptureReader().load(path)
        self.addCleanup(capture.close)
        self.assertEqual(capture.count, 3)
        self.assertEqual(capture.times.tolist(), [0, 10, 100])
        self.assertEqual(capture.timescale, "1ns")
        self.assertEqual([s.name for s in capture.signals],
                         ["top/data[7:0]", "top/ready", "top/ready_alias"])
        self.assertEqual([capture.signals[0].value_at(i) for i in range(3)], [1, 2, 2])
        self.assertEqual([capture.signals[1].value_at(i) for i in range(3)], [0, 1, 0])
        self.assertEqual([capture.signals[2].value_at(i) for i in range(3)], [0, 1, 0])
        self.assertEqual(capture.format_time(2), "100 ns")
        self.assertAlmostEqual(capture.sample_at_time(55), 1.5)

    def test_vcd_scalar_bits_form_expandable_bus_with_original_transitions(self):
        lines = ["$timescale 1 ns $end", "$scope module top $end"]
        for bit in range(8, -1, -1):
            lines.append(f"$var wire 1 {chr(33 + 8 - bit)} rx_data [{bit}] $end")
        lines += ["$var wire 48 z status_out [47:0] $end",
                  "$var wire 1 A partial [7] $end", "$var wire 1 B partial [5] $end",
                  "$upscope $end", "$enddefinitions $end", "#0"]
        lines += [f"0{chr(33 + 8 - bit)}" for bit in range(8, -1, -1)]
        lines += ["b0 z", "0A", "0B", "#10", "1!", "1)", "#20", "x!", "#30", "0!"]
        path = Path(self.directory.name) / "split.vcd"
        path.write_text("\n".join(lines) + "\n", encoding="ascii")
        capture = VcdCaptureReader().load(path)
        self.addCleanup(capture.close)
        bus = next(sig for sig in capture.signals if sig.name == "top/rx_data[8:0]")
        self.assertEqual([bus.value_at(i) for i in range(4)], [0, 257, "X00000001", 1])
        self.assertEqual(bus.starts.tolist(), [0, 1, 2, 3])
        tree = SignalTree(capture.signals, split_buses=capture.split_buses)
        self.assertEqual([node.label for node in tree.root.children],
                         ["rx_data[8:0]", "status_out[47:0]", "partial[7]", "partial[5]"])
        node = tree.root.children[0]
        self.assertEqual([child.label for child in node.children], [f"[{i}]" for i in range(8, -1, -1)])
        self.assertEqual(node.children[0].signal.value_at(1), 1)
        self.assertEqual(node.children[-1].signal.value_at(1), 1)
        self.assertEqual(node.children[0].signal.starts.tolist(), [0, 1, 2, 3])
        self.assertEqual(len(tree.rows()), 4)
        node.expanded = True
        self.assertEqual(len(tree.rows()), 13)

    def test_vcd_unordered_split_bits_comments_and_short_vectors(self):
        lines = ["$timescale 1 ns $end"]
        lines += [f"$var wire 1 {symbol} data [{bit}] $end" for symbol, bit in zip("!\"#$", (1, 3, 0, 2))]
        lines += ["$var wire 4 % wide [3:0] $end", "$enddefinitions $end",
                  "$comment", "1! this line is not a value change", "$end",
                  "#0", "0!", "1\"", "0#", "0$", "bx %", "#10", "1#", "b1 %"]
        path = Path(self.directory.name) / "unordered.vcd"
        path.write_text("\n".join(lines) + "\n", encoding="ascii")
        capture = VcdCaptureReader().load(path)
        self.addCleanup(capture.close)
        self.assertEqual(list(capture.split_buses), ["data[3:0]"])
        bus = next(sig for sig in capture.signals if sig.name == "data[3:0]")
        self.assertEqual([bus.value_at(i) for i in range(2)], [0b1000, 0b1001])
        tree = SignalTree(capture.signals, split_buses=capture.split_buses)
        self.assertEqual([child.label for child in tree.root.children[0].children],
                         ["[3]", "[2]", "[1]", "[0]"])
        wide = next(sig for sig in capture.signals if sig.name == "wide[3:0]")
        self.assertEqual([wide.value_at(i) for i in range(2)], ["XXXX", 1])

    def test_recent_files_are_unique_strings_and_bounded(self):
        path = Path(self.directory.name) / "settings.json"
        path.write_text('{"recent_files": ["a.csv", 3, "a.csv", ""' +
                        "".join(f', "{i}.vcd"' for i in range(20)) + "]}", encoding="utf-8")
        recent = PreferencesStore(path).load().recent_files
        self.assertEqual(recent[0], "a.csv")
        self.assertEqual(len(recent), PreferencesStore.RECENT_LIMIT)
        self.assertEqual(len(set(recent)), len(recent))

    def test_wlf_source_uses_converter_and_keeps_original_path(self):
        source = Path(self.directory.name) / "vsim.wlf"
        source.write_bytes(b"WLF placeholder")
        vcd = "$timescale 1 ps $end\n$var wire 1 ! clk $end\n$enddefinitions $end\n#0\n0!\n#5\n1!\n"
        class FakeConverter:
            def convert(self, original, target, cancel):
                self.assert_source = original
                target.write_text(vcd, encoding="utf-8")
        capture = AutoCaptureSource(wlf=WlfCaptureSource(converter=FakeConverter())).load(source)
        self.addCleanup(capture.close)
        self.assertEqual(capture.path, source.resolve())
        self.assertEqual(capture.times.tolist(), [0, 5])
        self.assertEqual([capture.signals[0].value_at(i) for i in range(2)], [0, 1])

    def test_missing_wlf_converter_explains_prerequisite(self):
        with patch.dict(os.environ, {"WLF2VCD": ""}), patch("ila_viewer.sources.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "wlf2vcd"):
                QuestaWlfConverter().convert("capture.wlf", "out.vcd")

    def test_wlf_converter_recovers_only_a_temporary_copy(self):
        source = Path(self.directory.name) / "damaged.wlf"
        target = Path(self.directory.name) / "converted.vcd"
        source.write_bytes(b"original WLF bytes")
        calls = []
        def runner(command, cancel):
            calls.append(command)
            if command[0] == "wlfrecover":
                self.assertEqual(Path(command[1]).read_bytes(), source.read_bytes())
                return 0, "recovered"
            if command[1] == str(source):
                return 7, 'Unable to open WLF file "damaged.wlf"'
            self.assertEqual(Path(command[1]).read_bytes(), source.read_bytes())
            target.write_text("$enddefinitions $end\n", encoding="ascii")
            return 0, ""
        converter = QuestaWlfConverter("wlf2vcd", "wlfrecover", runner)
        converter.convert(source, target)
        self.assertEqual([command[0] for command in calls],
                         ["wlf2vcd", "wlfrecover", "wlf2vcd"])
        self.assertEqual(source.read_bytes(), b"original WLF bytes")
        self.assertFalse(list(target.parent.glob("wlf-recovery-*.wlf")))

    def test_wlf_export_persists_vcd_and_preserves_previous_file_on_error(self):
        source = Path(self.directory.name) / "source.wlf"
        destination = Path(self.directory.name) / "saved.vcd"
        source.write_bytes(b"original")
        destination.write_text("old", encoding="ascii")
        class Converter:
            fail = False
            def convert(self, original, target, cancel):
                target.write_text("new VCD", encoding="ascii")
                if self.fail:
                    raise RuntimeError("failed")
        converter = Converter()
        service = WlfExportService(converter)
        converter.fail = True
        with self.assertRaisesRegex(RuntimeError, "failed"):
            service.export(source, destination)
        self.assertEqual(destination.read_text(encoding="ascii"), "old")
        converter.fail = False
        self.assertEqual(service.export(source, destination), destination.resolve())
        self.assertEqual(destination.read_text(encoding="ascii"), "new VCD")
        self.assertEqual(source.read_bytes(), b"original")

    def test_vcd_cancel_removes_disk_index(self):
        path = Path(self.directory.name) / "cancel.vcd"
        path.write_text("$timescale 1 ns $end\n$var wire 1 ! bit $end\n"
                        "$enddefinitions $end\n#0\n0!\n#1\n1!\n", encoding="utf-8")
        with self.assertRaises(LoadCancelled):
            VcdCaptureReader().load(path, cancel=lambda: True)

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

    def test_open_vcd_draws_time_proportional_waveform(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "timed.vcd"
            path.write_text("$timescale 1 ns $end\n$var wire 1 ! bit $end\n"
                            "$enddefinitions $end\n#0\n0!\n#10\n1!\n#100\n0!\n", encoding="utf-8")
            self.window.open_file(path)
            self.wait_for_worker()
            view = self.window.view
            self.assertEqual(self.window.capture.times.tolist(), [0, 10, 100])
            self.assertAlmostEqual((view.sample_x(1) - view.sample_x(0)) / view.plot_width, 10 / 101)
            self.assertAlmostEqual(view.x_sample(view.sample_x(1)), 1)
            view.set_cursor(1)
            self.assertIn("tA: 10 ns", self.window.readout.text())
            self.assertFalse(view.viewport().grab().isNull())

    def test_copy_dialog_lists_rebuilt_bus_and_stays_within_screen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "split.vcd"
            lines = ["$timescale 1 ns $end"]
            lines += [f"$var wire 1 {chr(33 + i)} rx_data [{8 - i}] $end" for i in range(9)]
            lines += ["$var wire 1 A rx_we $end", "$var wire 1 B skid_rx_we $end",
                      "$enddefinitions $end", "#0"]
            lines += [f"0{chr(33 + i)}" for i in range(9)]
            lines += ["1A", "0B", "#10", "1!", "1)"]
            path.write_text("\n".join(lines) + "\n", encoding="ascii")
            self.window.loaded(VcdCaptureReader().load(path))
            self.window.copy_period, self.window.copy_phase = 1, 0
            self.window.copy_values()
            dialog = self.window.active_editor
            self.assertIsNotNone(dialog)
            self.assertEqual(dialog.signal_list.count(), 3)
            self.assertEqual([node.signal.short_name for node, _ in dialog.targets],
                             ["rx_data[8:0]", "rx_we", "skid_rx_we"])
            self.assertTrue(self.window.condition_compiler().compile("rx_we && rx_data == h101").matches(1))
            dialog.picker_toggle.click()
            self.app.processEvents()
            self.assertTrue(dialog.picker_panel.isVisible())
            self.assertLessEqual(dialog.height(), dialog.screen().availableGeometry().height() - 40,
                                 (dialog.minimumSizeHint(), dialog.maximumSize(),
                                  dialog.channels.minimumSizeHint(), dialog.picker_panel.minimumSizeHint()))
            self.assertTrue(dialog.findChild(QDialogButtonBox).isVisible())
            dialog.close()

    def test_highlight_dialog_lists_rebuilt_bus_and_trigger_follows_capture(self):
        self.assertTrue(self.window.trigger_action.isVisible())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "split.vcd"
            lines = ["$timescale 1 ns $end"]
            lines += [f"$var wire 1 {chr(33 + i)} rx_data [{8 - i}] $end" for i in range(9)]
            lines += ["$var wire 1 A rx_we $end", "$enddefinitions $end", "#0"]
            lines += [f"0{chr(33 + i)}" for i in range(9)] + ["1A", "#10", "1!"]
            path.write_text("\n".join(lines) + "\n", encoding="ascii")
            self.window.loaded(VcdCaptureReader().load(path))
            self.assertFalse(self.window.trigger_action.isVisible())
            self.assertFalse(self.window.clock_period_action.isEnabled())
            self.assertIn(str(path.resolve()), self.window.preferences.recent_files)
            self.window.copy_period, self.window.copy_phase = 1, 0
            self.window.create_highlight()
            dialog = self.window.active_editor
            self.assertIsNotNone(dialog)
            names = [dialog.signal_list.item(i).text() for i in range(dialog.signal_list.count())]
            self.assertEqual(names, ["rx_data[8:0]", "rx_we"])
            dialog.close()
            self.app.processEvents()
        self.window.loaded(load_csv(FIXTURE_CSV))
        self.assertTrue(self.window.trigger_action.isVisible())

    def test_find_buttons_follow_value_search_field(self):
        w = self.window
        self.assertLess(w.search_edit.geometry().right(), w.previous_button.geometry().left())
        self.assertLess(w.next_button.geometry().right(), w.width() / 2 + w.view.name_width)
        menus = [action.text() for action in w.menuBar().actions()]
        self.assertEqual(menus, ["Файл", "Вид", "Навигация", "Анализ", "Настройки", "Справка"])

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
