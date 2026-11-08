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

import numpy as np
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
from ila_viewer.cpu import CpuTopology

TEST_CPU = CpuTopology(physical=16, logical=32, name="Test CPU")
from ila_viewer.preferences import PreferencesStore, ViewerPreferences
from ila_viewer.copying import CopyValuesService
from ila_viewer.conditions import ConditionCompiler
from ila_viewer.copy_dialog import CopyConditionDialog
from ila_viewer.period_dialog import CopyPeriodBanner
from ila_viewer.highlights import HighlightGroup, HighlightModel, HighlightService
from ila_viewer.advanced import AdvancedHighlightService
from ila_viewer.bookmarks import BookmarkModel
from ila_viewer.measurements import EdgeMeasurementService, MeasurementFormatter
from ila_viewer.vcd import VcdCaptureReader
from ila_viewer.sampling import EdgeSampling, clock_candidates
from ila_viewer.timebase import capture_axis, format_ruler, format_seconds, parse_clock
from ila_viewer.spi import (PARTIAL, UNKNOWN, UNKNOWN_COMMAND, BusWidths, DecodedTrack,
                            FixedFormat, LineRef, SpiDecoder, SpiDecoderConfig, SpiExportRequest,
                            SpiExportService, SpiPins, SpiSettings, bubble_label, field_label,
                            guess_pins, line_options, overlays)
from ila_viewer.annotations import SignalAnnotations


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
        for key, expected in (("space", "43 43 A6"), ("comma", "43,43,A6"),
                              ("text", "4343A6"), ("tab", "43\t43\tA6"),
                              ("newline", "43\n43\nA6")):
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

    def test_exact_short_name_wins_over_suffix_alias(self):
        cap = self.capture("rx_we,skid_rx_we,rx_data[8:0],skid_rx_data[8:0]\n"
                           "Radix - HEX,HEX,HEX,HEX\n1,0,0FE,000\n")
        compiler = ConditionCompiler(cap.signals)
        self.assertTrue(compiler.compile("rx_we && rx_data == FE").matches(0))
        self.assertEqual(compiler.token_for(cap.signals[0]), "rx_we")
        self.assertEqual(compiler.token_for(cap.signals[2]), "rx_data[8:0]")

    def test_copy_range_excludes_b_and_period_uses_marker_phase(self):
        bus, view, service = self.copy_fixture("bus[7:0]\nRadix - HEX\n00\n01\n02\n03\n04\n")
        view.cursor_a, view.cursor_b = 0, 4
        self.assertEqual(service.build(service.snapshot(view)).text, "00 01 02 03")
        view.cursor_a, view.cursor_b = 4, 0
        self.assertEqual(service.build(service.snapshot(view)).text, "00 01 02 03")
        self.assertEqual(service.snapshot(view).exclude_sample, 4)
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

    SPI = ("sck,data[7:0]\nRadix - HEX,HEX\n"
           "0,00\n0,11\n1,11\n0,22\n1,22\n0,33\n1,33\n0,44\n0,44\n0,55\n0,55\n1,66\n0,66\n")

    def spi_view(self):
        cap = self.capture(self.SPI)
        tree = SignalTree(cap.signals)
        view = SimpleNamespace(capture=cap, cursor_a=0, cursor_b=None, selected_nodes=[tree.root.children[1]],
                               preferences=ViewerPreferences(), display_radix=lambda trace: "HEX")
        return cap, view

    def test_clock_edges_sample_gated_clock_before_or_at_edge(self):
        cap, view = self.spi_view()
        sck = cap.signals[0]
        service = CopyValuesService()
        rising_before = EdgeSampling(sck, "rising", before_edge=True)
        self.assertEqual([p[0] for p in rising_before.points(0, cap.count, cap.count)], [2, 4, 6, 11])
        self.assertEqual(service.build(service.snapshot(view, period=rising_before)).text, "11 22 33 55")
        at_edge = EdgeSampling(sck, "rising", before_edge=False)
        self.assertEqual(service.build(service.snapshot(view, period=at_edge)).text, "11 22 33 66")
        falling = EdgeSampling(sck, "falling")
        self.assertEqual(service.build(service.snapshot(view, period=falling)).text, "11 22 33 66")
        both = EdgeSampling(sck, "both")
        self.assertEqual(len(list(both.points(0, cap.count, cap.count))), 8)
        compiler = ConditionCompiler(cap.signals)
        condition = compiler.compile("data != prev(data)")
        self.assertEqual(service.build(service.snapshot(view, "HEX", condition, rising_before)).text, "22 33 55")
        view.cursor_a, view.cursor_b = 3, 11  # B is exclusive: edge 11 is not copied
        self.assertEqual(service.build(service.snapshot(view, period=rising_before)).text, "22 33")

    def test_clock_edge_highlights_do_not_paint_idle_gaps(self):
        cap, _ = self.spi_view()
        sck = cap.signals[0]
        # Values sampled before an edge belong to the cycle that ends at that edge,
        # so that cycle is painted, not the one after the edge.
        grid = EdgeSampling(sck, "rising")
        everything = HighlightService().build(cap, None, 0, cap.count, grid)
        self.assertEqual(list(everything.visible(0, cap.count)), [(0.0, 6.0), (9.0, 11.0)])
        compiler = ConditionCompiler(cap.signals)
        late = HighlightService().build(cap, compiler.compile("data == 55"), 0, cap.count, grid)
        self.assertEqual(list(late.visible(0, cap.count)), [(9.0, 11.0)])  # where data is 55
        pattern = compiler.compile_pattern("data == 11 -> data == 22")
        sequence = AdvancedHighlightService().build(cap, pattern, 0, cap.count, grid)
        self.assertEqual(list(sequence.visible(0, cap.count)), [(0.0, 4.0)])
        # Sampling at the edge reads the new values, which hold until the next edge.
        at_edge = EdgeSampling(sck, "rising", before_edge=False)
        new_values = HighlightService().build(cap, compiler.compile("data == 66"), 0, cap.count, at_edge)
        self.assertEqual(list(new_values.visible(0, cap.count)), [(11.0, 13.0)])
        for intervals in (everything, late, sequence, new_values):
            intervals.close()

    def test_clock_candidates_prefer_clock_names(self):
        cap = self.capture("valid,spi_sck,data[3:0],clk_i,CLK\nRadix - HEX,HEX,HEX,HEX,HEX\n0,0,0,0,0\n")
        names = [signal.name for signal in clock_candidates(cap.signals)]
        self.assertEqual(names, ["clk_i", "CLK", "spi_sck", "valid"])

    def test_time_text_matches_vivado_style(self):
        self.assertEqual(format_seconds(1.0031e-3, 1e-9), "1,003,100 ns")
        self.assertEqual(format_seconds(1.0031005e-3, 1e-12), "1,003,100.5 ns")
        self.assertEqual(format_seconds(2e-6, 1e-6), "2 us")
        self.assertEqual(format_ruler(1.0032e-3, 200e-9), "1,003,200 ns")
        self.assertEqual(format_ruler(1.004e-3, 2e-6), "1,004 us")
        self.assertEqual(parse_clock("100 MHz"), 10.0)
        self.assertEqual(parse_clock("25MHz"), 40.0)
        self.assertEqual(parse_clock("8 ns"), 8.0)
        self.assertEqual(parse_clock("1,5 us"), 1500.0)
        with self.assertRaises(ValueError):
            parse_clock("fast")
        cap = self.capture(self.SPI)
        axis = capture_axis(cap, 10.0)
        self.assertEqual(axis.readout(100_310), "1,003,100 ns")
        labels = [label for _, label in axis.ruler(100_300, 100_330, 600)]
        self.assertTrue(labels and all(label.endswith(" ns") and "e+" not in label for label in labels), labels)
        self.assertIsNone(capture_axis(cap, 0.0))

    def test_reverse_bit_order_reverses_expanded_rows_when_enabled(self):
        cap = self.capture("bus[3:0]\nRadix - HEX\n0\n5\n")
        tree = SignalTree(cap.signals)
        bus = tree.root.children[0]
        bits = BitExpansionService().build(cap.signals[0], cap.storage.name)
        cap.derived.append(bits)
        tree.add_bits(bus, bits.signals)
        labels = lambda **kwargs: [row.node.label for row in tree.rows(**kwargs)][1:]
        self.assertEqual(labels(reverse_bits=True), ["[3]", "[2]", "[1]", "[0]"])
        bus.options.reverse = True
        self.assertEqual(labels(reverse_bits=True), ["[0]", "[1]", "[2]", "[3]"])
        self.assertEqual(labels(reverse_bits=False), ["[3]", "[2]", "[1]", "[0]"])
        self.assertEqual([child.label for child in bus.children], ["[3]", "[2]", "[1]", "[0]"])

    def test_new_preferences_default_and_round_trip(self):
        path = Path(self.temp.name) / "settings.json"
        store = PreferencesStore(path)
        defaults = store.load()
        self.assertTrue(defaults.reverse_bit_rows)
        self.assertEqual(defaults.sample_period_ns, 0.0)
        store.save(replace(defaults, reverse_bit_rows=False, sample_period_ns=6.4, clock_edge="falling"))
        loaded = PreferencesStore(path).load()
        self.assertEqual((loaded.reverse_bit_rows, loaded.sample_period_ns, loaded.clock_edge), (False, 6.4, "falling"))
        path.write_text('{"sample_period_ns": 10, "clock_edge": "sideways"}', encoding="utf-8")
        loaded = PreferencesStore(path).load()
        self.assertEqual((loaded.sample_period_ns, loaded.clock_edge), (10.0, "rising"))

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

    def test_highlight_navigation_steps_and_wraps_like_editor_find(self):
        cap = self.capture("ready\nRadix - HEX\n0\n1\n0\n0\n1\n1\n0\n1\n")
        intervals = HighlightService().build(cap, ConditionCompiler(cap.signals).compile("ready"), 0, cap.count, 1, 0)
        self.addCleanup(intervals.close)
        self.assertEqual([intervals.interval(i) for i in range(intervals.count)], [(1, 2), (4, 6), (7, 8)])
        self.assertEqual(intervals.step(0, 1), 0)
        self.assertEqual(intervals.step(1, 1), 1)
        self.assertEqual(intervals.step(7, 1), 0)            # wraps to the first span
        self.assertIsNone(intervals.step(7, 1, wrap=False))
        self.assertEqual(intervals.step(5, -1), 1)           # inside a span: back to its start
        self.assertEqual(intervals.step(1, -1), 2)           # wraps to the last span
        self.assertEqual((intervals.index_at(5), intervals.index_at(6)), (1, None))

    def test_condition_bit_selections_and_signed_patterns(self):
        cap = self.capture("we,data[7:0],s[7:0]\nRadix - HEX,HEX,SIGNED\n1,B5,-1\n0,25,5\n")
        compiler = ConditionCompiler(cap.signals, {"s[7:0]": "SIGNED"})
        rows = lambda text: [compiler.compile(text).matches(i, i - 1 if i else None) for i in range(cap.count)]
        # B5 = 1011_0101, 25 = 0010_0101; selections keep the written bit order.
        self.assertEqual(rows("data[7,5,4,2-0] == b111101"), [True, False])
        self.assertEqual(rows("data[7,5,4,2-0] == b010101"), [False, True])
        self.assertEqual(rows("data[0-1] == b10"), [True, True])
        self.assertEqual(rows("data[7:4] > 2"), [True, False])
        self.assertEqual(rows("data[7] && we"), [True, False])
        self.assertEqual(rows("`data[7:0]`[6:5] == b01"), [True, True])
        self.assertEqual(rows("prev(data[1:0]) == 1"), [False, True])
        self.assertEqual(rows("s == hFF"), [True, False])  # bit pattern on a signed signal
        for bad in ("we[1:0] == b01", "data[8]", "data[3,3]", "data[2:0] == b1111", "s == d200"):
            with self.assertRaises(ValueError, msg=bad):
                compiler.compile(bad)

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

    def test_bookmark_model_orders_names_navigates_and_clamps(self):
        model = BookmarkModel()
        changes = []
        model.changed.connect(lambda: changes.append(1))
        late = model.add(30)
        early = model.add(5, "  start  ")
        self.assertEqual((late.name, early.name), ("M1", "start"))
        self.assertEqual([b.id for b in model.items], [early.id, late.id])
        self.assertIs(model.at(30), late)
        self.assertIs(model.next_after(5), late)
        self.assertIsNone(model.next_after(30))
        self.assertIs(model.previous_before(30), early)
        self.assertIsNone(model.previous_before(5))
        self.assertFalse(model.rename(late.id, "   "))
        self.assertTrue(model.rename(late.id, "irq"))
        self.assertEqual(model.get(late.id).name, "irq")
        self.assertEqual([b.name for b in model.visible(0, 10)], ["start"])
        model.clamp(10)
        self.assertEqual(model.get(late.id).sample, 9)
        self.assertTrue(model.remove(early.id))
        self.assertFalse(model.remove(early.id))
        model.clear()
        self.assertEqual(len(model), 0)
        self.assertEqual(model.add(1).name, "M1")
        self.assertGreaterEqual(len(changes), 7)

    def test_rising_edges_between_markers_and_unknown_states(self):
        rows = "".join(f"{i % 2},{'X' if i == 4 else (1 if i in (2, 6) else 0)}\n" for i in range(12))
        cap = self.capture("clk,flag\nRadix - HEX,BINARY\n" + rows)
        clk, flag = cap.signals
        service = EdgeMeasurementService()
        # clk = 0,1,0,1,...: rising edges at odd samples; range is (start, end].
        full = service.measure(clk, 0, cap.count)
        self.assertEqual((full.rising, full.falling, full.first_rise, full.last_rise), (6, 5, 1, 11))
        self.assertEqual(full.period_samples, 2)
        self.assertEqual(service.measure(clk, 7, 3).rising, 2)
        self.assertEqual(service.measure(clk, 3, 4).rising, 0)
        self.assertEqual(service.measure(clk, 5, 5).rising, 0)
        # flag = 0,0,1,0,X,0,1,0,...: X -> 0 -> 1 counts once, X itself is never an edge.
        mixed = service.measure(flag, 0, cap.count)
        self.assertEqual((mixed.rising, mixed.first_rise, mixed.last_rise), (2, 2, 6))
        self.assertEqual(MeasurementFormatter().parts(full, cap, 10.0, "clk"),
                         ["↑clk A…B: 6", "T≈ 2 отсч.", "T≈ 20 ns", "f≈ 50 MHz"])
        bus = self.capture("bus[3:0]\nRadix - HEX\n0\n1\n")
        self.assertIsNone(service.measure(bus.signals[0], 0, 2))

    def test_vcd_edge_period_uses_simulator_time(self):
        lines = ["$timescale 1 ns $end", "$var wire 1 ! clk $end", '$var wire 1 " en $end',
                 "$enddefinitions $end", "#0", "0!", '0"']
        for step in range(1, 9):
            lines += [f"#{step * 5}", f"{step % 2}!"]
            if step == 3:
                lines.append('1"')
        path = Path(self.temp.name) / "clock.vcd"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        cap = VcdCaptureReader().load(path)
        self.addCleanup(cap.close)
        clk = next(s for s in cap.signals if s.short_name == "clk")
        measurement = EdgeMeasurementService().measure(clk, 0, cap.count)
        self.assertEqual(measurement.rising, 4)
        self.assertEqual(MeasurementFormatter().parts(measurement, cap, 0, "clk"),
                         ["↑clk A…B: 4", "T≈ 10 ns", "f≈ 100 MHz"])


QUAD = (3, 2, 1, 0)


def spi_transfers(lines, data):
    """DQ values per transfer; ``lines`` lists the DQ index of each bit, most significant first."""
    width = len(lines)
    values = []
    for byte in data:
        for step in range(8 // width):
            chunk = (byte >> (8 - width * (step + 1))) & ((1 << width) - 1)
            values.append(sum(1 << line for i, line in enumerate(lines) if chunk >> (width - 1 - i) & 1))
    return values


def spi_csv(frames, idle=3, unknown_dq=False):
    """Mode-0 SPI frames as Vivado CSV: 2 samples per clock half, data stable around each rising edge.

    Frame items: ``(lines, bytes)`` single rate, ``("dtr", lines, bytes)`` on both edges,
    ``("dummy", clocks)`` with DQ low (``"X"`` when ``unknown_dq``).
    """
    rows = []
    for frame in frames:
        rows += [(1, 0, 0)] * idle
        for item in frame:
            if item[0] == "dummy":
                rows += ([(0, 0, "X")] * 2 + [(0, 1, "X")] * 2 if unknown_dq else
                         [(0, 0, 0)] * 2 + [(0, 1, 0)] * 2) * item[1]
            elif item[0] == "dtr":
                values = spi_transfers(item[1], item[2])
                for even, odd in zip(values[::2], values[1::2]):
                    rows += [(0, 0, even)] * 2 + [(0, 1, odd)] * 2
            else:
                for value in spi_transfers(item[0], item[1]):
                    rows += [(0, 0, value)] * 2 + [(0, 1, value)] * 2
        rows += [(0, 0, 0)] * 2
    rows += [(1, 0, 0)] * idle
    body = "".join(f"{cs},{sck},{dq if isinstance(dq, str) else format(dq, 'X')}\n" for cs, sck, dq in rows)
    return "cs_n,sck,dq[3:0]\nRadix - BINARY,BINARY,HEX\n" + body


def spi_pins():
    return SpiPins(LineRef("sck"), LineRef("cs_n"), True,
                   tuple(LineRef("dq[3:0]", index, f"dq[{index}]") for index in range(4)))


FLASH_FRAMES = [
    [((0,), [0x06])],
    [((0,), [0xEB]), (QUAD, [0x12, 0x34, 0x56]), ("dummy", 10), (QUAD, [0xDE, 0xAD, 0xBE, 0xEF])],
    [((0,), [0x03, 0x00, 0x10, 0x00]), ((1,), [0x11, 0x22])],
    [((0,), [0x02, 0x00, 0x00, 0x08, 0xA5, 0x5A])],
]


class VectorizedPathTests(unittest.TestCase):
    """The numpy paths must give exactly what the original per-row/per-point code gave."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def write(self, name, text, newline="\n"):
        path = Path(self.temp.name) / name
        with path.open("w", encoding="utf-8", newline=newline) as stream:
            stream.write(text)
        return path

    def assertSameCapture(self, actual, expected):
        self.assertEqual(actual.count, expected.count)
        self.assertEqual([s.name for s in actual.signals], [s.name for s in expected.signals])
        for got, want in zip(actual.signals, expected.signals):
            self.assertEqual((got.width, got.values), (want.width, want.values), got.name)
            np.testing.assert_array_equal(got.starts, want.starts, err_msg=got.name)
            np.testing.assert_array_equal(got.codes, want.codes, err_msg=got.name)
        for name, data in expected.metadata.items():
            np.testing.assert_array_equal(actual.metadata[name], data)

    def generated_csv(self, rows=3000, newline="\n"):
        rng = np.random.default_rng(7)
        lines = ["Sample in Buffer,Sample in Window,TRIGGER,data[7:0],wide,s[7:0],bit,x,m[15:0]",
                 "Radix - UNSIGNED,UNSIGNED,UNSIGNED,HEX,HEX,SIGNED,BINARY,HEX,HEX"]
        for i in range(rows):
            data = "XX" if i % 97 < 3 else f"{int(rng.integers(0, 4)) * 0x41:02X}"
            wide = f"{(1 << 100) + i // 50:X}" if i % 400 else "zzzz"
            lines.append(f"{i},{i % 512},{int(i == rows // 2)},{data},{wide},{int(rng.integers(-3, 3))},"
                         f"{(i // 3) % 2},{'0a' if i % 10 else '0A'},{'XXXX' if i % 701 == 5 else f'{(i // 3) * 7 & 0xFFFF:04X}'}")
            if i == 100:
                lines.append("")      # blank line inside the body
            if i == 200:
                lines.append(",,,,,,,,")  # a row of empty cells
        return "\n".join(lines) + "\n"

    def test_vectorized_csv_matches_row_by_row_reader(self):
        from tests.reference import reference_load_csv
        cases = [FIXTURE_CSV, self.write("gen.csv", self.generated_csv()),
                 self.write("crlf.csv", self.generated_csv(500), newline="\r\n"),
                 self.write("quoted.csv", 'Sample in Buffer,"q[3:0]"\nRadix - UNSIGNED,HEX\n0,"1"\n1,"2"\n2,2\n'),
                 self.write("no_eol.csv", "a,b[1:0]\nRadix - HEX,HEX\n1,0\n0,3"),
                 self.write("single.csv", "a\nRadix - HEX\n0\n1\n1\n\n0\n")]
        for path in cases:
            with self.subTest(path=path.name):
                actual, expected = load_csv(path), reference_load_csv(path)
                self.addCleanup(actual.close)
                self.addCleanup(expected.close)
                self.assertSameCapture(actual, expected)

    def test_parallel_csv_merges_chunks_like_one_process(self):
        from ila_viewer import data
        from ila_viewer.cpu import WorkerPool
        path = self.write("parallel.csv", self.generated_csv(20000))
        pool = WorkerPool(3)
        self.addCleanup(pool.shutdown)
        with patch.object(data, "MIN_PARALLEL_CHUNK", 64 << 10):
            self.assertEqual(pool.workers_for(path.stat().st_size, data.MIN_PARALLEL_CHUNK), 3)
            actual = load_csv(path, pool=pool)
        self.addCleanup(actual.close)
        expected = load_csv(path)
        self.addCleanup(expected.close)
        self.assertSameCapture(actual, expected)
        self.assertFalse(list(Path(actual.storage.name).glob("part*")))
        bad = self.write("bad.csv", self.generated_csv(20000).replace("\n15000,", "\n15000,1,"))
        with patch.object(data, "MIN_PARALLEL_CHUNK", 64 << 10):
            with self.assertRaisesRegex(ValueError, r"Строка 15005: ожидалось 9 столбцов"):
                load_csv(bad, pool=pool)
        with self.assertRaisesRegex(ValueError, r"Строка 15005: ожидалось 9 столбцов"):
            load_csv(bad)

    def test_csv_errors_keep_physical_line_numbers(self):
        path = self.write("bad.csv", "a[3:0],b\nRadix - HEX,HEX\n1,0\n\n12,1\n")
        with self.assertRaisesRegex(ValueError, r"Строка 5, a\[3:0\]: значение 12 выходит за ширину 4 бит"):
            load_csv(path)

    def test_cpu_recommendation_uses_physical_cores(self):
        from ila_viewer.cpu import CpuTopology, WorkerPool
        topology = CpuTopology(physical=16, logical=32, name="AMD Ryzen 9 9950X3D")
        self.assertEqual(topology.recommended_workers(), 15)
        self.assertEqual(topology.recommended_workers(available=8), 7)
        self.assertEqual(CpuTopology(2, 4).recommended_workers(), 1)
        self.assertIn("16 ядер / 32 потоков", topology.describe())
        pool = WorkerPool(15)
        self.assertEqual(pool.workers_for(10 << 20, 16 << 20), 1)   # small files stay in-process
        self.assertEqual(pool.workers_for(100 << 20, 16 << 20), 7)
        self.assertEqual(pool.workers_for(1 << 30, 16 << 20), 15)

    def condition_capture(self):
        text = self.generated_csv(1500)
        cap = load_csv(self.write("cond.csv", text))
        self.addCleanup(cap.close)
        return cap

    EXPRESSIONS = ("data == 41", "data != 82", "data > 41", "41 < data", "data >= hC3",
                   "data[7:4] == 4", "data[0] && bit", "!bit || data == 0", "wide > 1",
                   "s < 0", "s == hFF", "s == d-2", "s[7] == 1", "bit != prev(bit)",
                   "prev(data) == data", "data == prev(data) && !(s >= 1)", "x == 0A", "(data == 41) == 1",
                   "data == wide", "data < s", "TRIGGER", "bit == true", "data != prev(wide)")

    def test_vector_condition_matches_scalar_condition(self):
        cap = self.condition_capture()
        radix = {"s[7:0]": "SIGNED"}
        compiler = ConditionCompiler(cap.signals, radix)
        at = np.arange(cap.count, dtype=np.int64)
        previous = at - 1
        for expression in self.EXPRESSIONS:
            with self.subTest(expression=expression):
                condition = compiler.compile(expression)
                expected = [condition.matches(i, i - 1 if i else None) for i in range(cap.count)]
                self.assertEqual(condition.evaluate(at, previous).tolist(), expected)

    def test_vector_highlights_match_per_point_builders(self):
        from tests.reference import reference_advanced, reference_highlight
        from ila_viewer.sampling import PeriodicSampling
        cap = self.condition_capture()
        compiler = ConditionCompiler(cap.signals)
        bit = next(s for s in cap.signals if s.name == "bit")
        grids = [PeriodicSampling(1), PeriodicSampling(3, 2), EdgeSampling(bit, "rising"),
                 EdgeSampling(bit, "both", before_edge=False), EdgeSampling(bit, "falling")]
        for grid in grids:
            for a, b in ((0, cap.count - 1), (cap.count - 1, 5), (400, 401), (700, 700)):
                for expression in ("data == 41", "bit != prev(bit)", "data > 41 && !bit"):
                    with self.subTest(grid=grid.describe(), a=a, b=b, expression=expression):
                        condition = compiler.compile(expression)
                        result = HighlightService().build(cap, condition, a, b, grid)
                        self.addCleanup(result.close)
                        self.assertEqual([result.interval(i) for i in range(result.count)],
                                         reference_highlight(cap, condition, a, b, grid))
                pattern = compiler.compile_pattern("data == 41 -> data != 41 -> bit")
                with self.subTest(grid=grid.describe(), a=a, b=b, pattern=True):
                    result = AdvancedHighlightService().build(cap, pattern, 0, cap.count - 1, grid)
                    self.addCleanup(result.close)
                    self.assertEqual([result.interval(i) for i in range(result.count)],
                                     reference_advanced(cap, pattern, 0, cap.count - 1, grid))

    def test_point_chunks_match_points_across_chunk_borders(self):
        from ila_viewer import sampling
        from ila_viewer.sampling import PeriodicSampling
        cap = self.condition_capture()
        bit = next(s for s in cap.signals if s.name == "bit")
        with patch.object(sampling, "CHUNK_POINTS", 7), patch.object(EdgeSampling, "CHUNK", 5):
            for grid in (PeriodicSampling(2, 1), EdgeSampling(bit), EdgeSampling(bit, "both", False)):
                for lower, upper in ((0, cap.count), (13, 600), (601, 602)):
                    with self.subTest(grid=grid.describe(), lower=lower, upper=upper):
                        expected = [tuple(-1 if v is None else v for v in point)
                                    for point in grid.points(lower, upper, cap.count)]
                        chunks = list(grid.point_chunks(lower, upper, cap.count))
                        actual = [tuple(map(int, row)) for chunk in chunks for row in zip(*chunk)]
                        self.assertEqual(actual, expected)


class SpiDecoderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def capture(self, frames, **options):
        path = Path(self.temp.name) / f"spi{len(list(Path(self.temp.name).iterdir()))}.csv"
        path.write_text(spi_csv(frames, **options), encoding="utf-8")
        cap = load_csv(path)
        self.addCleanup(cap.close)
        return cap

    def decode(self, cap, settings=None, pins=None):
        result = SpiDecoder().decode(cap, SpiDecoderConfig(pins or spi_pins(), settings or SpiSettings()))
        self.addCleanup(result.close)
        return result

    @staticmethod
    def labels(result, frame, lane=0):
        return [field_label(result, record) for record in result.frame_fields(frame, lane)]

    def test_command_table_decodes_mixed_formats_per_frame(self):
        cap = self.capture(FLASH_FRAMES)
        before = [(np.array(sig.starts), np.array(sig.codes)) for sig in cap.signals]
        result = self.decode(cap)
        self.assertEqual(result.frame_count, 4)
        self.assertEqual(self.labels(result, 0), ["CMD 06 WRITE ENABLE"])
        self.assertEqual(self.labels(result, 1), ["CMD EB QUAD I/O FAST READ", "ADDR 123456", "dummy 10",
                                                  "RD DE", "RD AD", "RD BE", "RD EF"])
        self.assertEqual(result.frame_format(1), "1-4-4")
        self.assertEqual(self.labels(result, 2)[1:], ["ADDR 001000", "RD 11", "RD 22"])
        self.assertEqual(result.frame_format(2), "1-1-1")
        self.assertEqual(self.labels(result, 3)[2:], ["WR A5", "WR 5A"])
        self.assertEqual(int(result.frames[1]["address"]), 0x123456)
        self.assertEqual(list(result.frames["data_bytes"]), [0, 4, 2, 2])
        self.assertFalse(any(int(flags) for flags in result.frames["flags"]))
        # Decoding reads the indices only; acquired samples stay untouched.
        for (starts, codes), sig in zip(before, cap.signals):
            self.assertTrue(np.array_equal(starts, sig.starts))
            self.assertTrue(np.array_equal(codes, sig.codes))
        lane = result.lanes[0]
        self.assertTrue(np.all(lane["start"][1:] >= lane["end"][:-1]))  # ordered, not overlapping

    def test_spi_query_finds_commands_addresses_and_byte_sequences(self):
        from ila_viewer.spi import SpiQuery
        cap = self.capture(FLASH_FRAMES)
        result = self.decode(cap)
        frames = lambda text: SpiQuery.parse(text).frames(result).tolist()
        self.assertEqual(frames("cmd EB"), [1])
        self.assertEqual(frames("cmd 0x03"), [2])
        self.assertEqual(frames("@1000"), [2])
        self.assertEqual(frames("@1000-123456"), [1, 2])
        self.assertEqual(frames("DE AD BE"), [1])
        self.assertEqual(frames("deadbeef"), [1])          # one token, split into bytes
        self.assertEqual(frames("12 34 56 DE"), [1])       # address bytes, then data
        self.assertEqual(frames("EB 12"), [1])             # command, then the first address byte
        self.assertEqual(frames("data 12"), [])            # 12 is an address byte here
        self.assertEqual(frames("EF 03"), [])              # never across two transactions
        self.assertEqual(frames("AD DE"), [])
        self.assertEqual(SpiQuery.parse("@1000-FFF").values, (0xFFF, 0x1000))
        for bad in ("", "cmd", "zz", "@1-2-3"):
            with self.assertRaises(ValueError, msg=bad):
                SpiQuery.parse(bad)
        track = DecodedTrack(result, 0, "SPI")
        data = result.frame_data(1)
        query = SpiQuery.parse("AD")
        self.assertEqual(track.search(query, 0, 1), (int(data[1]["start"]), False))
        self.assertEqual(track.search(query, int(data[1]["start"]), 1), (int(data[1]["start"]), True))
        self.assertIsNone(track.search(query, int(data[1]["start"]), 1, wrap=False))
        self.assertEqual(track.search(query, cap.count, -1), (int(data[1]["start"]), False))
        # Chunk borders: a sequence that spans two chunks is still found once.
        lane = result.lanes[0]
        expected = SpiQuery.parse("DE AD BE EF").field_matches(lane).tolist()
        self.assertEqual(SpiQuery.parse("DE AD BE EF").field_matches(lane, chunk=2).tolist(), expected)
        self.assertEqual(SpiQuery.parse("AD").field_matches(lane, chunk=1).tolist(),
                         SpiQuery.parse("AD").field_matches(lane).tolist())

    def test_manual_dummy_and_fixed_formats_are_used_exactly(self):
        cap = self.capture([[((0,), [0xEB]), (QUAD, [0x00, 0x01, 0x00]), ("dummy", 6), (QUAD, [0x42, 0x43])]])
        wrong = self.decode(cap)  # the table default is 10 dummy clocks: the data shifts and ends short
        self.assertNotEqual(self.labels(wrong, 0)[3:], ["RD 42", "RD 43"])
        right = self.decode(cap, SpiSettings(dummy_cycles=6))
        self.assertEqual(self.labels(right, 0)[2:], ["dummy 6", "RD 42", "RD 43"])
        fixed = self.decode(cap, SpiSettings(mode="fixed", fixed=FixedFormat(BusWidths.parse("1-4-4"), 3, 6)))
        self.assertEqual(self.labels(fixed, 0), ["CMD EB QUAD I/O FAST READ", "ADDR 000100", "dummy 6",
                                                 "RD 42", "RD 43"])
        # Continuous read without an opcode: 0-4-4.
        xip = self.capture([[(QUAD, [0x00, 0x20, 0x00]), ("dummy", 4), (QUAD, [0x99])]])
        result = self.decode(xip, SpiSettings(mode="fixed", fixed=FixedFormat(BusWidths.parse("0-4-4"), 3, 4)))
        self.assertEqual(self.labels(result, 0), ["ADDR 002000", "dummy 4", "RD 99"])
        self.assertEqual(int(result.frames[0]["opcode"]), -1)
        with self.assertRaises(ValueError):
            BusWidths.parse("1-3-4")

    def test_quad_protocol_dtr_raw_lsb_unknown_and_partial(self):
        cap = self.capture([[(QUAD, [0x0B, 0x00, 0x00, 0x40]), ("dummy", 10), (QUAD, [0x5A])]])
        result = self.decode(cap, SpiSettings(protocol="quad"))
        self.assertEqual(self.labels(result, 0), ["CMD 0B FAST READ", "ADDR 000040", "dummy 10", "RD 5A"])
        self.assertEqual(result.frame_format(0), "4-4-4")
        dtr = self.capture([[((0,), [0xED]), ("dtr", QUAD, [0xAB, 0xCD, 0xEF]), ("dummy", 8),
                             ("dtr", QUAD, [0x01, 0x02])]])
        result = self.decode(dtr)
        self.assertEqual(self.labels(result, 0)[1:], ["ADDR ABCDEF", "dummy 8", "RD 01", "RD 02"])
        self.assertIn("DTR", result.frame_format(0))
        raw = self.capture([[((0,), [0x01, 0x80])]])
        result = self.decode(raw, SpiSettings(mode="raw", msb_first=False))
        self.assertEqual(self.labels(result, 0), ["MOSI 80", "MOSI 01"])
        self.assertEqual(self.labels(result, 0, lane=1), ["MISO 00", "MISO 00"])
        self.assertEqual(result.lane_count, 2)
        odd = self.capture([[((0,), [0x9F]), ("dummy", 2)]], unknown_dq=True)
        result = self.decode(odd)
        fields = result.frame_fields(0)
        self.assertEqual(int(fields[1]["flags"]), UNKNOWN | PARTIAL)  # 2 X bits of a byte
        self.assertTrue(field_label(result, fields[1]).startswith("RD X"))
        unknown = self.decode(self.capture([[((0,), [0x77, 0x01])]]))
        self.assertTrue(int(unknown.frames[0]["flags"]) & UNKNOWN_COMMAND)
        self.assertEqual(self.labels(unknown, 0), ["CMD 77 нет в таблице", "MOSI 01"])

    def test_export_stream_frames_range_binary_and_cancel_cleanup(self):
        cap = self.capture(FLASH_FRAMES)
        result = self.decode(cap)
        service = SpiExportService()
        self.assertEqual(service.build(SpiExportRequest(result)).text, "DE AD BE EF 11 22 A5 5A")
        frames = service.build(SpiExportRequest(result, frames=(1, 3), layout="frames", separator=",", prefix=True))
        self.assertEqual(frames.text, "#2 EB @123456: 0xDE,0xAD,0xBE,0xEF\n#4 02 @000008: 0xA5,0x5A")
        read = service.build(SpiExportRequest(result, content="read", radix="DEC", separator=" "))
        self.assertEqual(read.text, "222 173 190 239 17 34")
        fields = result.frame_fields(1)
        # A…B: the earlier boundary is included, the later one excluded.
        between = service.build(SpiExportRequest(result, start=int(fields[3]["start"]) + 1,
                                                 end=int(fields[6]["start"]) + 1))
        self.assertEqual(between.text, "AD BE EF")
        everything = service.build(SpiExportRequest(result, frames=(1,), content="all"))
        self.assertEqual(everything.text, "EB 123456 DE AD BE EF")
        target = Path(self.temp.name) / "data.bin"
        self.assertEqual(service.write_binary(SpiExportRequest(result, frames=(1, 2)), target), 6)
        self.assertEqual(target.read_bytes(), bytes([0xDE, 0xAD, 0xBE, 0xEF, 0x11, 0x22]))
        with self.assertRaises(LoadCancelled):
            service.write_binary(SpiExportRequest(result), Path(self.temp.name) / "x.bin", cancel=lambda: True)
        self.assertFalse((Path(self.temp.name) / "x.bin.part").exists())
        storage = Path(cap.storage.name)
        existing = set(storage.iterdir())
        with self.assertRaises(LoadCancelled):
            SpiDecoder().decode(cap, SpiDecoderConfig(spi_pins()), cancel=lambda: True)
        self.assertEqual(set(storage.iterdir()), existing)
        paths = list(result.paths)
        result.close()
        result.close()
        self.assertFalse(any(path.exists() for path in paths))

    def test_track_navigation_labels_and_line_choices(self):
        cap = self.capture(FLASH_FRAMES)
        result = self.decode(cap)
        track = DecodedTrack(result, 0, "SPI")
        fields = result.lanes[0]
        first = int(fields[0]["start"])
        self.assertEqual(track.transition(0, 1), first)
        self.assertEqual(track.transition(first, 1), int(fields[0]["end"]))
        self.assertEqual(track.transition(int(fields[2]["start"]), -1), int(fields[1]["start"]))
        self.assertEqual(track.label_at(first), "CMD 06 WRITE ENABLE")
        self.assertEqual(track.label_at(0), "")
        self.assertEqual(result.frame_at(int(fields[2]["start"])), 1)
        self.assertEqual(track.window(int(fields[2]["start"]), int(fields[4]["start"])), (2, 4))
        self.assertTrue(list(track.busy(np.linspace(0, cap.count, 50))))
        labels = [option.text for option in line_options(cap.signals)]
        self.assertEqual(labels, ["cs_n", "sck", "dq[3]", "dq[2]", "dq[1]", "dq[0]"])
        self.assertEqual(guess_pins(cap.signals), spi_pins())
        with self.assertRaises(ValueError):
            SpiDecoder().decode(cap, SpiDecoderConfig(SpiPins(LineRef("missing_sck"))))


class SpiModeTrackingTests(unittest.TestCase):
    """Commands of the table change protocol, dummy and XIP; chip files load exactly."""

    setUp, capture, decode = SpiDecoderTests.setUp, SpiDecoderTests.capture, SpiDecoderTests.decode
    labels = staticmethod(SpiDecoderTests.labels)

    def micron(self, **changes):
        from ila_viewer import spi_profiles
        sets = {item.name: item for item in spi_profiles.builtin()}
        return SpiSettings(commands=sets["Micron MT25QL128ABA"].commands, profile="Micron MT25QL128ABA", **changes)

    def test_enter_and_reset_quad_mode_switch_the_command_lines(self):
        from ila_viewer.spi import STATE_CHANGE, decode_summary
        cap = self.capture([
            [((0,), [0x35])],
            [(QUAD, [0x0B, 0x00, 0x00, 0x40]), ("dummy", 10), (QUAD, [0x5A])],
            [(QUAD, [0xF5])],
            [((0,), [0x03, 0x00, 0x10, 0x00]), ((1,), [0x11])]])
        result = self.decode(cap)
        self.assertEqual([result.frame_format(i) for i in range(4)], ["1-0-0", "4-4-4", "4-0-0", "1-1-1"])
        self.assertEqual(self.labels(result, 1), ["CMD 0B FAST READ", "ADDR 000040", "dummy 10", "RD 5A"])
        self.assertEqual([bool(f & STATE_CHANGE) for f in result.frames["flags"]], [True, False, True, False])
        self.assertIn("смен режима: 2 (в конце: Extended)", decode_summary(result))
        # The same capture without following the commands stays in the starting protocol.
        fixed = self.decode(cap, SpiSettings(track_state=False))
        self.assertNotEqual(int(fixed.frames["opcode"][1]), 0x0B)
        self.assertEqual(int(fixed.frames["cmd_lines"][1]), 1)
        self.assertFalse(np.asarray(fixed.frames["flags"] & STATE_CHANGE).any())

    def test_micron_register_writes_set_protocol_dummy_and_xip(self):
        from ila_viewer.spi import XIP_FRAME, decode_summary, state_text
        cap = self.capture([
            [((0,), [0x06])],
            [((0,), [0x81, 0x87])],                       # VCR: 8 dummy clocks, XIP enabled
            [((0,), [0xEB]), (QUAD, [0x12, 0x34, 0x56]), ((0,), [0x00]), (QUAD, [0xA1, 0xA2])],  # confirm 0
            [(QUAD, [0x00, 0x01, 0x00]), ((0,), [0x00]), (QUAD, [0xB1])],    # XIP: no command
            [(QUAD, [0x00, 0x02, 0x00]), ((0,), [0x80]), (QUAD, [0xC1])],    # confirm 1: last XIP frame
            [((0,), [0x06])],
            [((0,), [0x61, 0x7F])],                       # EVCR bit 7 = 0: quad protocol
            [(QUAD, [0x05]), (QUAD, [0x00])]])
        result = self.decode(cap, self.micron())
        formats = [result.frame_format(i) for i in range(result.frame_count)]
        self.assertEqual(formats, ["1-0-0", "1-0-1", "1-4-4", "0-4-4", "0-4-4", "1-0-0", "1-0-1", "4-0-4"])
        self.assertEqual(state_text(result.frames[1]), "Extended, dummy 8, XIP разрешён")
        self.assertEqual(self.labels(result, 2)[2:], ["dummy 8 XIP", "RD A1", "RD A2"])
        self.assertEqual(self.labels(result, 3), ["ADDR 000100", "dummy 8 XIP", "RD B1"])
        self.assertEqual(self.labels(result, 4), ["ADDR 000200", "dummy 8 выход XIP", "RD C1"])
        self.assertEqual(list(result.frames["opcode"][3:5]), [0xEB, 0xEB])
        self.assertTrue(all(result.frames["flags"][3:5] & XIP_FRAME))
        self.assertEqual(state_text(result.frames[4]), "Extended, dummy 8")
        self.assertEqual(state_text(result.frames[6]), "Quad 4-4-4, dummy 8")
        self.assertEqual(self.labels(result, 7), ["CMD 05 READ STATUS REGISTER", "RD 00"])
        self.assertIn("в XIP без кода команды: 2", decode_summary(result))

    def test_xip_at_capture_start_and_reset_memory_needs_reset_enable(self):
        cap = self.capture([
            [(QUAD, [0x00, 0x30, 0x00]), ((0,), [0x00]), (QUAD, [0x42])],
            [((0,), [0x99])],                     # no 66h before: ignored
            [(QUAD, [0x00, 0x40, 0x00]), ((0,), [0x00]), (QUAD, [0x43])]])
        settings = self.micron(xip_opcode=0xEB, dummy_cycles=8)
        result = self.decode(cap, settings)
        self.assertEqual(self.labels(result, 0), ["ADDR 003000", "dummy 8 XIP", "RD 42"])
        with self.assertRaises(ValueError):
            SpiSettings(xip_opcode=0x03)          # 03h READ has no confirmation bit
        quad = self.capture([[((0,), [0x35])], [(QUAD, [0x66])], [(QUAD, [0x99])], [((0,), [0x05, 0x00])]])
        result = self.decode(quad, self.micron())
        self.assertEqual(result.frame_format(3), "1-0-1")  # 66h, 99h: back to the starting protocol

    def test_command_set_files_load_save_and_name_their_errors(self):
        from ila_viewer import spi_profiles
        from ila_viewer.spi import config_to_dict, settings_from_dict
        folder = Path(self.temp.name)
        settings = self.micron(xip_opcode=0xEB, track_state=True)
        target = folder / "chip.json"
        spi_profiles.save(target, "My chip", settings.commands, "datasheet")
        loaded = spi_profiles.load(target)
        self.assertEqual((loaded.name, loaded.source, loaded.commands), ("My chip", "datasheet", settings.commands))
        stored = json.loads(json.dumps(config_to_dict(SpiDecoderConfig(spi_pins(), settings))))
        self.assertEqual(settings_from_dict(stored), settings)
        text = {"format": "spi-command-set", "version": 1, "name": "x", "commands": [
            {"code": ["C7", "60"], "name": "ERASE", "extended": "1-0-0"},
            {"code": "0xEBh", "extended": "1-4-4"}]}
        bad_cases = [
            ({"code": "35", "extended": "1-0-0", "adress": 3}, "неизвестные ключи adress"),
            ({"code": "61", "extended": "1-0-1", "effects": [{"set": "protocol", "bits": "7:6",
                                                              "values": {"0": "quad"}}]}, "2 бит"),
            ({"code": "61", "extended": "1-0-1", "effects": [{"set": "protocol", "value": "qpi"}]},
             "extended, dual или quad"),
            ({"code": "1G", "extended": "1-0-0"}, "HEX 00…FF"),
            ({"code": "C7", "extended": "1-0-0"}, "уже описан"),
        ]
        path = folder / "good.json"
        path.write_text(json.dumps(text), encoding="utf-8")
        self.assertEqual([spec.opcode for spec in spi_profiles.load(path).commands][:2], [0xC7, 0x60])
        for entry, message in bad_cases:
            broken = dict(text, commands=[*text["commands"][:1], entry])
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(spi_profiles.CommandSetError, msg=message) as caught:
                spi_profiles.load(path)
            self.assertIn("команда 2", str(caught.exception))
            self.assertIn(message, str(caught.exception))
        path.write_text("{\"commands\": [", encoding="utf-8")
        with self.assertRaisesRegex(spi_profiles.CommandSetError, "строке 1"):
            spi_profiles.load(path)

    def test_dialog_loads_a_chip_file_and_keeps_its_effects(self):
        from ila_viewer import spi_profiles
        from ila_viewer.spi_ui import SpiDecoderDialog
        QApplication.instance() or QApplication([])
        cap = self.capture(FLASH_FRAMES)
        dialog = SpiDecoderDialog(cap.signals)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.set_box.currentText(), spi_profiles.GENERIC_NAME)
        micron = self.micron()
        path = Path(self.temp.name) / "mt25q.json"
        spi_profiles.save(path, "MT25Q copy", micron.commands)
        self.assertTrue(dialog.load_command_set(path))
        self.assertEqual(dialog.set_box.currentText(), "MT25Q copy")
        dialog.xip_box.setCurrentIndex(dialog.xip_box.findData(0xEB))
        settings = dialog.config().settings
        self.assertEqual(settings.commands, micron.commands)
        self.assertEqual((settings.profile, settings.xip_opcode), ("MT25Q copy", 0xEB))
        path.write_text("[]", encoding="utf-8")
        self.assertFalse(dialog.load_command_set(path))
        self.assertIn("mt25q.json", dialog.error.text())
        again = SpiDecoderDialog(cap.signals, settings=settings)
        self.addCleanup(again.deleteLater)
        self.assertEqual(again.set_box.currentText(), "Micron MT25QL128ABA")  # same table as the built-in one
        self.assertEqual(again.xip_box.currentData(), 0xEB)


def wire_spi_csv(frames, idle=3):
    """Mode-0 single-line SPI on separate 1-bit wires; ``frames`` holds (mosi, miso) byte pairs."""
    rows = []
    for frame in frames:
        rows += [(1, 0, 0, 1)] * idle
        for mosi, miso in frame:
            for bit in range(7, -1, -1):
                o, i = mosi >> bit & 1, miso >> bit & 1
                rows += [(0, 0, o, i)] * 2 + [(0, 1, o, i)] * 2
            rows += [(0, 0, 0, 0)] * 3  # inter-byte gap, as many controllers leave
        rows += [(0, 0, 0, 0)] * 2
    rows += [(1, 0, 0, 1)] * idle
    body = "".join(",".join(map(str, row)) + ",0\n" for row in rows)
    return "spi_cs,spi_clk,spi_mosi,spi_miso,reset_crc\nRadix - HEX,HEX,HEX,HEX,HEX\n" + body


class SpiOverlayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "wires.csv"
        path.write_text(wire_spi_csv([[(0x05, 0xFF), (0xFF, 0x02)], [(0x03, 0x00), (0x00, 0x45), (0x00, 0x46)]]),
                        encoding="utf-8")
        self.cap = load_csv(path)
        self.addCleanup(self.cap.close)

    def decode(self, mode):
        pins = guess_pins(self.cap.signals)
        result = SpiDecoder().decode(self.cap, SpiDecoderConfig(pins, SpiSettings(mode=mode)))
        self.addCleanup(result.close)
        return result

    def test_reset_named_signal_is_not_guessed_as_hold(self):
        pins = guess_pins(self.cap.signals)
        self.assertEqual([line.text if line else None for line in pins.io], ["spi_mosi", "spi_miso", None, None])

    def test_fields_end_after_their_last_bit_and_navigation_steps_by_byte(self):
        result = self.decode("raw")
        lane = result.lanes[0]
        self.assertTrue(np.all(lane["end"][:-1] < lane["start"][1:]))  # visible gaps between bytes
        self.assertEqual(int(lane["end"][0] - lane["start"][0]), 7 * 4 + 2)  # 8 bits, half a clock after the last
        track = DecodedTrack(result, 0, "MOSI")
        self.assertEqual(track.transition(int(lane["start"][0]), 1), int(lane["start"][1]))
        self.assertEqual(track.transition(int(lane["start"][1]), 1), int(lane["end"][1]))  # frame end
        self.assertEqual(track.transition(int(lane["start"][2]), -1), int(lane["end"][1]))
        self.assertEqual([bubble_label(result, f) for f in lane], ["0x05", "0xFF", "0x03", "0x00", "0x00"])

    def test_overlays_put_bytes_on_their_wires_and_arrows_on_sck(self):
        result = self.decode("commands")
        tracks = [DecodedTrack(result, lane, "SPI") for lane in range(result.lane_count)]
        items = overlays(result, self.cap, tracks)
        names = [(name.rsplit("/", 1)[-1], annotation.kind) for name, annotation in items]
        self.assertEqual(names, [("spi_clk", "edges"), ("spi_mosi", "fields"), ("spi_miso", "fields")])
        mosi, miso = items[1][1], items[2][1]
        first, stop = mosi.window(0, self.cap.count)
        labels = [bubble_label(result, f) for f in mosi.visible(first, stop)]
        self.assertEqual(labels[:2], ["READ STATUS REGISTER (0x05)", "READ (0x03)"])
        self.assertTrue(labels[2].startswith("адрес 0x"))
        self.assertEqual([bubble_label(result, f) for f in miso.visible(*miso.window(0, self.cap.count))], ["0x02"])
        edges = items[0][1].visible(0, self.cap.count, 1000)
        self.assertEqual(len(edges), 5 * 8)  # rising sample edges inside CS only
        self.assertIsNone(items[0][1].visible(0, self.cap.count, 10))  # too dense: draw nothing
        self.assertTrue(list(mosi.busy(np.linspace(0, self.cap.count, 20))))
        registry = SignalAnnotations()
        registry.set("spi", items)
        mosi_signal = next(s for s in self.cap.signals if s.short_name == "spi_mosi")
        self.assertEqual(registry.for_signal(mosi_signal), [mosi])
        registry.set_enabled(False)
        self.assertEqual(registry.for_signal(mosi_signal), [])
        registry.set_enabled(True)
        registry.clear("spi")
        self.assertEqual(registry.for_signal(mosi_signal), [])


class InteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        if os.name == "nt":
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/consola.ttf")
        cls.app.setFont(QFont("Segoe UI", 10))

    def setUp(self):
        self.window = MainWindow(preferences_store=PreferencesStore(), cpu=TEST_CPU)
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
            other = MainWindow(preferences_store=PreferencesStore(path), cpu=TEST_CPU)
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

    def test_worker_processes_follow_cpu_and_preference(self):
        from ila_viewer.settings_dialog import AppearanceDialog
        from ila_viewer.cpu import available_cpus
        w = self.window
        self.assertEqual(w.worker_pool.workers, TEST_CPU.recommended_workers(available_cpus()))
        self.assertEqual(w.preferences.cpu_signature, TEST_CPU.signature)
        self.assertIsNotNone(w.worker_hint)
        self.assertIn("16 ядер / 32 потоков", w.worker_hint[0].text())
        dialog = AppearanceDialog(w.preferences, w.themes, w, cpu=TEST_CPU)
        dialog.show_tab("performance")
        self.assertEqual(dialog.workers_box.value(), 0)
        self.assertTrue(dialog.workers_box.specialValueText().startswith("Авто"))
        dialog.workers_box.setValue(4)
        w.apply_preferences(dialog.result_preferences())
        self.assertEqual((w.preferences.worker_count, w.worker_pool.workers), (4, 4))
        dialog.restore_defaults()
        self.assertEqual(dialog.workers_box.value(), 0)
        dialog.deleteLater()
        # The same CPU on the next start shows no hint.
        again = MainWindow(preferences_store=PreferencesStore(), cpu=TEST_CPU)
        again.preferences.cpu_signature = TEST_CPU.signature
        again.worker_hint = None
        again.suggest_workers()
        self.assertIsNone(again.worker_hint)
        again.close()

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
        self.assertGreaterEqual(dialog.signal_list.minimumHeight(),
                                min(300, max(120, dialog.screen().availableGeometry().height() - 560)))
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
            dialog.wlf_converter_edit.setText("C:/Questa/wlf2vcd.exe")
            dialog.accept()
        QTimer.singleShot(0, choose)
        w.show_appearance()
        self.assertTrue(v.preferences.fill_high)
        self.assertEqual(w.preferences.wlf2vcd_path, "C:/Questa/wlf2vcd.exe")
        self.assertEqual(w.source.readers[".wlf"].converter.executable, "C:/Questa/wlf2vcd.exe")
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

    def test_clock_banner_fits_small_screens_and_caches_edge_statistics(self):
        from ila_viewer.sampling import EdgeSampling
        v = self.window.view
        self.assertTrue(v.begin_period_setup())
        signals = self.window.condition_signals()
        banner = CopyPeriodBanner(v, self.window, signals=signals)
        self.addCleanup(banner.close)
        self.addCleanup(banner.detach)
        self.assertLess(banner.minimumSizeHint().width(), 1366 - 40)
        if banner.source_box.count() > 1:
            banner.source_box.setCurrentIndex(1)
            calls = []
            original = EdgeSampling.count_edges
            with patch.object(EdgeSampling, "count_edges",
                              lambda self, cancel=lambda: False: calls.append(1) or original(self, cancel)):
                banner.clock_edit.setText("100 MHz")
                banner.clock_edit.setText("25 MHz")
            self.assertEqual(calls, [])  # typing re-formats the cached statistics
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

    def test_named_bookmarks_menu_navigation_rename_delete_and_capture_lifetime(self):
        w = self.window
        v = w.view
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "capture.csv"
            path.write_text("clk\nRadix - HEX\n" + "".join(f"{i % 2}\n" for i in range(20)), encoding="utf-8")
            w.open_file(path)
            self.wait()
            controller = w.bookmark_controller
            self.assertIn("Навигация", [a.text() for a in w.menuBar().actions()])
            self.assertEqual(controller.add_action.shortcut().toString(), "Ctrl+M")
            v.set_cursor(15)
            controller.add_action.trigger()
            v.set_cursor(4)
            controller.add_action.trigger()
            controller.add_action.trigger()  # No duplicate marker on the same sample.
            self.assertEqual([(b.name, b.sample) for b in v.bookmarks.items], [("M2", 4), ("M1", 15)])
            v.set_cursor(0)
            controller.next_action.trigger()
            self.assertEqual(v.cursor_a, 4)
            controller.next_action.trigger()
            self.assertEqual(v.cursor_a, 15)
            controller.next_action.trigger()
            self.assertEqual(v.cursor_a, 15)
            controller.previous_action.trigger()
            self.assertEqual(v.cursor_a, 4)
            v.grab()  # Bookmarks render as labelled lines without disturbing samples.
            self.assertEqual(v.capture.signals[0].value_at(4), 0)

            dialog = controller.show_manager(w)
            self.assertFalse(dialog.isModal())
            self.assertEqual(dialog.list.count(), 2)
            dialog.list.item(1).setText("irq")
            self.assertEqual(v.bookmarks.items[1].name, "irq")
            dialog.list.item(0).setText("")
            self.assertEqual(dialog.list.item(0).text(), "M2")
            dialog.list.setCurrentRow(0)
            dialog.delete_button.click()
            self.assertEqual([b.name for b in v.bookmarks.items], ["irq"])

            path.write_text("clk\nRadix - HEX\n" + "".join(f"{i % 2}\n" for i in range(10)), encoding="utf-8")
            w.refresh_csv()
            self.wait()
            self.assertEqual([(b.name, b.sample) for b in v.bookmarks.items], [("irq", 9)])
            w.open_file(path)
            self.wait()
            self.assertEqual(len(v.bookmarks), 0)
            self.assertEqual(dialog.list.count(), 0)
            self.assertNotIn("bookmark", json.dumps(w.preferences.__dict__, default=str).lower())

    def test_bookmark_context_actions_and_edge_readout(self):
        w = self.window
        v = w.view
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "capture.csv"
            rows = "".join(f"{i % 2},{i % 16:X}\n" for i in range(40))
            path.write_text("clk,bus[3:0]\nRadix - HEX,HEX\n" + rows, encoding="utf-8")
            w.open_file(path)
            self.wait()
            controller = v.menus
            class Menu:
                def build(inner, view):
                    from PySide6.QtWidgets import QMenu
                    inner.last = QMenu(view)
                    QTimer.singleShot(0, inner.last.close)
                    return inner.last
            v.menus = Menu()
            point = self.point(0, round(v.sample_x(10)))
            QApplication.sendEvent(v.viewport(), QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse, point, v.viewport().mapToGlobal(point)))
            v.menus = controller
            self.assertEqual(v.context_sample, 10)
            menu = v.menus.build(v)
            next(a for a in menu.actions() if a.text().startswith("Поставить маркер здесь")).trigger()
            self.assertEqual([b.sample for b in v.bookmarks.items], [10])
            menu = v.menus.build(v)
            self.assertFalse(any(a.text().startswith("Поставить маркер здесь") for a in menu.actions()))
            with patch("ila_viewer.actions.QInputDialog.getText", return_value=("start", True)):
                next(a for a in menu.actions() if a.text().startswith("Переименовать маркер")).trigger()
            self.assertEqual(v.bookmarks.items[0].name, "start")
            next(a for a in v.menus.build(v).actions() if a.text().startswith("Удалить маркер")).trigger()
            self.assertEqual(len(v.bookmarks), 0)

            v.selected = 0
            v.set_cursor(2)
            v.set_cursor(12, secondary=True)
            self.assertIn("↑clk A…B: 5", w.readout.text())
            self.assertIn("T≈ 2 отсч.", w.readout.text())
            v.selected = 1
            v.selectionChanged.emit()
            self.assertNotIn("↑", w.readout.text())


    def spi_window(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "spi.csv"
        path.write_text(spi_csv(FLASH_FRAMES), encoding="utf-8")
        w = self.window
        clipboard = SimpleNamespace(text="")
        clipboard.write = lambda value: setattr(clipboard, "text", value)
        w.clipboard = clipboard
        w.open_file(path)
        self.wait()
        return w, clipboard, path

    def test_spi_decoder_row_navigation_selection_and_copy(self):
        w, clipboard, _ = self.spi_window()
        v = w.view
        dialog = w.spi.open_dialog()
        self.assertIsNotNone(dialog)
        self.assertFalse(dialog.isModal())
        self.assertEqual(dialog.sck_box.currentData(), LineRef("sck", 0, "sck"))
        dialog.dummy_box.setValue(10)
        dialog.submit()
        self.wait()
        result = w.spi.result
        self.assertIsNotNone(result)
        self.assertEqual(result.frame_count, 4)
        self.assertEqual(v.rows[0].node.kind, "decoded")
        panel = w.spi.panel
        self.assertTrue(panel.isVisible())
        self.assertEqual(panel.model.rowCount(), 4)
        self.assertIn("DE AD BE EF", panel.model.data(panel.model.index(1, 8)))
        # ←/→ on the decoder row step through fields; Shift+→ extends B byte by byte.
        v.selected = 0
        v.interaction_region = "waveform"
        v.setFocus()
        data = result.frame_data(1)
        v.set_cursor(int(data[0]["start"]))
        QTest.keyClick(v, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
        QTest.keyClick(v, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
        self.assertEqual(v.cursor_b, int(data[2]["start"]))
        self.assertEqual(v.row_value(v.rows[0].node), "RD DE")
        w.spi.copy_action.trigger()
        self.wait()
        self.assertEqual(clipboard.text, "DE AD")
        # Panel: one line per selected transaction, then select its data with A/B.
        panel.layout_box.setCurrentIndex(panel.layout_box.findData("frames"))
        model = panel.table.selectionModel()
        model.select(panel.model.index(2, 0), model.SelectionFlag.ClearAndSelect | model.SelectionFlag.Rows)
        panel.buttons["Копировать выбранные"].click()
        self.wait()
        self.assertEqual(clipboard.text, "#3 03 @001000: 11 22")
        panel.buttons["Выделить данные"].click()
        fields = result.frame_data(2)
        self.assertEqual((v.cursor_a, v.cursor_b), (int(fields[0]["start"]), int(fields[-1]["end"])))
        panel.filter_box.setCurrentIndex(panel.filter_box.findData(0x02))
        self.assertEqual(panel.model.rowCount(), 1)
        panel.filter_box.setCurrentIndex(0)
        # Search: the panel keeps matching transactions, F3 on the decoder row jumps to bytes.
        panel.search_edit.setText("@1000")
        QTest.keyClick(panel.search_edit, Qt.Key.Key_Return)
        self.assertEqual(panel.model.rowCount(), 1)
        self.assertEqual(panel.model.frame_index(0), 2)
        self.assertIn("адрес 1000", panel.summary.text())
        panel.search_edit.setText("BE EF")
        QTest.keyClick(panel.search_edit, Qt.Key.Key_Return)
        self.assertEqual([panel.model.frame_index(i) for i in range(panel.model.rowCount())], [1])
        panel.search_edit.setText("q")
        QTest.keyClick(panel.search_edit, Qt.Key.Key_Return)
        self.assertIn("не HEX", panel.summary.text())
        panel.search_edit.clear()
        self.assertEqual(panel.model.rowCount(), 4)
        v.selected = 0
        v.selectionChanged.emit()
        self.assertTrue(w.search_edit.placeholderText().startswith("SPI"))
        v.set_cursor(0)
        w.search_edit.setText("22")
        w.find_value(1)
        self.wait()
        self.assertEqual(v.cursor_a, int(result.frame_data(2)[1]["start"]))
        w.search_edit.setText("cmd 77")
        w.find_value(1)
        self.wait()
        self.assertIn("не найдено", w.statusBar().currentMessage())
        # The context menu offers the transaction under the mouse.
        v.selected = 0
        v.context_sample = int(fields[0]["start"])
        menu = v.menus.build(v)
        spi_menu = next(a.menu() for a in menu.actions() if a.text() == "SPI / QSPI")
        self.assertIn("Выделить транзакцию #3", [a.text() for a in spi_menu.actions()])
        v.viewport().grab()  # labelled fields
        v.fit()
        v.viewport().grab()  # zoomed-out band
        next(a for a in spi_menu.actions() if a.text() == "Убрать строку декодера").trigger()
        self.assertNotEqual(v.rows[0].node.kind, "decoded")

    def test_spi_refresh_redecodes_and_other_file_drops_decoder(self):
        w, clipboard, path = self.spi_window()
        w.spi.open_dialog().submit()
        self.wait()
        paths = list(w.spi.result.paths)
        w.refresh_csv()
        self.wait()
        QTest.qWait(30)
        self.wait()
        self.assertIsNotNone(w.spi.result)
        self.assertEqual(w.spi.result.frame_count, 4)
        self.assertFalse(any(p.exists() for p in paths))  # old decoder files released
        self.assertEqual(sum(1 for row in w.view.rows if row.node.kind == "decoded"), 1)
        w.open_file(FIXTURE_CSV)
        self.wait()
        QTest.qWait(30)
        self.assertIsNone(w.spi.result)
        self.assertIsNone(w.spi.config)
        self.assertFalse(any(row.node.kind == "decoded" for row in w.view.rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
