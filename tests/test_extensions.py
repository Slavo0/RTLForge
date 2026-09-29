# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Behavioral coverage for tree edits, bit extraction, menus and theme changes."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import json
from dataclasses import replace
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase, QContextMenuEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QDialogButtonBox, QInputDialog, QSpinBox

from ila_viewer.bits import BitExpansionService
from ila_viewer.data import LoadCancelled, load_csv
from ila_viewer.formats import FORMATS, RealSettings
from ila_viewer.tree import BusNode, GroupNode, SignalTree, ParameterNode
from ila_viewer.themes import DARK, LIGHT, SignalPalette
from ila_viewer.window import MainWindow
from ila_viewer.preferences import PreferencesStore, ViewerPreferences
from ila_viewer.copying import CopyValuesService
from ila_viewer.conditions import ConditionCompiler
from ila_viewer.copy_dialog import CopyConditionDialog
from ila_viewer.period_dialog import CopyPeriodBanner
from ila_viewer.highlights import HighlightGroup, HighlightModel, HighlightService
from ila_viewer.advanced import AdvancedHighlightService


FIXTURE_CSV = Path(__file__).parent / "fixtures" / "waveform.csv"
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def capture(self, text):
        path = Path(self.temp.name) / "capture.csv"
        path.write_text(text, encoding="utf-8")
        cap = load_csv(path)
        self.addCleanup(cap.close)
        return cap

    def test_bits_only_transition_when_the_selected_bit_changes(self):
        cap = self.capture("bus[3:0]\nRadix - HEX\n0\n1\n2\n3\n8\n9\nA\nB\n")
        result = BitExpansionService().build(cap.signals[0], cap.storage.name)
        cap.derived.append(result)
        msb, _, _, lsb = result.signals
        self.assertEqual(msb.starts.tolist(), [0, 4])
        self.assertEqual(msb.transition(0, 1), 4)
        self.assertIsNone(msb.transition(4, 1))
        self.assertEqual(msb.search(1, 0, 1), (4, False))
        self.assertEqual(lsb.starts.tolist(), list(range(8)))
        for offset, sig in enumerate(reversed(result.signals)):
            for sample, value in enumerate([0, 1, 2, 3, 8, 9, 10, 11]):
                self.assertEqual(sig.value_at(sample), (value >> offset) & 1)

    def test_ascending_nonzero_indices_and_unknown_bits(self):
        cap = self.capture("bus[4:7]\nRadix - HEX\n8\nX\nZ\n")
        result = BitExpansionService().build(cap.signals[0], cap.storage.name)
        cap.derived.append(result)
        self.assertEqual([s.short_name for s in result.signals], ["bus[4]", "bus[5]", "bus[6]", "bus[7]"])
        self.assertEqual([s.value_at(0) for s in result.signals], [1, 0, 0, 0])
        self.assertEqual([s.value_at(1) for s in result.signals], ["X"] * 4)
        self.assertEqual([s.value_at(2) for s in result.signals], ["Z"] * 4)

    def test_partial_unknown_bits_are_not_all_unknown(self):
        cap = self.capture("bus[7:0]\nRadix - HEX\nX1\n2Z\n")
        result = BitExpansionService().build(cap.signals[0], cap.storage.name)
        cap.derived.append(result)
        self.assertEqual([s.value_at(0) for s in result.signals], ["X"] * 4 + [0, 0, 0, 1])
        self.assertEqual([s.value_at(1) for s in result.signals], [0, 0, 1, 0] + ["Z"] * 4)

    def test_expansion_cancel_removes_partial_files(self):
        cap = self.capture("bus[3:0]\nRadix - HEX\n0\n1\n2\n")
        before = set(Path(cap.storage.name).iterdir())
        counter = [0]
        def cancel():
            counter[0] += 1
            return counter[0] > 3
        with self.assertRaises(LoadCancelled):
            BitExpansionService().build(cap.signals[0], cap.storage.name, cancel=cancel)
        self.assertEqual(set(Path(cap.storage.name).iterdir()), before)

    def test_reverse_changes_values_but_not_data_or_children(self):
        cap = self.capture("bus[3:0]\nRadix - HEX\n1\n2\nA\n")
        tree = SignalTree(cap.signals)
        node = tree.root.children[0]
        result = BitExpansionService().build(cap.signals[0], cap.storage.name)
        cap.derived.append(result)
        tree.add_bits(node, result.signals)
        child_ids = [c.id for c in node.children]
        node.options.reverse = True
        self.assertEqual(node.signal.name, "bus[0:3]")
        self.assertEqual(node.signal.format_value(1, "HEX"), "8")
        raw = node.signal.query_value("8", "HEX")
        self.assertEqual(raw, 1)
        self.assertEqual(node.signal.search(raw, 1, -1), (0, False))
        self.assertEqual([c.id for c in node.children], child_ids)
        self.assertEqual(cap.signals[0].value_at(0), 1)

    def test_tree_moves_groups_filter_and_delete_subtree(self):
        cap = self.capture("a,b,c\nRadix - HEX,HEX,HEX\n0,1,0\n")
        tree = SignalTree(cap.signals)
        a, b, c = tree.root.children
        self.assertTrue(tree.move(a, c, "after"))
        self.assertEqual(tree.root.children, [b, c, a])
        group = tree.create_group("RX", b)
        self.assertTrue(tree.move(c, group, "inside"))
        self.assertEqual(group.children, [b, c])
        self.assertFalse(tree.move(group, b, "inside"))
        group.expanded = False
        self.assertEqual([r.node for r in tree.rows()], [group, a])
        self.assertEqual([r.node for r in tree.rows("c")], [group])
        group.expanded = True
        self.assertEqual([r.node for r in tree.rows("c")], [group, c])
        divider = tree.create_divider("TX", group)
        tree.remove_container(group)
        self.assertEqual(tree.root.children, [divider, a])
        self.assertNotIn(b, [row.node for row in tree.rows()])
        self.assertEqual(cap.signals[1].value_at(0), 1)  # Removing a row never edits CSV data.

    def test_settings_validate_types_and_recover_corrupt_file(self):
        path = Path(self.temp.name) / "settings.json"
        store = PreferencesStore(path)
        path.write_text('{"name_width": -1, "value_width": "bad", "global_radix": "???", "fill_high": "false"}')
        prefs = store.load()
        self.assertEqual(prefs.name_width, 100)
        self.assertEqual(prefs.value_width, 90)
        self.assertEqual(prefs.global_radix, "HEX")
        self.assertFalse(prefs.fill_high)
        path.write_text("invalid json")
        self.assertEqual(store.load(), ViewerPreferences())
        self.assertIsNotNone(store.error)

    def copy_fixture(self, text="bus[8:0]\nRadix - HEX\n043\n1A6\n1FF\n0EA\n"):
        cap = self.capture(text)
        tree = SignalTree(cap.signals)
        bus = tree.root.children[0]
        bits = BitExpansionService().build(cap.signals[0], cap.storage.name)
        cap.derived.append(bits)
        tree.add_bits(bus, bits.signals)
        view = SimpleNamespace(capture=cap, cursor_a=0, cursor_b=cap.count - 1,
                               selected_nodes=bus.children[1:], preferences=ViewerPreferences(),
                               display_radix=lambda trace: "HEX")
        return bus, view, CopyValuesService()

    def test_copy_selected_byte_and_nine_bits_padding_and_radix(self):
        bus, view, service = self.copy_fixture()
        self.assertEqual(service.build(service.snapshot(view)).text, "43 A6 FF")
        view.selected_nodes = list(reversed(bus.children))
        self.assertEqual(service.build(service.snapshot(view)).text, "043 1A6 1FF")
        self.assertEqual(service.build(service.snapshot(view, "UNSIGNED")).text, "67 422 511")
        view.selected_nodes = [bus.children[-1], bus.children[-3]]
        self.assertEqual(service.build(service.snapshot(view, "BINARY")).text, "01 10 11")
        view.selected_nodes = [bus, *bus.children]
        self.assertEqual(service.build(service.snapshot(view)).text, "043 1A6 1FF")

    def test_copy_separators_changes_reversed_markers_and_cancel(self):
        bus, view, service = self.copy_fixture("bus[8:0]\nRadix - HEX\n043\n143\n1A6\n1A6\n")
        view.cursor_a, view.cursor_b = 3, 0
        for key, expected in (("space", "43 A6 A6"), ("comma", "43,A6,A6"),
                              ("text", "43A6A6"), ("tab", "43\tA6\tA6"),
                              ("newline", "43\nA6\nA6")):
            view.preferences.copy_separator = key
            self.assertEqual(service.build(service.snapshot(view)).text, expected)
        view.preferences.copy_separator = "space"
        self.assertEqual(service.build(service.snapshot(view, samples="changes")).text, "43 A6")
        with self.assertRaises(LoadCancelled):
            service.build(service.snapshot(view), cancel=lambda: True)
        service.MAX_CHARACTERS = 2
        with self.assertRaisesRegex(ValueError, "превышает"):
            service.build(service.snapshot(view))

    def test_copy_snapshot_keeps_settings_and_handles_partial_unknowns(self):
        bus, view, service = self.copy_fixture("bus[8:0]\nRadix - HEX\nX1\n043\n")
        view.selected_nodes = bus.children[-4:]
        request = service.snapshot(view)
        bus.options.reverse = True
        view.preferences.copy_separator = "comma"
        self.assertEqual(service.build(request).text, "1")
        view.selected_nodes = bus.children[1:]
        bus.options.reverse = False
        self.assertEqual(service.build(service.snapshot(view)).text, "XXXX0001")

    def test_copy_condition_and_sample_period(self):
        bus, view, service = self.copy_fixture(
            "tx_data[7:0],tx_ready,tx_we\nRadix - HEX,HEX,HEX\n"
            "FE,1,1\n00,1,0\n12,1,1\nFE,0,1\nFE,1,1\n")
        view.selected_nodes = [bus]
        compiler = ConditionCompiler(view.capture.signals)
        condition = compiler.compile("tx_ready && tx_we && tx_data == FE")
        request = service.snapshot(view, "HEX", condition)
        self.assertEqual(service.build(request).text, "FE")
        condition = compiler.compile("~tx_ready || tx_data == 12")
        self.assertEqual(service.build(service.snapshot(view, "HEX", condition)).text, "12 FE")
        self.assertEqual(service.build(service.snapshot(view, "HEX", condition, period=2, phase=0)).text, "12")
        with self.assertRaisesRegex(ValueError, "Не закрыта скобка"):
            compiler.compile("(tx_ready && tx_we")

    def test_copy_range_excludes_b_and_period_uses_marker_phase(self):
        bus, view, service = self.copy_fixture("bus[7:0]\nRadix - HEX\n00\n01\n02\n03\n04\n")
        view.cursor_a, view.cursor_b = 0, 4
        self.assertEqual(service.build(service.snapshot(view)).text, "00 01 02 03")
        view.cursor_a, view.cursor_b = 4, 0
        self.assertEqual(service.build(service.snapshot(view)).text, "01 02 03 04")
        view.cursor_a, view.cursor_b = 0, 4
        request = service.snapshot(view, period=2, phase=1)
        self.assertEqual(service.build(request).text, "01 03")

    def test_copy_without_b_per_signal_radix_bit_ranges_and_unique_sort(self):
        cap = self.capture("bus[7:0],flag\nRadix - HEX,HEX\nA1,1\nA1,0\n23,1\n11,0\n")
        tree = SignalTree(cap.signals)
        bus, flag = tree.root.children
        view = SimpleNamespace(capture=cap, cursor_a=2, cursor_b=None,
                               selected_nodes=[bus, flag], preferences=ViewerPreferences(),
                               display_radix=lambda trace: "HEX")
        service = CopyValuesService()
        options = {bus.id: {"radix": "HEX", "bits": "7-4 2-1"},
                   flag.id: {"radix": "UNSIGNED", "bits": ""}}
        request = service.snapshot(view, channel_options=options)
        self.assertEqual((request.start, request.end, request.exclude_sample), (0, 3, None))
        self.assertEqual(service.build(request).text, "28 28 09 04\n1 0 1 0")
        unique = service.snapshot(view, channel_options=options, samples="unique", sort="ascending")
        self.assertEqual(service.build(unique).text, "04 09 28\n0 1")
        self.assertEqual(service.parse_bit_ranges("7-4 2-1", list(range(7, -1, -1))), (7, 6, 5, 4, 2, 1))
        with self.assertRaisesRegex(ValueError, "вне шины"):
            service.parse_bit_ranges("9-7", list(range(7, -1, -1)))

    def test_search_wrap_can_be_disabled(self):
        cap = self.capture("sig\nRadix - HEX\n0\n1\n0\n")
        signal = cap.signals[0]
        self.assertIsNone(signal.search(1, 2, 1, wrap=False))
        self.assertEqual(signal.search(1, 2, 1, wrap=True), (1, True))

    def test_period_phase_extends_back_before_first_marker(self):
        _, view, service = self.copy_fixture("bus[7:0]\nRadix - HEX\n00\n01\n02\n03\n04\n05\n06\n07\n08\n09\n")
        view.cursor_a, view.cursor_b = 0, 9
        # Markers 1=5 and 2=8 define phase 2 modulo 3, including sample 2.
        self.assertEqual(service.build(service.snapshot(view, period=3, phase=5)).text, "02 05 08")

    def test_highlight_intervals_are_disk_backed_clipped_and_cancellable(self):
        cap = self.capture("ready,bus[7:0]\nRadix - HEX,HEX\n0,00\n1,F4\n0,00\n0,00\n0,00\n1,F4\n0,00\n0,00\n")
        self.assertEqual([ConditionCompiler(cap.signals).compile("ready").matches(i) for i in range(cap.count)],
                         [False, True, False, False, False, True, False, False])
        condition = ConditionCompiler(cap.signals).compile("ready && bus = F4")
        service = HighlightService()
        ranges = service.build(cap, condition, 0, 7, 2, 1)
        self.assertEqual(ranges.count, 2)
        self.assertEqual(list(ranges.visible(2, 6)), [(2.0, 3.0), (5.0, 6.0)])
        path = ranges.path
        model = HighlightModel()
        group = HighlightGroup("test", "ready && bus = F4", "#ffcc74", ranges, 0, 7, 2, 1)
        model.add(group)
        model.remove(group)
        self.assertFalse(path.exists())
        with self.assertRaises(LoadCancelled):
            service.build(cap, condition, 0, 7, 1, 0, cancel=lambda: True)
        self.assertFalse(list(Path(cap.storage.name).glob("highlight_*.bin")))

    def test_condition_ordering_radix_prefixes_and_previous_sample(self):
        cap = self.capture("tx_data[7:0],tx_valid\nRadix - HEX,HEX\n09,1\n0A,1\n10,0\n")
        hex_compiler = ConditionCompiler(cap.signals, {cap.signals[0].name: "HEX"})
        decimal_compiler = ConditionCompiler(cap.signals, {cap.signals[0].name: "UNSIGNED"})
        cases = (("tx_data >= 0A", [False, True, True]),
                 ("tx_data <= h0A", [True, True, False]),
                 ("h0A <= tx_data", [False, True, True]),
                 ("tx_data < b0010000", [True, True, False]),
                 ("tx_data == d10", [False, True, False]))
        for expression, expected in cases:
            with self.subTest(expression=expression):
                self.assertEqual([hex_compiler.compile(expression).matches(i) for i in range(3)], expected)
        self.assertEqual([decimal_compiler.compile("tx_data >= 10").matches(i) for i in range(3)],
                         [False, True, True])
        self.assertEqual([hex_compiler.compile("tx_data >= 10").matches(i) for i in range(3)],
                         [False, False, True])
        condition = hex_compiler.compile("tx_data != prev(tx_data) && tx_valid")
        self.assertEqual([condition.matches(i, i - 1 if i else None) for i in range(3)],
                         [False, True, False])
        self.assertTrue(hex_compiler.compile("tx_data != tx_data_prev").matches(1, 0))
        signed = self.capture("signed[7:0]\nRadix - SIGNED\n-1\n0\n")
        signed_compiler = ConditionCompiler(signed.signals, {signed.signals[0].name: "SIGNED"})
        self.assertTrue(signed_compiler.compile("signed == d-1").matches(0))

    def test_advanced_highlight_matches_event_sequences_on_period_phase(self):
        cap = self.capture("tx_valid,tx_ready,tx_data[7:0]\nRadix - HEX,HEX,HEX\n"
                           "0,0,00\n1,0,A0\n0,0,00\n1,0,A1\n0,0,00\n0,1,A2\n"
                           "0,0,00\n1,0,B0\n0,0,00\n1,0,B1\n0,0,00\n")
        compiler = ConditionCompiler(cap.signals)
        pattern = compiler.compile_pattern("tx_valid && !tx_ready -> tx_valid && tx_data != prev(tx_data)")
        service = AdvancedHighlightService()
        intervals = service.build(cap, pattern, 0, cap.count, 2, 1)
        self.addCleanup(intervals.close)
        self.assertEqual(list(intervals.visible(0, cap.count)), [(1.0, 5.0), (7.0, 11.0)])
        with self.assertRaises(LoadCancelled):
            service.build(cap, pattern, 0, cap.count, 2, 1, cancel=lambda: True)
        self.assertEqual(len(list(Path(cap.storage.name).glob("advanced_*.bin"))), 1)

    def test_style_and_highlight_palette_preferences_roundtrip_without_groups(self):
        path = Path(self.temp.name) / "preferences.json"
        store = PreferencesStore(path)
        prefs = ViewerPreferences(separate_signal_styles=True, fill_bus=True,
                                  signal_colors={"dark": {"bus": "#abcdef", "bit": "#123456"}},
                                  highlight_palette=["#ff0000", "#00ff00"],
                                  highlight_opacities=[15, 72], restore_highlight_visibility=False)
        store.save(prefs)
        loaded = store.load()
        self.assertEqual(loaded.signal_colors, prefs.signal_colors)
        self.assertEqual(loaded.highlight_palette, prefs.highlight_palette)
        self.assertEqual(loaded.highlight_opacities, [15, 72])
        self.assertFalse(loaded.restore_highlight_visibility)
        self.assertFalse(hasattr(loaded, "highlight_opacity"))
        self.assertTrue(loaded.separate_signal_styles and loaded.fill_bus)
        self.assertNotIn("groups", json.loads(path.read_text(encoding="utf-8")))

    def test_multi_move_is_atomic_and_preserves_order(self):
        cap = self.capture("a,b,c,d\nRadix - HEX,HEX,HEX,HEX\n0,1,0,1\n")
        tree = SignalTree(cap.signals)
        a, b, c, d = tree.root.children
        self.assertTrue(tree.move_many([b, c], a, "before"))
        self.assertEqual(tree.root.children, [b, c, a, d])
        group = tree.create_group_many("pair", [b, c])
        self.assertEqual(group.children, [b, c])
        before = list(tree.root.children)
        self.assertFalse(tree.move_many([group, d], b, "after"))
        self.assertEqual(tree.root.children, before)

    def test_radix_roundtrips(self):
        for key, width, bits, label in [
            ("OCTAL", 9, 511, "777"), ("ASCII", 16, 0x4142, "AB"),
            ("SIGNED_MAGNITUDE", 8, 0x85, "-5"), ("SIGNED", 8, 0xFB, "-5"),
            ("BINARY", 4, 5, "0101"), ("HEX", 9, 511, "1FF"),
        ]:
            with self.subTest(key=key):
                self.assertEqual(FORMATS.format(bits, width, key), label)
                self.assertEqual(FORMATS.parse(label, width, key), bits)
        self.assertEqual(FORMATS.format(0, 8, "ASCII"), "\\x00")
        self.assertEqual(FORMATS.parse("\\x00", 8, "ASCII"), 0)
        literal_escape = int.from_bytes(b"\\x41", "big")
        self.assertEqual(FORMATS.parse(FORMATS.format(literal_escape, 32, "ASCII"), 32, "ASCII"), literal_escape)
        fixed = RealSettings("fixed", 4, True)
        self.assertEqual(FORMATS.format(0xE8, 8, "REAL", fixed), "-1.5")
        self.assertEqual(FORMATS.parse("-1.5", 8, "REAL", fixed), 0xE8)
        self.assertEqual(FORMATS.format(0x3FC00000, 32, "REAL"), "1.5")
        self.assertEqual(FORMATS.parse("1.5", 32, "REAL"), 0x3FC00000)
        with self.assertRaises(ValueError):
            FORMATS.parse("256", 8, "UNSIGNED")

    def test_palette_is_theme_aware(self):
        palette = SignalPalette()
        self.assertGreater(len(palette.colors), 16)
        self.assertNotEqual(palette.recommended(DARK), palette.recommended(LIGHT))
        for theme in (DARK, LIGHT):
            self.assertGreaterEqual(len(palette.recommended(theme)), 6)
            for color in palette.recommended(theme):
                self.assertGreaterEqual(palette.contrast(color, theme.background), 3)
                self.assertGreaterEqual(palette.contrast(color, theme.selected), 3)
        self.assertNotIn("#da9cff", palette.recommended(DARK, [DARK.bus]))


class InteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if os.name == "nt":
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/consola.ttf")
        cls.app.setFont(QFont("Segoe UI", 10))

    def setUp(self):
        self.window = MainWindow(preferences_store=PreferencesStore())
        self.window.resize(1280, 900)
        self.window.show()
        self.window.loaded(load_csv(FIXTURE_CSV))
        self.app.processEvents()
        self.addCleanup(self.window.close)

    def wait(self):
        limit = time.monotonic() + 20
        while self.window.worker and time.monotonic() < limit:
            QTest.qWait(5)
        self.assertIsNone(self.window.worker)

    def point(self, row, x=125, fraction=.5):
        v = self.window.view
        return QPoint(x, round(v.HEADER + row * v.ROW + fraction * v.ROW - v.verticalScrollBar().value()))

    def test_shift_click_creates_b_plain_diagram_click_removes_it(self):
        v = self.window.view
        p = self.point(2, v.plot_left + 100)
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, p)
        self.assertIsNotNone(v.cursor_b)
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(2))
        self.assertIsNotNone(v.cursor_b)  # Selecting a NAME keeps the measurement.
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p)
        self.assertIsNone(v.cursor_b)
        self.assertNotIn("B:", self.window.readout.text())

    def test_waveform_click_selects_row_and_ctrl_toggles_without_shift_selection(self):
        v = self.window.view
        x = v.plot_left + 100
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(1, x))
        self.assertEqual(v.selection.ids, {v.rows[1].node.id})
        first_a = v.cursor_a
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, self.point(3, x + 20))
        self.assertEqual(v.selection.ids, {v.rows[1].node.id, v.rows[3].node.id})
        self.assertNotEqual(v.cursor_a, first_a)
        selected = set(v.selection.ids)
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, self.point(2, x + 40))
        self.assertEqual(v.selection.ids, selected)
        self.assertIsNotNone(v.cursor_b)

    def test_click_rounds_to_nearest_sample_and_period_uses_shift_for_second_marker(self):
        v = self.window.view
        v.set_range(0, 20)
        x = lambda sample: round(v.sample_x(sample))
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         self.point(0, x(4.6)))
        self.assertEqual(v.cursor_a, 5)
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
                         self.point(0, x(7.6)))
        self.assertEqual(v.cursor_b, 8)
        self.assertTrue(v.begin_period_setup())
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         self.point(0, x(2.6)))
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
                         self.point(0, x(6.6)))
        self.assertEqual(v.period_markers, [3, 7])
        self.assertEqual((v.cursor_a, v.cursor_b), (5, 8))
        v.end_period_setup()

    def test_ctrl_a_selects_names_or_full_waveform_by_last_interaction(self):
        v = self.window.view
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(2))
        QTest.keyClick(v, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual(v.selection.ids, {row.node.id for row in v.rows if row.node.signal or row.node.kind == "group"})
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         self.point(2, v.plot_left + 100))
        v.set_range(300, 100)
        original_view = (v.left, v.span)
        QTest.keyClick(v, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        self.assertEqual((v.cursor_a, v.cursor_b), (0, v.capture.count))
        self.assertEqual((v.left, v.span), original_view)
        result = CopyValuesService().build(CopyValuesService().snapshot(v))
        self.assertEqual(result.count, v.capture.count)

    def test_last_csv_folder_and_refresh_read_changed_file(self):
        w = self.window
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "capture.csv"
            path.write_text("sig\nRadix - HEX\n0\n1\n", encoding="utf-8")
            w.open_file(path)
            self.wait()
            self.assertEqual(w.capture.count, 2)
            self.assertEqual(w.preferences.last_csv_directory, str(Path(folder).resolve()))
            with patch("ila_viewer.window.QFileDialog.getOpenFileName", return_value=("", "")) as chooser:
                w.choose_file()
                self.assertEqual(chooser.call_args.args[2], str(Path(folder).resolve()))
            path.write_text("sig\nRadix - HEX\n0\n1\n2\n", encoding="utf-8")
            w.refresh_csv()
            self.wait()
            self.assertEqual(w.capture.count, 3)

    def test_copy_name_and_displayed_value_from_context_column(self):
        w, v = self.window, self.window.view
        clipboard = SimpleNamespace(text="")
        clipboard.write = lambda value: setattr(clipboard, "text", value)
        w.clipboard = clipboard
        v.selected = 2
        v.context_column = "name"
        menu = v.menus.build(v)
        next(a for a in menu.actions() if a.text() == "Copy Name").trigger()
        self.assertEqual(clipboard.text, v.row_name(v.current_node))
        menu.deleteLater()
        v.context_column = "value"
        menu = v.menus.build(v)
        next(a for a in menu.actions() if a.text() == "Copy Value").trigger()
        self.assertEqual(clipboard.text, v.row_value(v.current_node))
        menu.deleteLater()

    def test_filter_does_not_expand_collapsed_bus(self):
        v = self.window.view
        bus = v.rows[2].node
        v.toggle_node(bus)
        self.wait()
        self.assertTrue(bus.expanded)
        v.filter_signals("data")
        self.assertTrue(any(row.node.parent is bus for row in v.rows))
        v.toggle_node(bus)
        self.assertFalse(bus.expanded)
        self.assertFalse(any(row.node.parent is bus for row in v.rows))
        v.filter_signals("")
        v.filter_signals("data")
        self.assertFalse(bus.expanded)
        self.assertFalse(any(row.node.parent is bus for row in v.rows))

    def test_mouse_expansion_drag_reorder_and_groups(self):
        v = self.window.view
        bus = v.rows[2].node
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(2, 15))
        self.wait()
        self.assertEqual(len(bus.children), 9)
        self.assertEqual(v.rows[3].node.label, "[8]")
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(2, 15))
        self.assertFalse(bus.expanded)
        moving, target = v.rows[4].node, v.rows[3].node
        QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(4))
        QTest.mouseMove(v.viewport(), self.point(3, fraction=.1))
        QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(3, fraction=.1))
        self.assertEqual(v.rows[3].node, moving)
        self.assertEqual(v.rows[4].node, target)
        group = v.tree.create_group("RX", moving)
        v.refresh_rows(group)
        target_index = next(i for i, r in enumerate(v.rows) if r.node is target)
        group_index = next(i for i, r in enumerate(v.rows) if r.node is group)
        QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(target_index))
        QTest.mouseMove(v.viewport(), self.point(group_index))
        QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(group_index))
        self.assertEqual(target.parent, group)
        self.assertEqual(group.children, [moving, target])

    def test_context_actions_and_theme_screenshots(self):
        v = self.window.view
        v.selected = 2
        menu = v.menus.build(v)
        self.addCleanup(menu.deleteLater)
        actions = {a.text(): a for a in menu.actions()}
        self.assertIn("Radix", actions)
        self.assertIn("Signal Color", actions)
        self.assertFalse(actions["Go to Source Code"].isEnabled())
        radix_actions = {a.text(): a for a in actions["Radix"].menu().actions()}
        radix_actions["Octal"].trigger()
        self.assertEqual(v.display_radix(), "OCTAL")
        actions["Reverse Bit Order"].trigger()
        self.assertTrue(v.current_node.options.reverse)
        actions["Reverse Bit Order"].trigger()
        actions["Find Value…"].trigger()
        self.assertTrue(self.window.search_edit.hasFocus())
        bus = v.current_node
        v.set_radix("HEX")
        v.set_color("#ff7f96")
        v.toggle_node(bus)
        self.wait()
        self.assertEqual(v.signal_color(bus.children[0].signal).name(), "#ff7f96")
        divider = v.tree.create_divider("Приём / RX", v.tree.root.children[0])
        group = v.tree.create_group("RX DATA", bus)
        v.refresh_rows(bus)
        v.set_range(490, 50)
        v.set_cursor(506)
        v.set_cursor(520, True)
        v.name_width = 360
        artifacts = ARTIFACTS
        artifacts.mkdir(exist_ok=True)
        for theme in ("dark", "light"):
            self.window.themes.select(theme)
            if theme == "light":
                v.set_color(None)
            self.app.processEvents()
            self.assertTrue(self.window.grab().save(str(artifacts / f"waveform-{theme}-tree.png")))

    def test_close_during_bit_expansion(self):
        v = self.window.view
        v.toggle_node(v.rows[2].node)
        self.window.close()
        self.wait()
        self.assertIsNone(self.window.capture)

    def test_context_dialogs_create_group_divider_and_fixed_point(self):
        v = self.window.view
        v.selected = 2
        bus = v.current_node
        menu = v.menus.build(v)
        self.addCleanup(menu.deleteLater)
        def named_dialog(text):
            dialog = QApplication.activeModalWidget()
            self.assertIsInstance(dialog, QInputDialog)
            dialog.setTextValue(text)
            dialog.accept()
        QTimer.singleShot(0, lambda: named_dialog("Приём"))
        next(a for a in menu.actions() if a.text() == "New Group…").trigger()
        self.assertEqual(bus.parent.label, "Приём")
        self.assertEqual(v.current_node, bus.parent)
        group = v.current_node
        menu2 = v.menus.build(v)
        self.addCleanup(menu2.deleteLater)
        QTimer.singleShot(0, lambda: named_dialog("Разделитель"))
        next(a for a in menu2.actions() if a.text() == "New Divider…").trigger()
        self.assertEqual(v.current_node.kind, "divider")
        v.refresh_rows(bus)
        def real_dialog():
            dialog = QApplication.activeModalWidget()
            dialog.findChild(QSpinBox).setValue(4)
            dialog.accept()
        QTimer.singleShot(0, real_dialog)
        next(a for a in menu.actions() if a.text() == "Radix").menu().actions()[-1].trigger()
        self.assertEqual(bus.options.radix, "REAL")
        self.assertEqual(bus.options.real.fractional_bits, 4)
        self.assertEqual(bus.signal.format_value(24), "1.5")
        self.assertEqual(bus.signal.query_value("1.5"), 24)

    def test_shift_names_selects_range_without_changing_markers(self):
        v = self.window.view
        v.set_cursor(200)
        v.set_cursor(220, True)
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(1))
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, self.point(4))
        self.assertEqual(v.selection.ids, {r.node.id for r in v.rows[1:5]})
        self.assertEqual((v.cursor_a, v.cursor_b), (200, 220))
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, self.point(0))
        self.assertEqual(v.selection.ids, {r.node.id for r in v.rows[:2]})
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(6))
        self.assertEqual(v.selection.ids, {v.rows[6].node.id})
        self.assertEqual(v.cursor_b, 220)

    def test_global_radix_local_override_and_persistent_general_settings(self):
        w, v = self.window, self.window.view
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "preferences.json"
            w.preferences_store = PreferencesStore(path)
            w.apply_preferences(replace(w.preferences, global_radix="BINARY", theme="blue", fill_high=True))
            first, second = v.rows[1].node, v.rows[2].node
            self.assertEqual(v.display_radix(first.signal), "BINARY")
            self.assertEqual(v.display_radix(second.signal), "BINARY")
            v.selected = 1
            v.set_radix("OCTAL")
            w.apply_preferences(replace(w.preferences, global_radix="UNSIGNED"))
            self.assertEqual(v.display_radix(first.signal), "OCTAL")
            self.assertEqual(v.display_radix(second.signal), "UNSIGNED")
            v.set_radix("DEFAULT")
            self.assertEqual(v.display_radix(first.signal), "UNSIGNED")
            v.set_radix("OCTAL")
            v.set_column_width("name", 347)
            v.set_column_width("value", 183)
            v.tree.create_group("Not persisted", first)
            w.close()
            content = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(content), set(ViewerPreferences.__dataclass_fields__))
            other = MainWindow(preferences_store=PreferencesStore(path))
            try:
                self.assertIsNone(other.capture)
                self.assertEqual(other.view.rows, [])
                self.assertEqual(other.themes.current.key, "blue")
                self.assertTrue(other.preferences.fill_high)
                self.assertEqual((other.view.name_width, other.view.value_width), (347, 183))
                other.loaded(load_csv(FIXTURE_CSV))
                self.assertEqual(other.view.display_radix(other.view.rows[1].node.signal), "UNSIGNED")
                self.assertFalse(any(r.node.label == "Not persisted" for r in other.view.rows))
            finally:
                other.close()
            # setUp cleanup may close the window again after TemporaryDirectory exits.
            w.preferences_store = PreferencesStore()

    def test_theme_specific_bus_bit_colors_and_fill_preferences(self):
        w, v = self.window, self.window.view
        colors = {"dark": {"bus": "#ff0000", "bit": "#00ff00"},
                  "light": {"bus": "#0000ff", "bit": "#ffff00"}}
        bus = next(row.node.signal for row in v.rows if row.node.signal and row.node.signal.is_bus)
        bit = next(row.node.signal for row in v.rows if row.node.signal and not row.node.signal.is_bus)
        w.apply_preferences(replace(w.preferences, separate_signal_styles=True, fill_bus=True,
                                    fill_high=False, signal_colors=colors))
        self.assertEqual(v.signal_color(bus).name(), "#ff0000")
        self.assertEqual(v.signal_color(bit).name(), "#00ff00")
        w.apply_preferences(replace(w.preferences, theme="light"))
        self.assertEqual(v.signal_color(bus).name(), "#0000ff")
        self.assertEqual(v.signal_color(bit).name(), "#ffff00")
        w.apply_preferences(replace(w.preferences, separate_signal_styles=False))
        self.assertEqual(v.signal_color(bus).name(), v.signal_color(bit).name())
        self.assertTrue(w.preferences.fill_bus)

    def test_highlight_groups_panel_visibility_and_capture_reset(self):
        w, v = self.window, self.window.view
        v.set_cursor(0)
        v.set_cursor(20, True)
        w.copy_period, w.copy_phase = 2, 0

        w.create_highlight()
        dialog = w.active_editor
        self.assertIsNotNone(dialog)
        self.assertIsNone(dialog.color_button)
        self.assertIsNone(dialog.opacity_box)
        dialog.expression.setText("port0_rx_ready")
        dialog.accept()
        self.wait()
        self.assertEqual(len(w.highlights.groups), 1)
        group = w.highlights.groups[0]
        self.assertGreater(group.intervals.count, 0)
        self.assertTrue(w.highlight_panel.isVisible())
        self.assertEqual(v.viewportMargins().bottom(), w.highlight_panel.height())
        self.app.processEvents()
        self.assertTrue(w.highlight_panel.findChild(QCheckBox).isVisible())
        self.assertFalse(v.viewport().grab().isNull())
        ARTIFACTS.mkdir(exist_ok=True)
        self.assertTrue(w.grab().save(str(ARTIFACTS / "waveform-highlight-panel.png")))
        x, y = round(v.sample_x(10)), v.viewport().height() - 30
        highlighted = v.viewport().grab().toImage().pixelColor(x, y)
        old_path, old_id = group.intervals.path, group.id

        w.create_highlight(group)
        dialog = w.active_editor
        self.assertIsNotNone(dialog.color_button)
        self.app.processEvents()
        buttons = dialog.findChild(QDialogButtonBox)
        self.assertLessEqual(buttons.mapTo(dialog, QPoint(0, buttons.height())).y(), dialog.height())
        dialog.opacity_box.setValue(51)
        dialog.name_edit.setText("Edited")
        dialog.expression.setText("port0_rx_ready || port0_rx_re")
        dialog.accept()
        self.wait()
        group = w.highlights.groups[0]
        self.assertEqual((group.id, group.name), (old_id, "Edited"))
        self.assertEqual(group.opacity, 51)
        self.assertFalse(old_path.exists())
        self.assertTrue(w.highlight_panel.isVisible())
        self.assertEqual(v.viewportMargins().bottom(), w.highlight_panel.height())
        toggle = w.highlight_panel._chips[group.id].enabled_box
        QTest.mouseClick(toggle, Qt.MouseButton.LeftButton)
        self.app.processEvents()
        self.assertFalse(group.visible)
        self.assertTrue(w.highlight_panel.isVisible())
        self.assertTrue(w.highlight_panel.findChild(QCheckBox).isVisible())
        self.assertNotEqual(v.viewport().grab().toImage().pixelColor(x, y), highlighted)
        path = group.intervals.path
        w.highlights.remove(group)
        self.assertFalse(path.exists())
        self.assertTrue(w.highlight_panel.isVisible())
        self.assertEqual(v.viewportMargins().bottom(), w.highlight_panel.height())
        w.apply_preferences(replace(w.preferences, hide_empty_highlight_panel=True))
        self.assertEqual(v.viewportMargins().bottom(), 0)

    def test_highlight_without_b_covers_capture_and_has_own_opacity(self):
        w, v = self.window, self.window.view
        w.copy_period, w.copy_phase = 1, 0
        v.cursor_b = None
        w.preferences.highlight_opacities[1] = 63
        w.create_highlight()
        dialog = w.active_editor
        dialog.style_box.setCurrentIndex(1)
        dialog.accept()
        self.wait()
        group = w.highlights.groups[0]
        self.assertEqual((group.marker_a, group.marker_b, group.opacity), (0, w.capture.count, 63))
        self.assertTrue(w.highlight_panel.isVisible())

    def test_master_highlight_toggle_preserves_or_resets_individual_visibility(self):
        w = self.window
        from ila_viewer.highlights import HighlightIntervals
        first = HighlightGroup("one", "", "#ff0000", HighlightIntervals(), 0, 2, 1, 0)
        second = HighlightGroup("two", "", "#00ff00", HighlightIntervals(), 0, 2, 1, 0, visible=False)
        w.highlights.add(first)
        w.highlights.add(second)
        w.highlight_panel.master_box.setChecked(False)
        self.assertFalse(w.highlights.master_enabled)
        w.highlight_panel.master_box.setChecked(True)
        self.assertEqual([group.visible for group in w.highlights.groups], [True, False])
        w.apply_preferences(replace(w.preferences, restore_highlight_visibility=False))
        w.highlight_panel.master_box.setChecked(False)
        w.highlight_panel.master_box.setChecked(True)
        self.assertEqual([group.visible for group in w.highlights.groups], [True, True])

    def test_refresh_keeps_compatible_colors_and_highlights(self):
        w = self.window
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "capture.csv"
            path.write_text("data[7:0],dead\nRadix - HEX,HEX\n01,1\n02,0\n03,1\n", encoding="utf-8")
            w.open_file(path)
            self.wait()
            node = w.view.tree.root.children[0]
            node.options.color = "#ff1234"
            w.copy_period, w.copy_phase = 1, 0
            w.create_highlight()
            dialog = w.active_editor
            dialog.expression.setText("dead")
            dialog.accept()
            self.wait()
            self.assertEqual(len(w.highlights.groups), 1)
            old_id = w.highlights.groups[0].id
            w.create_highlight(mode="advanced")
            advanced_dialog = w.active_editor
            self.assertEqual(advanced_dialog.mode, "advanced")
            advanced_dialog.stage_edit.setPlainText("dead\ndead && data != prev(data)")
            advanced_dialog.validate_and_accept()
            self.wait()
            self.assertEqual(len(w.highlights.groups), 2)
            self.assertEqual(w.highlights.groups[1].mode, "advanced")
            path.write_text("data[7:0],dead\nRadix - HEX,HEX\n01,1\n02,1\n03,0\n04,1\n", encoding="utf-8")
            w.refresh_csv()
            self.wait()
            self.assertEqual(w.view.tree.root.children[0].options.color, "#ff1234")
            self.assertEqual(len(w.highlights.groups), 2)
            self.assertEqual(w.highlights.groups[0].id, old_id)
            self.assertEqual(w.highlights.groups[0].marker_b, 4)
            self.assertEqual(w.highlights.groups[1].mode, "advanced")
            path.write_text("data[7:0]\nRadix - HEX\n01\n02\n", encoding="utf-8")
            w.refresh_csv()
            self.wait()
            self.assertEqual(len(w.highlights.groups), 0)

    def test_copy_editor_allows_waveform_zoom_and_adding_signal(self):
        w, v = self.window, self.window.view
        w.copy_period = 1
        w.copy_values()
        dialog = w.active_editor
        self.assertIsNotNone(dialog)
        self.assertFalse(dialog.isModal())
        self.assertGreaterEqual(dialog.signal_list.minimumHeight(), 300)
        self.app.processEvents()
        buttons = dialog.findChild(QDialogButtonBox)
        self.assertLessEqual(buttons.mapTo(dialog, QPoint(0, buttons.height())).y(), dialog.height())
        ARTIFACTS.mkdir(exist_ok=True)
        self.assertTrue(dialog.grab().save(str(ARTIFACTS / "copy-editor.png")))
        self.assertGreater(len(dialog.targets), len(dialog.selected_targets()))
        v.set_range(0, 200)
        v.zoom_toolbar(0.5)
        self.assertEqual(v.span, 100)
        extra = next(i for i in range(len(dialog.targets))
                     if dialog.channels.item(i, 0).checkState() == Qt.CheckState.Unchecked)
        dialog.channels.item(extra, 0).setCheckState(Qt.CheckState.Checked)
        self.assertEqual(len(dialog.selected_targets()), 2)
        dialog.reject()

    def test_value_search_keeps_focus_and_respects_loop_checkbox(self):
        w, v = self.window, self.window.view
        v.selected = 2
        v.set_cursor(v.capture.count - 1)
        w.search_edit.setText("001")
        w.search_edit.setFocus()
        w.loop_search.setChecked(False)
        QTest.keyClick(w.search_edit, Qt.Key.Key_Return)
        self.wait()
        self.assertEqual(v.cursor_a, v.capture.count - 1)
        w.loop_search.setChecked(True)
        QTest.keyClick(w.search_edit, Qt.Key.Key_Return)
        self.wait()
        self.assertEqual(v.cursor_a, 0)
        self.assertTrue(w.search_edit.hasFocus())
        QTest.keyClick(w.search_edit, Qt.Key.Key_Return)
        self.wait()
        self.assertTrue(w.search_edit.hasFocus())

    def test_toolbar_zoom_centers_on_marker_interval(self):
        v = self.window.view
        v.set_range(0, 200)
        v.set_cursor(350)
        v.set_cursor(450, True)
        v.set_range(0, 200)
        v.zoom_toolbar(0.5)
        self.assertAlmostEqual(v.left + v.span / 2, 400)

    def test_independent_column_drag_double_click_and_marker_cursor(self):
        v = self.window.view
        v.set_column_width("name", 320)
        v.set_column_width("value", 140)
        y = v.HEADER // 2
        QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(320, y))
        QTest.mouseMove(v.viewport(), QPoint(370, y))
        QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(370, y))
        self.assertEqual((v.name_width, v.value_width), (370, 140))
        QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(510, y))
        QTest.mouseMove(v.viewport(), QPoint(560, y))
        QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(560, y))
        self.assertEqual((v.name_width, v.value_width), (370, 190))
        v.set_column_width("name", 650)
        QTest.mouseDClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(650, y))
        self.assertLess(v.name_width, 650)
        self.assertEqual(v.value_width, 190)
        self.assertEqual(self.window.filter_edit.width(), v.name_width + 1)
        widths = v.name_width, v.value_width
        self.window.resize(900, 600)
        self.app.processEvents()
        self.assertEqual((v.name_width, v.value_width), widths)
        v.set_range(0, 100)
        v.set_cursor(20)
        v.set_cursor(40, True)
        QTest.mouseMove(v.viewport(), self.point(2, round(v.sample_x(20))))
        self.assertEqual(v.viewport().cursor().shape(), Qt.CursorShape.SizeHorCursor)
        QTest.mouseMove(v.viewport(), self.point(2, round(v.sample_x(40))))
        self.assertEqual(v.viewport().cursor().shape(), Qt.CursorShape.SizeHorCursor)

    def test_transition_navigation_reaches_both_capture_edges(self):
        v = self.window.view
        v.selected = 3
        v.set_cursor(0)
        v.next_transition(1)
        self.assertEqual(v.cursor_a, 1)
        v.next_transition(-1)
        self.assertEqual(v.cursor_a, 0)
        v.set_cursor(v.capture.count - 2)
        v.next_transition(1)
        self.assertEqual(v.cursor_a, v.capture.count - 1)
        v.next_transition(1)
        self.assertEqual(v.cursor_a, v.capture.count - 1)

    def test_fill_high_only_and_expanded_bits(self):
        v = self.window.view
        v.set_range(0, 8)
        x = round(v.sample_x(3.3))
        def pixel(row):
            return v.viewport().grab().toImage().pixelColor(x, self.point(row).y())
        v.preferences.fill_high = False
        high_off, low_off = pixel(4), pixel(6)
        v.preferences.fill_high = True
        self.assertNotEqual(pixel(4), high_off)
        self.assertEqual(pixel(6), low_off)
        bus = v.rows[2].node
        v.toggle_node(bus)
        self.wait()
        bit_row = next(i for i, r in enumerate(v.rows) if r.node is bus.children[-1])
        v.verticalScrollBar().setValue(max(0, bit_row * v.ROW - 150))
        v.preferences.fill_high = False
        before = pixel(bit_row)
        v.preferences.fill_high = True
        self.assertNotEqual(pixel(bit_row), before)

    def test_general_appearance_dialog_and_layout_screenshot(self):
        w, v = self.window, self.window.view
        def choose():
            dialog = QApplication.activeModalWidget()
            dialog.theme_box.setCurrentIndex(dialog.theme_box.findData("dark"))
            dialog.radix_box.setCurrentIndex(dialog.radix_box.findData("HEX"))
            dialog.fill_box.setChecked(True)
            dialog.accept()
        QTimer.singleShot(0, choose)
        w.show_appearance()
        self.assertTrue(v.preferences.fill_high)
        bus = v.rows[2].node
        v.tree.create_group("RX", bus)
        v.refresh_rows(bus)
        v.toggle_node(bus)
        self.wait()
        v.set_column_width("name", 320)
        v.set_column_width("value", 130)
        v.set_range(490, 60)
        v.set_cursor(506)
        v.set_cursor(520, True)
        index = next(i for i, r in enumerate(v.rows) if r.node is bus.children[1])
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(index))
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, self.point(index + 2))
        self.app.processEvents()
        output = ARTIFACTS / "waveform-preferences-columns.png"
        self.assertTrue(w.grab().save(str(output)))

    def test_batch_radix_color_context_selection_and_drag(self):
        v = self.window.view
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(3))
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, self.point(5))
        chosen = v.selected_nodes
        def choose_from_popup():
            popup = QApplication.activePopupWidget()
            radix = next(a.menu() for a in popup.actions() if a.text() == "Radix")
            next(a for a in radix.actions() if a.text() == "Binary").trigger()
            popup.close()
        QTimer.singleShot(0, choose_from_popup)
        p = self.point(4)
        QApplication.sendEvent(v.viewport(), QContextMenuEvent(QContextMenuEvent.Reason.Mouse, p, v.viewport().mapToGlobal(p)))
        self.assertEqual(v.selected_nodes, chosen)
        self.assertTrue(all(n.options.radix == "BINARY" for n in chosen))
        self.assertEqual(v.rows[2].node.options.radix, "DEFAULT")
        menu = v.menus.build(v)
        self.addCleanup(menu.deleteLater)
        colors = next(a.menu() for a in menu.actions() if a.text() == "Signal Color")
        next(a for a in colors.actions() if a.text().startswith("#ff7f96")).trigger()
        self.assertTrue(all(n.options.color == "#ff7f96" for n in chosen))
        self.assertIsNone(v.rows[2].node.options.color)
        group = v.tree.create_group("Batch")
        v.refresh_rows()
        source_row = next(i for i, row in enumerate(v.rows) if row.node is chosen[1])
        target_row = next(i for i, row in enumerate(v.rows) if row.node is group)
        QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(source_row))
        QTest.mouseMove(v.viewport(), self.point(target_row))
        QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, self.point(target_row))
        self.assertEqual(group.children, chosen)
        self.assertEqual(v.selected_nodes, chosen)

    def test_drag_zoom_both_directions_minimum_and_marker_drag(self):
        v = self.window.view
        def gesture(start, end, modifiers=Qt.KeyboardModifier.NoModifier):
            first, last = self.point(2, round(v.sample_x(start))), self.point(2, round(v.sample_x(end)))
            QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, modifiers, first)
            QTest.mouseMove(v.viewport(), last)
            QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, modifiers, last)
        for start, end in ((20, 60), (60, 20)):
            v.set_range(0, 100)
            v.set_cursor(0)
            gesture(start, end)
            self.assertAlmostEqual(v.left, 20, delta=.2)
            self.assertAlmostEqual(v.span, 40, delta=.3)
        v.set_range(0, 4)
        v.set_cursor(0)
        gesture(1, 1.5)
        self.assertEqual((v.left, v.span), (0, 4))
        v.set_range(0, 100)
        v.set_cursor(20)
        gesture(20, 35)
        self.assertEqual(v.span, 100)
        self.assertAlmostEqual(v.cursor_a, 35, delta=1)
        v.set_cursor(40, True)
        gesture(40, 55, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(v.span, 100)
        self.assertAlmostEqual(v.cursor_b, 55, delta=1)

    def test_copy_menu_writes_injected_clipboard_and_preserves_selected_bits(self):
        w, v = self.window, self.window.view
        w.copy_period = 1
        w.copy_phase = 0
        clipboard = SimpleNamespace(text="old")
        clipboard.write = lambda text: setattr(clipboard, "text", text)
        w.clipboard = clipboard
        bus = v.rows[2].node
        v.toggle_node(bus)
        self.wait()
        v._selected = next(i for i, row in enumerate(v.rows) if row.node is bus.children[1])
        v.selection.ids = {n.id for n in bus.children[1:]}
        v.set_cursor(506)
        v.set_cursor(510, True)
        v.set_range(490, 60)
        selected = set(v.selection.ids)
        def copy_from_popup():
            popup = QApplication.activePopupWidget()
            next(a for a in popup.actions() if a.text() == "Copy Values…").trigger()
            w.active_editor.accept()
            popup.close()
        QTimer.singleShot(0, copy_from_popup)
        selected_row = next(i for i, row in enumerate(v.rows) if row.node is bus.children[1])
        p = self.point(selected_row, round(v.sample_x(508)))
        QApplication.sendEvent(v.viewport(), QContextMenuEvent(QContextMenuEvent.Reason.Mouse, p, v.viewport().mapToGlobal(p)))
        self.wait()
        expected = " ".join(f"{bus.signal.source.value_at(i) & 255:02X}" for i in range(506, 510))
        self.assertEqual(clipboard.text, expected)
        self.assertEqual(v.selection.ids, selected)
        self.assertEqual((v.cursor_a, v.cursor_b), (506, 510))

    def test_context_click_selects_row_or_preserves_existing_multi_selection(self):
        v = self.window.view
        first, second, other = (v.rows[i].node for i in (0, 1, 2))
        v.selection.ids = {first.id, second.id}
        seen = []
        class Menu:
            def build(inner, view):
                from PySide6.QtWidgets import QMenu
                seen.append({node.id for node in view.selected_nodes})
                inner.last = QMenu(view)
                return inner.last
        v.menus = Menu()
        def right_click(row, x):
            QTimer.singleShot(0, lambda: v.menus.last.close())
            point = self.point(row, x)
            QApplication.sendEvent(v.viewport(), QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, point, v.viewport().mapToGlobal(point)))
        right_click(0, v.plot_left + 40)
        self.assertEqual(seen[-1], {first.id, second.id})
        right_click(2, v.plot_left + 40)
        self.assertEqual(seen[-1], {other.id})

    def test_fill_reaches_zero_line_without_extending_below_it(self):
        v = self.window.view
        v.set_range(0, 8)
        x = round(v.sample_x(3.3))
        zero_y = v.HEADER + 4 * v.ROW + v.ROW - 12
        v.preferences.fill_high = False
        off = v.viewport().grab().toImage()
        v.preferences.fill_high = True
        on = v.viewport().grab().toImage()
        self.assertNotEqual(off.pixelColor(x, zero_y), on.pixelColor(x, zero_y))
        self.assertEqual(off.pixelColor(x, zero_y + 1), on.pixelColor(x, zero_y + 1))

    def test_copy_options_are_in_export_dialog(self):
        w = self.window
        from ila_viewer.copy_dialog import CopyOptionsDialog
        dialog = CopyOptionsDialog(w.capture.signals, w.condition_compiler(),
                                   w.copy_service.selected_targets(w.view), w.copy_service, w)
        self.assertEqual(dialog.separator_box.currentData(), "space")
        self.assertFalse(dialog.sort_box.isEnabled())
        dialog.unique_box.setChecked(True)
        self.assertTrue(dialog.sort_box.isEnabled())
        dialog.close()

    def test_copy_picker_filters_names_and_inserts_trailing_space(self):
        signals = self.window.capture.signals
        compiler = ConditionCompiler(signals)
        dialog = CopyConditionDialog(signals, compiler, self.window, full_names=True)
        dialog.name_filter.setText("tx_data")
        self.assertEqual(dialog.signal_list.count(), 1)
        signal = dialog.signal_list.item(0).data(Qt.ItemDataRole.UserRole)
        self.assertIn("/", dialog.signal_list.item(0).text())
        dialog.full_names.setChecked(False)
        self.assertNotIn("/", dialog.signal_list.item(0).text())
        dialog.insert_signal(signal)
        self.assertTrue(dialog.expression.text().endswith(" "))
        self.assertEqual(dialog.expression.cursorPosition(), len(dialog.expression.text()))
        dialog.close()

    def test_copy_period_banner_drag_confirm_and_phase(self):
        v = self.window.view
        v.set_range(0, 20)
        self.assertTrue(v.begin_period_setup())
        banner = CopyPeriodBanner(v, self.window)
        banner.show()
        first = v.period_markers[0]
        QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         self.point(0, round(v.sample_x(first))))
        QTest.mouseMove(v.viewport(), self.point(0, round(v.sample_x(3))))
        QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           self.point(0, round(v.sample_x(3))))
        self.assertEqual(v.period_markers[0], 3)
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         self.point(0, round(v.sample_x(2))))
        self.assertEqual(v.period_markers[0], 2)
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier,
                         self.point(0, round(v.sample_x(7)) + 1))
        values = []
        banner.confirmed.connect(lambda period, phase: values.append((period, phase)))
        self.assertTrue(banner.confirm_button.isEnabled(), v.period_markers)
        self.assertTrue(banner.confirm_button.isVisible(), banner.geometry())
        banner.confirm_button.click()
        self.assertEqual(values, [(5, 2)])
        self.assertEqual(v.cursor_b, None)
        banner.detach()
        banner.close()
        v.end_period_setup()

    def test_copy_period_reselect_keeps_markers_expression_and_prior_period_on_cancel(self):
        w, v = self.window, self.window.view
        v.set_cursor(1)
        v.set_cursor(12, True)
        original_markers = (v.cursor_a, v.cursor_b)
        w.copy_values("HEX")
        banner = w.copy_period_banner
        self.assertIsNotNone(banner)
        self.assertTrue(v.period_mode)
        self.assertEqual((v.cursor_a, v.cursor_b), original_markers)
        v.period_markers = [5, 8]
        banner.update_status()

        banner.confirm_button.click()
        dialog = w.active_editor
        self.assertIsInstance(dialog, CopyConditionDialog)
        dialog.expression.setText("tx_ready")
        dialog.period_button.click()
        self.assertEqual(w.copy_period, 3)
        self.assertEqual(w.copy_phase, 5)
        self.assertIsNotNone(w.copy_period_banner)
        self.assertEqual(w.pending_copy_expression, "tx_ready")
        w.cancel_period_setup()
        self.assertEqual(w.copy_period, 3)
        self.assertFalse(v.period_mode)
        self.assertEqual((v.cursor_a, v.cursor_b), original_markers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
