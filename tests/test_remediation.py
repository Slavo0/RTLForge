"""Regressions for the review of 8dbe201 (HANDOFF.md items 1-6)."""
import json
import os
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from ila_viewer.conditions import ConditionCompiler
from ila_viewer.copying import CopyValuesService
from ila_viewer.cpu import WorkerPool
from ila_viewer.data import load_csv
from ila_viewer.highlights import HighlightGroup, HighlightIntervals, HighlightService
from ila_viewer.preferences import PreferencesStore, ViewerPreferences
from ila_viewer.refresh import CaptureRefreshState, resolved_radixes
from ila_viewer.sampling import EdgeSampling
from ila_viewer.sources import WlfExportService
from ila_viewer.spi import (DATA, FIELD_DTYPE, PARTIAL, RESET_FRAME, UNKNOWN, UNKNOWN_COMMAND, BusWidths,
                            CommandSpec, FixedFormat, SpiDecoder, SpiDecoderConfig, SpiExportRequest,
                            SpiExportService, SpiSettings, config_to_dict, default_commands, guess_pins,
                            pins_from_dict, settings_from_dict)
from ila_viewer.vcd import VcdCaptureReader
from ila_viewer.tempstore import TempStorage


class RemediationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def load(self, text, suffix="csv", name="capture", binary=False):
        path = self.folder / f"{name}.{suffix}"
        if binary:
            path.write_bytes(text)
        else:
            path.write_text(text, encoding="utf-8")
        cap = load_csv(path) if suffix == "csv" else VcdCaptureReader().load(path)
        self.addCleanup(cap.close)
        return cap

    # 1. SPI export and atomic writes -------------------------------------------------

    @staticmethod
    def spi_result(values, flags):
        fields = np.zeros(len(values), dtype=FIELD_DTYPE)
        fields["start"] = np.arange(len(values)) * 10
        fields["end"] = fields["start"] + 8
        fields["value"], fields["flags"] = values, flags
        fields["kind"], fields["bits"], fields["frame"] = DATA, 8, 0
        return SimpleNamespace(lanes=[fields], capture_count=100)

    def test_binary_export_rejects_unknown_bytes_and_keeps_destination(self):
        target = self.folder / "out.bin"
        target.write_bytes(b"previous")
        neighbour = self.folder / "out.bin.part"
        neighbour.write_bytes(b"neighbour")
        service = SpiExportService()
        unknown = self.spi_result([0x12, 0x00, 0x34], [0, UNKNOWN, 0])
        with self.assertRaisesRegex(ValueError, r"#1.*байт 1.*X/Z"):
            service.write_binary(SpiExportRequest(unknown), target)
        self.assertEqual(target.read_bytes(), b"previous")
        known = self.spi_result([0x12, 0x00, 0x34], [0, 0, 0])
        self.assertEqual(service.write_binary(SpiExportRequest(known), target), 3)
        self.assertEqual(target.read_bytes(), bytes([0x12, 0x00, 0x34]))
        self.assertEqual(neighbour.read_bytes(), b"neighbour")
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["out.bin", "out.bin.part"])

    def test_preferences_never_touch_a_shared_temporary(self):
        path = self.folder / "settings.json"
        shared = self.folder / "settings.json.tmp"
        shared.write_text("another instance", encoding="utf-8")
        store = PreferencesStore(path)
        store.save(ViewerPreferences(theme="light"))
        self.assertEqual(store.load().theme, "light")
        self.assertEqual(shared.read_text(encoding="utf-8"), "another instance")

    def test_wlf_export_refuses_to_overwrite_its_source(self):
        source = self.folder / "sim.wlf"
        source.write_bytes(b"WLF")
        converter = SimpleNamespace(convert=lambda src, dst, cancel: Path(dst).write_text("$end"))
        with self.assertRaises(ValueError):
            WlfExportService(converter).export(source, source)
        with self.assertRaises(ValueError):
            WlfExportService(converter).export(source, self.folder / "." / "sim.wlf")
        self.assertEqual(source.read_bytes(), b"WLF")

    # 2. Condition names and refresh ----------------------------------------------------

    def test_quoted_full_names_are_case_exact(self):
        text = ("$scope module top $end $var wire 1 ! CLK $end $var wire 1 \" clk $end $upscope $end "
                "$enddefinitions $end\n#0 1! 0\"\n#1 1! 0\"\n")
        cap = self.load(text, "vcd")
        compiler = ConditionCompiler(cap.signals)
        self.assertTrue(compiler.compile("`top/CLK` == 1").matches(0))
        self.assertFalse(compiler.compile("`top/clk` == 1").matches(0))
        with self.assertRaisesRegex(ValueError, "регистр"):
            compiler.compile("`TOP/clk` == 1")

    def test_literal_like_signal_names_are_quoted(self):
        cap = self.load("b1,abc,data\nRadix - BINARY,BINARY,BINARY\n0,0,1\n")
        compiler = ConditionCompiler(cap.signals)
        tokens = {s.short_name: compiler.token_for(s) for s in cap.signals}
        self.assertEqual(tokens, {"b1": "`b1`", "abc": "`abc`", "data": "data"})
        self.assertFalse(compiler.compile(tokens["b1"] + " == 1").matches(0))
        self.assertTrue(compiler.compile(tokens["b1"] + " == 0").matches(0))

    def test_refresh_keeps_literal_radix_after_display_radix_changes(self):
        path = self.folder / "data.csv"
        path.write_text("data[7:0]\nRadix - HEX\n02\n10\n02\n", encoding="utf-8")
        cap = load_csv(path)
        self.addCleanup(cap.close)
        name = cap.signals[0].name
        hex_compiler = ConditionCompiler(cap.signals, {name: "HEX"})
        condition = hex_compiler.compile("data == 10")
        intervals = HighlightService().build(cap, condition, 0, cap.count, 1, 0)
        group = HighlightGroup("h", "data == 10", "#ffffff", intervals, 0, cap.count, 1, 0,
                               radixes=resolved_radixes(hex_compiler, condition))
        self.addCleanup(group.close)
        view = SimpleNamespace(tree=SimpleNamespace(root=SimpleNamespace(signal=None, children=[])),
                               capture=cap)
        model = SimpleNamespace(groups=[group], master_enabled=True)
        binary = ConditionCompiler(cap.signals, {name: "BINARY"})
        state = CaptureRefreshState.capture(view, model, binary)  # used to raise ValueError
        self.assertEqual(state.highlights[0].dependencies, frozenset({name}))
        rebuilt = state.rebuild_highlights(cap, binary, HighlightService(), HighlightService())
        self.addCleanup(lambda: [g.close() for g in rebuilt])
        self.assertEqual(list(rebuilt[0].intervals.visible(0, cap.count)), [(1.0, 2.0)])  # 0x10, not 2

    # 3. VCD time axis --------------------------------------------------------------------

    VCD_CLOCK = ("$timescale 1ns $end $var wire 1 ! clk $end $var wire 1 \" d $end $enddefinitions $end\n"
                 "#0 0! 0\"\n#5 1!\n#7 1\"\n#10 0!\n#15 1!\n#20 0!\n#22 0\"\n#24 1\"\n#25 1!\n")

    def test_vcd_clock_period_uses_timestamps(self):
        cap = self.load(self.VCD_CLOCK, "vcd")
        clk = cap.signals[0]
        rising = EdgeSampling(clk, "rising", times=cap.times)
        self.assertEqual(rising.typical_interval(), 3.5)  # rows depend on unrelated activity
        self.assertEqual(rising.typical_ticks(), 10)
        from ila_viewer.period_dialog import edge_statistics
        stats = edge_statistics(clk, "rising", cap.times, 1e-9)
        self.assertEqual(stats.edges, 3)
        self.assertAlmostEqual(stats.period_seconds, 10e-9)

    def test_gated_clock_cap_follows_time_not_rows(self):
        # Busy cycles of 10 ns with three unrelated changes each (5 rows per period), then
        # a long idle stretch of sparse rows before the clock resumes at #1000.
        body = ["#0 0! 0\""]
        level = 0
        for t in (5, 15, 25, 35):
            body.append(f"#{t} 1!")
            for k in ((1, 2, 3) if t < 35 else ()):  # the last cycle is quiet
                level ^= 1
                body.append(f"#{t + k} {level}\"")
            body.append(f"#{t + 5} 0!")
        body += ["#100 1\"", "#200 0\"", "#300 1\"", "#1000 1!"]
        text = ("$timescale 1ns $end $var wire 1 ! clk $end $var wire 1 \" d $end $enddefinitions $end\n"
                + "\n".join(body) + "\n")
        cap = self.load(text, "vcd")
        times = cap.times.tolist()
        last_busy, idle = times.index(35), times.index(100)
        at_edge = EdgeSampling(cap.signals[0], "rising", before_edge=False, times=cap.times)
        chunk = next(at_edge.point_chunks(0, cap.count, cap.count))
        index = list(chunk.sample).index(last_busy)
        # One 10 ns period after #35 ends before the idle row at #100 ...
        self.assertEqual(int(chunk.end[index]), idle)
        # ... while a cap of "5 rows" would reach into the idle stretch.
        rows = EdgeSampling(cap.signals[0], "rising", before_edge=False)
        self.assertGreater(int(next(rows.point_chunks(0, cap.count, cap.count)).end[index]), idle)
        points = list(at_edge.points(0, cap.count, cap.count))
        ends = [int(v) for c in at_edge.point_chunks(0, cap.count, cap.count) for v in c.end]
        self.assertEqual([p[2] for p in points], ends)

    def test_dense_highlights_bin_by_pixel_edges(self):
        path = self.folder / "intervals.bin"
        data = np.array([[i, i + 1] for i in range(0, 200, 2)], dtype="<i8")
        data.tofile(path)
        intervals = HighlightIntervals(path, len(data))
        self.addCleanup(intervals.close)
        # Rows 0..200 are crammed into the first pixel, the rest is a long idle stretch.
        edges = np.array([0.0, 200.0, 300.0, 400.0, 500.0])
        self.assertEqual(list(intervals.visible(0, 500, 4, edges)), [(0.0, 200.0)])

    def test_uninitialised_vcd_bus_has_no_false_transition(self):
        text = "$var wire 4 ! bus $end $var wire 1 \" b $end $enddefinitions $end\n#0 0\"\n#10 bx ! 1\"\n"
        cap = self.load(text, "vcd")
        bus = cap.signals[0]
        self.assertEqual(bus.value_at(0), bus.value_at(1))
        self.assertIsNone(bus.transition(0, 1))

    # 4. Bit ranges and CSV values ------------------------------------------------------

    def test_bit_range_typo_is_rejected_without_allocation(self):
        labels = list(range(7, -1, -1))
        tracemalloc.start()
        try:
            started = time.perf_counter()
            with self.assertRaises(ValueError):
                CopyValuesService.parse_bit_ranges("0-1000000000", labels)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 64 * 1024)
        self.assertLess(time.perf_counter() - started, 1)
        self.assertEqual(CopyValuesService.parse_bit_ranges("0-1", labels), (1, 0))

    def test_csv_rejects_embedded_nul(self):
        with self.assertRaises(Exception):
            self.load(b"data[7:0]\nRadix - HEX\n1\x002\n", binary=True)
        with self.assertRaises(Exception):
            self.load(b"Sample in Window,data[7:0]\nRadix - UNSIGNED,HEX\n1\x002,1\n", binary=True, name="meta")

    def test_signed_pattern_is_stored_as_the_value_it_displays(self):
        cap = self.load("data[7:0]\nRadix - SIGNED\n255\n-1\n1\n")
        signal = cap.signals[0]
        self.assertEqual([signal.value_at(i) for i in range(3)], [-1, -1, 1])
        self.assertEqual(signal.transition(0, 1), 2)  # 255 and -1 are one value
        self.assertEqual(signal.search(signal.query_value("-1", "SIGNED"), 2, 1), (0, True))

    # 5. Worker pool ---------------------------------------------------------------------

    def test_pool_is_capped_and_resizes_only_when_idle(self):
        pool = WorkerPool(62, limit=61)
        self.assertEqual(pool.workers, 61)
        pool = WorkerPool(2)
        future = object()
        pool._active.add(future)
        pool._executor = executor = SimpleNamespace(shutdown=lambda **kw: self.fail("cancelled running work"))
        pool.set_workers(4)
        self.assertEqual(pool.workers, 2)
        self.assertIs(pool._executor, executor)
        pool._finished(future)
        pool._executor = None
        pool.set_workers(4)
        self.assertEqual(pool.workers, 4)

    # 6. Marker badges --------------------------------------------------------------------

    def test_coincident_markers_share_one_badge(self):
        from ila_viewer.rendering import WaveformRenderer
        theme = SimpleNamespace(marker_a="#ff0000", marker_b="#0000ff")
        view = SimpleNamespace(themes=SimpleNamespace(current=theme), cursor_a=10, cursor_b=10,
                               left=0, span=100, sample_x=lambda s: 200 + s * 5)
        renderer = WaveformRenderer(view)
        badges = renderer.cursor_badges(1000, 200)
        self.assertEqual([b[0] for b in badges], ["A/B"])
        self.assertEqual(len(badges[0][3]), 2)
        view.cursor_b = 11  # 5 px apart: labels go to opposite sides of their lines
        first, second = renderer.cursor_badges(1000, 200)
        self.assertEqual((first[0], second[0]), ("A", "B"))
        self.assertLessEqual(first[1] + first[2], second[1])
        view.cursor_b = 50
        self.assertEqual([b[0] for b in renderer.cursor_badges(1000, 200)], ["A", "B"])


def qspi_reset_csv(frames, idle=3):
    """Mode-0 frames on ``dq_in[3:0]`` like a Vivado ILA: each frame is a list of DQ values per clock."""
    rows = []
    for values in frames:
        rows += [(1, 0, 0xF)] * idle
        for value in values:
            rows += [(0, 0, value)] * 2 + [(0, 1, value)] * 2
        rows += [(0, 0, 0xF)] * 2
    rows += [(1, 0, 0xF)] * idle
    body = "".join(f"{cs},{sck},{dq:X}\n" for cs, sck, dq in rows)
    return "top/phy/cs_n_ff,top/phy/sck,top/phy/dq_in[3:0]\nRadix - HEX,HEX,HEX\n" + body


def byte_clocks(byte, mask=0xE):
    """One byte on IO0, other lines high as pull-ups leave them."""
    return [mask | (byte >> bit & 1) for bit in range(7, -1, -1)]


def nibbles(data):
    return [n for byte in data for n in (byte >> 4, byte & 0xF)]


class SpiResetAndMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def capture(self, frames):
        path = Path(self.temp.name) / f"q{len(list(Path(self.temp.name).iterdir()))}.csv"
        path.write_text(qspi_reset_csv(frames), encoding="utf-8")
        cap = load_csv(path)
        self.addCleanup(cap.close)
        return cap

    def decode(self, cap, settings=None):
        result = SpiDecoder().decode(cap, SpiDecoderConfig(guess_pins(cap.signals), settings or SpiSettings()))
        self.addCleanup(result.close)
        return result

    def flash_boot(self):
        # Recovery sequence (all ones, odd clock counts), 66/99 in 1-1-1, then EB in 1-4-4.
        read = (byte_clocks(0xEB) + nibbles([0x00, 0x01, 0x00]) + [0, 0] + [0xC] * 8
                + nibbles([0x30, 0x00, 0x41, 0x2F]))
        return self.capture([[0xF] * 7, [0xF] * 13, [0xF] * 16, byte_clocks(0x66), byte_clocks(0x99), read])

    def test_bus_named_dq_in_gives_io_lines(self):
        cap = self.flash_boot()
        pins = guess_pins(cap.signals)
        self.assertEqual((pins.sck.text, pins.cs.text), ("sck", "cs_n_ff"))
        self.assertEqual([(line.signal, line.offset) for line in pins.io],
                         [("top/phy/dq_in[3:0]", offset) for offset in range(4)])
        self.assertEqual([line.text for line in pins.io], ["dq_in[0]", "dq_in[1]", "dq_in[2]", "dq_in[3]"])

    def test_recovery_frames_are_reset_not_unknown_commands(self):
        result = self.decode(self.flash_boot())
        frames = result.frames
        self.assertEqual(list(frames["flags"][:3] & RESET_FRAME), [RESET_FRAME] * 3)
        self.assertFalse((frames["flags"][:3] & (UNKNOWN_COMMAND | PARTIAL)).any())
        self.assertEqual([result.frame_format(i) for i in range(3)], ["сброс"] * 3)
        self.assertEqual([int(f["value"]) for f in (result.frame_fields(i)[0] for i in range(3))], [7, 13, 16])
        self.assertEqual(list(frames["opcode"][3:]), [0x66, 0x99, 0xEB])
        self.assertEqual(int(frames["address"][5]), 0x000100)
        data = result.frame_data(5)
        self.assertEqual(list(data["value"]), [0x30, 0x00, 0x41, 0x2F])
        self.assertEqual(int(frames["flags"][5]), 0)

    def test_reset_option_off_or_ff_in_table_keeps_old_meaning(self):
        cap = self.flash_boot()
        off = self.decode(cap, SpiSettings(reset_frames=False))
        self.assertTrue(off.frames["flags"][2] & UNKNOWN_COMMAND)
        self.assertFalse(off.frames["flags"][2] & RESET_FRAME)
        table = SpiSettings(commands=(*default_commands(), CommandSpec(0xFF, "MY FF", ("1-0-0", "", ""))))
        listed = self.decode(cap, table)
        self.assertEqual(int(listed.frames["opcode"][2]), 0xFF)
        self.assertFalse(listed.frames["flags"][2] & RESET_FRAME)
        # A data byte FF after a real command is data, not a reset.
        self.assertEqual(int(self.decode(self.capture([byte_clocks(0x66) + [0xF] * 8])).frames["flags"][0]), 0)

    def test_wrong_protocol_is_named_and_hinted(self):
        from ila_viewer.spi import bubble_label, decode_summary, field_label
        cap = self.flash_boot()
        right = self.decode(cap)
        self.assertEqual(decode_summary(right), "SPI: 6 транзакций, 4 байт данных, сбросов режима: 3")
        quad = self.decode(cap, SpiSettings(protocol="quad"))
        command = quad.frame_fields(3)[0]
        self.assertEqual(bubble_label(quad, command), "0xEF нет в таблице")
        self.assertEqual(bubble_label(quad, command, short=True), "0xEF ?")
        self.assertEqual(field_label(quad, command), "CMD EF нет в таблице")
        self.assertIn("проверьте протокол (QPI/DPI", decode_summary(quad))
        self.assertEqual(bubble_label(right, right.frame_fields(3)[0]), "RESET ENABLE (0x66)")

    def test_dialog_choices_round_trip_and_pins_need_every_signal(self):
        cap = self.flash_boot()
        custom = (*default_commands()[:3], CommandSpec(0x77, "X", ("1-1-4", "", ""), 3, (6, 6, 6), "read"))
        settings = SpiSettings(mode="fixed", sample_edge="falling", before_edge=False, dummy_cycles=8,
                               fixed=FixedFormat(BusWidths(0, 4, 4), 3, 6, "read", "off"), reset_frames=False,
                               commands=custom)
        config = SpiDecoderConfig(guess_pins(cap.signals), settings)
        stored = json.loads(json.dumps(config_to_dict(config)))
        self.assertEqual(settings_from_dict(stored), settings)
        self.assertEqual(pins_from_dict(stored, cap.signals), config.pins)
        self.assertNotIn("commands", config_to_dict(SpiDecoderConfig(config.pins))["settings"])
        other = self.capture([byte_clocks(0x66)])
        renamed = [s for s in other.signals]
        self.assertIsNotNone(pins_from_dict(stored, renamed))
        stored["pins"]["io"][3] = {"signal": "missing", "offset": 0}
        self.assertIsNone(pins_from_dict(stored, cap.signals))
        self.assertEqual(settings_from_dict({"settings": {"mode": "bogus"}}), SpiSettings())
        self.assertEqual(settings_from_dict({}), SpiSettings())

    def test_controller_remembers_last_decoder_choices(self):
        from PySide6.QtWidgets import QApplication
        from ila_viewer.spi_ui import SpiController, SpiDecoderDialog
        QApplication.instance() or QApplication([])
        cap = self.flash_boot()
        dialog = SpiDecoderDialog(cap.signals)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.bus_box.currentData(), "top/phy/dq_in[3:0]")
        self.assertTrue(dialog.quad_toggle.isChecked())
        dialog.io_boxes[3].setCurrentIndex(0)
        self.assertIsNone(dialog.bus_box.currentData())
        dialog.bus_box.setCurrentIndex(1)
        self.assertEqual(dialog.config().pins.io, guess_pins(cap.signals).io)

        preferences = ViewerPreferences()
        saved = []
        view = SimpleNamespace(capture=cap, menus=SimpleNamespace(providers=[]))
        runner = SimpleNamespace(worker=None, start_work=lambda *args: None, clipboard=None)
        controller = SpiController(view, runner, preferences=preferences, persist=lambda: saved.append(1))
        config = SpiDecoderConfig(guess_pins(cap.signals), SpiSettings(mode="fixed", sample_edge="falling"))
        controller.decode(config)
        self.assertEqual(saved, [1])
        self.assertEqual(preferences.spi_decoder["settings"]["mode"], "fixed")
        store = PreferencesStore(Path(self.temp.name) / "prefs.json")
        store.save(preferences)
        again = SpiController(view, runner, preferences=store.load())
        from PySide6.QtWidgets import QWidget
        again.window = QWidget()
        self.addCleanup(again.window.deleteLater)
        self.assertEqual(again.settings, config.settings)
        dialog = again.open_dialog()
        self.addCleanup(dialog.reject)
        self.assertEqual(dialog.mode_box.currentData(), "fixed")
        self.assertEqual(dialog.edge_box.currentData(), "falling")
        self.assertEqual(dialog.config().pins, config.pins)


class SortedSearchTests(unittest.TestCase):
    """WLF performance task 2: integer keys and no whole-array conversion per search."""

    def keys(self, rng, array):
        picks = rng.choice(array, 40)
        return [*picks.tolist(), *(picks + 0.5).tolist(), *(picks - 0.25).tolist(), -7.5, -3, 0.0,
                float(array[-1]) + 0.1, int(array[-1]) + 10, float(array[0]) - 1e6, 1e15, -1e15]

    def test_integer_keys_match_float_search_for_both_sides(self):
        from ila_viewer.data import search_sorted
        rng = np.random.default_rng(11)
        for size in (0, 1, 2, 17, 1000):
            array = np.sort(rng.integers(-500, 2**40, size)).astype(np.int64)
            if size > 4:
                array[3] = array[4] = array[2]  # duplicates
            keys = self.keys(rng, array) if size else [0, 0.5, -1.5]
            # Structured fields and 2-D columns are strided views; they must not be copied.
            table = np.zeros(size, dtype=[("start", "<i8"), ("end", "<i8")])
            table["start"] = array
            pairs = np.stack([array, array], axis=1)
            for view in (array, table["start"], pairs[:, 0]):
                for side in ("left", "right"):
                    for key in keys:
                        expected = int(np.searchsorted(array, np.float64(key), side=side))
                        got = search_sorted(view, key, side)
                        self.assertEqual(got, expected, (size, side, key))
                        self.assertIsInstance(got, int)
                    floats = np.asarray(keys, dtype=np.float64)
                    expected = np.searchsorted(array, floats, side=side)
                    np.testing.assert_array_equal(search_sorted(view, floats, side), expected)
                    ints = np.floor(floats).astype(np.int64)
                    np.testing.assert_array_equal(search_sorted(view, ints, side),
                                                  np.searchsorted(array, ints, side=side))

    def test_few_keys_bisect_a_strided_index_without_copying_it(self):
        from ila_viewer.data import search_sorted
        rng = np.random.default_rng(5)
        pairs = np.sort(rng.integers(0, 10**9, (300_000, 2)), axis=0)
        pairs[1000:1010, 1] = pairs[999, 1]
        column = pairs[:, 1]
        self.assertFalse(column.flags.c_contiguous)
        keys = np.r_[rng.uniform(-10, 1.1e9, 20), column[[0, 999, 1005, 299_999]].astype(np.float64)]
        with patch.object(np, "ascontiguousarray", side_effect=AssertionError("copied")):
            for side in ("left", "right"):
                np.testing.assert_array_equal(search_sorted(column, keys, side),
                                              np.searchsorted(column.copy(), keys, side=side))
                for key in keys[-4:]:
                    self.assertEqual(search_sorted(column, key, side),
                                     int(np.searchsorted(column.copy(), key, side=side)))

    def test_keys_beyond_the_int64_range_and_nan(self):
        from ila_viewer.data import search_sorted
        array = np.array([-(2**63), -5, 0, 7, 2**63 - 1], dtype=np.int64)
        for side in ("left", "right"):
            self.assertEqual(search_sorted(array, float("inf"), side), 5)
            self.assertEqual(search_sorted(array, float("-inf"), side), 0)
            self.assertEqual(search_sorted(array, float("nan"), side), 5)
            self.assertEqual(search_sorted(array, 2**70, side), 5)
            self.assertEqual(search_sorted(array, -(2**70), side), 0)
            np.testing.assert_array_equal(
                search_sorted(array, np.array([np.inf, -np.inf, np.nan, 6.5, 1e30]), side), [5, 0, 5, 3, 5])
        self.assertEqual(search_sorted(array, 2**63 - 1, "left"), 4)
        self.assertEqual(search_sorted(array, 2**63 - 1, "right"), 5)
        self.assertEqual(search_sorted(array, -(2**63), "left"), 0)
        self.assertEqual(search_sorted(array, -(2**63), "right"), 1)

    def test_float_arrays_are_searched_unchanged(self):
        from ila_viewer.data import search_sorted
        edges = np.linspace(0.5, 99.5, 50)
        for side in ("left", "right"):
            for key in (0, 3.2, 50, 99.5, 120):
                self.assertEqual(search_sorted(edges, key, side), int(np.searchsorted(edges, key, side=side)))


class FrameIdentityTests(unittest.TestCase):
    """Rendering optimizations must not change a pixel (golden frames in tests/fixtures/frames)."""

    @classmethod
    def setUpClass(cls):
        from tests.frames import render_all
        cls.temp = tempfile.TemporaryDirectory()
        cls.searches = []
        original = np.searchsorted

        def recording(array, keys, *args, **kwargs):
            array_view = np.asarray(array) if not isinstance(array, np.ndarray) else array
            cls.searches.append((array_view.dtype, np.asarray(keys).dtype,
                                 array_view.flags.c_contiguous, len(array_view)))
            return original(array, keys, *args, **kwargs)

        from ila_viewer.data import Capture
        cls.offset_calls = 0
        offset_at = Capture.time_offset_at

        def counting(self, *args, **kwargs):
            cls.offset_calls += 1
            return offset_at(self, *args, **kwargs)

        with patch.object(np, "searchsorted", recording), patch.object(Capture, "time_offset_at", counting):
            cls.images, cls.environment = render_all(cls.temp.name)
        from tests.frames import ENVIRONMENT, recorded_environment
        recorded = recorded_environment()
        cls.environment_mismatch = None
        if recorded != cls.environment:
            differences = ", ".join(f"{key}: recorded {(recorded or {}).get(key)!r}, here {cls.environment.get(key)!r}"
                                    for key in sorted(set(cls.environment) | set(recorded or {}))
                                    if (recorded or {}).get(key) != cls.environment.get(key))
            cls.environment_mismatch = (f"golden frames were recorded in another environment ({differences}); "
                                        f"see {ENVIRONMENT.name} and regenerate with python -m tests.frames "
                                        "from a revision known to render correctly")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_frames_equal_the_golden_images(self):
        from PySide6.QtGui import QImage
        from tests.frames import FRAMES
        if self.environment_mismatch:
            self.skipTest(self.environment_mismatch)
        self.assertEqual(len(self.images), 20)
        for name, image in self.images.items():
            golden = QImage(str(FRAMES / name))
            self.assertFalse(golden.isNull(), name)
            self.assertTrue(golden.convertToFormat(image.format()) == image, f"{name} differs from the golden frame")

    def test_render_path_never_converts_an_index_array(self):
        self.assertTrue(self.searches)
        for array_dtype, key_dtype, contiguous, size in self.searches:
            if array_dtype.kind in "iu":
                self.assertIn(key_dtype.kind, "iu", f"float key searched in an int{array_dtype.itemsize * 8} index")
            # A strided view is copied by numpy on every call; indices are bisected instead.
            self.assertTrue(contiguous or size <= 1, f"strided {array_dtype} array of {size} searched")

    def test_frame_origin_is_computed_once_not_per_segment(self):
        # Before the viewport transform each sample_x made three time_offset_at calls,
        # tens of thousands for these frames; now they come from transforms and the ruler
        # (about 16 per frame, 20 frames).
        self.assertLess(self.offset_calls, 20 * 20)


class PixelRunsTests(unittest.TestCase):
    """WLF performance task 4: dense rows split into runs with numpy, equal to the old loop."""

    def check(self, starts, codes, edges):
        from ila_viewer.viewport import pixel_runs
        from tests.reference import reference_pixel_runs
        starts = np.asarray(starts, dtype=np.int64)
        codes = np.asarray(codes, dtype=np.uint32)
        indices = np.maximum(0, np.searchsorted(starts, np.asarray(edges), side="right") - 1)
        runs = pixel_runs(indices, codes[indices[:-1]])
        actual = list(zip(*(part.tolist() for part in runs)))
        self.assertEqual(actual, reference_pixel_runs(indices, codes, len(indices) - 1))
        return actual

    def test_random_rows_equal_the_scalar_loop(self):
        rng = np.random.default_rng(11)
        for trial in range(60):
            count = int(rng.integers(2, 5000))
            starts = np.unique(np.concatenate([[0], rng.integers(1, count, int(rng.integers(0, count)))]))
            # Few distinct codes, so quiet neighbours often repeat a value; code 2 stands for X.
            codes = rng.integers(0, int(rng.integers(1, 4)), len(starts))
            width = int(rng.integers(1, 400))
            edges = np.linspace(0, count, width + 1) if trial % 2 else np.sort(rng.uniform(0, count, width + 1))
            self.check(starts, codes, edges)

    def test_entirely_busy_quiet_and_unknown_rows(self):
        width = 50
        edges = np.linspace(0, 1000, width + 1)
        self.assertEqual(self.check(np.arange(1000), np.arange(1000) % 2, edges), [(0, width, True, 0)])
        self.assertEqual(self.check([0], [1], edges), [(0, width, False, 1)])
        # An X run between two quiet values splits the quiet pixels by code.
        runs = self.check([0, 400, 600], [0, 2, 0], edges)
        self.assertEqual([(busy, code) for _, _, busy, code in runs],
                         [(False, 0), (True, 0), (False, 2), (True, 2), (False, 0)])

    def test_row_painter_paints_like_the_old_per_run_drawing(self):
        # RowPainter (vectorized 1-bit lines, and run-by-run segments for buses and fills)
        # must paint exactly what the old draw_segment did with a pen and draw call per run,
        # including unknown runs between known ones and runs narrower than a pixel.
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtGui import QColor, QImage, QPainter
        from PySide6.QtWidgets import QApplication
        from ila_viewer.rendering import RowPainter
        from ila_viewer.themes import ThemeManager
        from tests.reference import reference_draw_segment
        QApplication.instance() or QApplication([])
        rng = np.random.default_rng(5)
        for trial in range(48):
            count = int(rng.integers(1, 600))
            is_bus = trial % 4 == 3
            values = [0, 5, 10, 255, "X", "1X"] if is_bus else [0, 1, "X", "Z"]
            known = 4 if is_bus else 2
            weights = np.r_[np.full(known, 0.9 / known), np.full(len(values) - known, 0.1 / (len(values) - known))]
            codes = rng.choice(len(values), count, p=weights) if trial % 3 else rng.integers(0, known, count)
            scale = 920 if trial % 2 else 9000  # sub-pixel runs, or wide runs with labels
            edges = np.sort(rng.uniform(-20, scale, count + 1))
            x1, x2 = edges[:-1], edges[1:]
            transitions = rng.random(count) < 0.9
            sig = SimpleNamespace(values=values, is_bus=is_bus, format_value=lambda value, radix: str(value))
            preferences = ViewerPreferences(fill_bus=bool(trial // 2 % 2), fill_high=bool(trial // 8 % 2))
            view = SimpleNamespace(preferences=preferences, themes=ThemeManager(),
                                   display_radix=lambda signal: "HEX", signal_color=lambda signal: QColor("#61dfb5"))
            images = []
            for new_code in (True, False):
                image = QImage(900, 40, QImage.Format.Format_ARGB32)
                image.fill(QColor("#161618"))
                painter = QPainter(image)
                if new_code:
                    row = RowPainter(view, painter, sig, (12, 28))
                    if not (row.levels(codes, x1, x2, transitions) or row.shapes(codes, x1, x2)):
                        for code, a, b, step in zip(codes.tolist(), x1.tolist(), x2.tolist(), transitions.tolist()):
                            row.segment(code, a, b, step)
                    row.finish()
                else:
                    for code, a, b, step in zip(codes.tolist(), x1.tolist(), x2.tolist(), transitions.tolist()):
                        reference_draw_segment(painter, view, sig, code, a, b, step, (12, 28))
                painter.end()
                images.append(image)
            self.assertTrue(images[0] == images[1], f"trial {trial}")

    def test_empty_width(self):
        from ila_viewer.viewport import pixel_runs
        self.assertEqual([len(part) for part in pixel_runs(np.array([0]), np.array([], dtype=np.uint32))], [0] * 4)


class ViewportTransformTests(unittest.TestCase):
    """WLF performance task 3: one transform per frame, equal to the old scalar mapping."""

    def setUp(self):
        from tests.frames import irregular_vcd, dense_csv
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        folder = Path(self.temp.name)
        irregular_vcd(folder / "irregular.vcd", count=3000)
        dense_csv(folder / "dense.csv", count=3000)
        self.vcd = VcdCaptureReader().load(folder / "irregular.vcd")
        self.addCleanup(self.vcd.close)
        self.csv = load_csv(folder / "dense.csv")
        self.addCleanup(self.csv.close)

    def positions(self, capture, left, span):
        rng = np.random.default_rng(3)
        last = capture.count - 1
        return [left, left + span, 0, last, last - 0.5, last + 0.75, -0.25, float(int(left) + 1),
                *rng.uniform(left - span, left + 2 * span, 60).tolist(), *range(int(left), int(left + span) + 2)]

    def test_x_xs_and_inverse_equal_the_scalar_reference(self):
        from ila_viewer.viewport import TimeTransform, UniformTransform, viewport_transform
        from tests.reference import reference_sample_x, reference_x_sample
        for capture, kind in ((self.vcd, TimeTransform), (self.csv, UniformTransform)):
            for left, span in ((0, capture.count), (1234.37, 97.3), (2999 - 40.5, 40.5), (17, 2)):
                for plot_left, plot_width in ((370, 700), (0, 1)):
                    t = viewport_transform(capture, left, span, plot_left, plot_width)
                    self.assertIsInstance(t, kind)
                    samples = self.positions(capture, left, span)
                    expected = [reference_sample_x(capture, left, span, plot_left, plot_width, s) for s in samples]
                    self.assertEqual([t.x(s) for s in samples], expected)
                    self.assertEqual(t.xs(np.asarray(samples, dtype=np.float64)).tolist(), expected)
                    for x in (plot_left, plot_left + plot_width, plot_left + plot_width / 3, plot_left - 5.5):
                        self.assertEqual(t.sample(x), reference_x_sample(capture, left, span, plot_left, plot_width, x))
                    edges = t.edges
                    self.assertIs(t.edges, edges)  # computed once per transform
                    self.assertEqual(len(edges), plot_width + 1)

    def test_view_transform_is_cached_until_the_geometry_changes(self):
        from ila_viewer.view import WaveformView
        view = SimpleNamespace(capture=self.vcd, left=10.0, span=200.0, plot_left=300, plot_width=500)
        first = WaveformView.transform(view)
        self.assertIs(WaveformView.transform(view), first)
        view.left = 11.0
        second = WaveformView.transform(view)
        self.assertIsNot(second, first)
        view.plot_width = 501
        self.assertIsNot(WaveformView.transform(view), second)
        view.capture = self.csv
        self.assertEqual(WaveformView.sample_x(view, 11.0), 300.0)

    def test_vectorized_samples_at_offsets_equal_the_scalar_reference(self):
        from tests.reference import reference_samples_at_offsets
        capture = self.vcd
        times = capture.times
        rng = np.random.default_rng(9)
        span = int(times[-1] - times[0])
        for origin in (int(times[0]), int(times[1500]), int(times[-1])):
            offsets = np.r_[rng.uniform(-span, 2 * span, 500), (times[:50] - origin).astype(np.float64),
                            -1e30, 1e30, -0.5, 0.0, 0.999999]
            np.testing.assert_array_equal(capture.samples_at_offsets(offsets, origin),
                                          reference_samples_at_offsets(capture, offsets, origin))
            grid = offsets[:500].reshape(20, 25)
            np.testing.assert_array_equal(capture.samples_at_offsets(grid, origin),
                                          reference_samples_at_offsets(capture, grid, origin))
        np.testing.assert_array_equal(capture.samples_at_offsets([12.5]), reference_samples_at_offsets(capture, [12.5]))


class VcdBusAssemblyTests(unittest.TestCase):
    """WLF performance task 5: split buses built after the parse equal the per-timestamp build."""

    HEADER = ("$timescale 1ns $end\n$scope module top $end\n{vars}$upscope $end\n$enddefinitions $end\n")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def write(self, name, variables, body):
        path = Path(self.temp.name) / name
        path.write_text(self.HEADER.format(vars="".join(f"$var {v} $end\n" for v in variables)) + body,
                        encoding="ascii")
        return path

    def assert_same(self, path, **patches):
        from ila_viewer import vcd
        from tests.reference import reference_load_vcd
        from tests.vcd_compare import capture_differences
        expected = reference_load_vcd(path)
        self.addCleanup(expected.close)
        with patch.multiple(vcd.SplitBusAssembler, **patches.get("assembler", {"BLOCK_CELLS": 1 << 21})), \
                patch.multiple(vcd.RawTimeline, **patches.get("timeline", {"SPILL": 8192})):
            actual = VcdCaptureReader().load(path)
        self.addCleanup(actual.close)
        self.assertEqual(capture_differences(actual, expected), [])
        return actual

    def test_repository_and_synthetic_captures(self):
        from scripts.benchmark_wlf import SyntheticVcd
        synthetic = Path(self.temp.name) / "synthetic.vcd"
        SyntheticVcd(3000).write(synthetic)
        # Small blocks and spills: block seams and disk-backed timelines change nothing.
        for path, block, spill in ((Path(__file__).resolve().parents[1] / "vsim.vcd", 1 << 16, 4096),
                                   (synthetic, 400, 5)):
            if not path.exists():
                continue
            with self.subTest(path=path.name):
                capture = self.assert_same(path)
                self.assertTrue(capture.split_buses)
                self.assert_same(path, assembler={"BLOCK_CELLS": block}, timeline={"SPILL": spill})

    def test_edge_cases(self):
        bits = [f"wire 1 {chr(33 + n)} d [{n}]" for n in (0, 2, 1, 3)]  # declared out of order
        cases = {
            "out_of_order": (bits, "#0\n0!\n1#\nx\"\n0$\n#5\n1!\n#9\nz#\n"),
            # Bit 3 is never dumped, so it stays x; two changes of one bit in one timestamp.
            "undumped_and_double": ([f"wire 1 {chr(33 + n)} q [{n}]" for n in range(4)],
                                    "#0\n0!\n1\"\n0#\n#3\n1!\n0!\n#4\n1!\n#7\nX\"\nZ#\n"),
            # The same timestamp written twice continues one row.
            "repeated_timestamp": ([f"wire 1 {chr(33 + n)} r [{n}]" for n in range(3)],
                                   "#0\n0!\n0\"\n0#\n#2\n1!\n#2\n1\"\n#6\n0!\n0\"\n#6\n1#\n"),
            # A vector-form change of a 1-bit member, longer than one character.
            "multi_character": ([f"reg 1 {chr(33 + n)} m [{n}]" for n in range(3)],
                                "#0\nb0 !\nb0 \"\nb0 #\n#1\nb01 !\n#2\nb1 \"\n#3\nbx #\n"),
            # Every value repeats: the bus must not record unchanged rows.
            "no_change": ([f"wire 1 {chr(33 + n)} n [{n}]" for n in range(2)],
                          "#0\n1!\n1\"\n#1\n1!\n#2\n1\"\n1!\n"),
        }
        for name, (variables, body) in cases.items():
            with self.subTest(name):
                capture = self.assert_same(self.write(f"{name}.vcd", variables, body))
                self.assertEqual(len(capture.split_buses), 1)

    def test_cancel_during_bus_assembly_releases_every_file(self):
        from ila_viewer import vcd
        from ila_viewer.data import LoadCancelled
        from scripts.benchmark_wlf import SyntheticVcd
        path = Path(self.temp.name) / "cancel.vcd"
        SyntheticVcd(2000).write(path)
        folders = []
        original = TempStorage.create

        def recording(*args, **kwargs):
            folders.append(original(*args, **kwargs))
            return folders[-1]

        state = {"building": False}
        build = vcd.SplitBusAssembler.build

        def building(assembler, *args, **kwargs):
            state["building"] = True
            return build(assembler, *args, **kwargs)

        for spill in (5, 8192):  # timelines on disk, and in memory
            with self.subTest(spill=spill), \
                    patch.object(TempStorage, "create", recording), \
                    patch.object(vcd.SplitBusAssembler, "build", building), \
                    patch.object(vcd.RawTimeline, "SPILL", spill), \
                    patch.object(vcd.SplitBusAssembler, "BLOCK_CELLS", 64):
                state["building"] = False
                with self.assertRaises(LoadCancelled):
                    VcdCaptureReader().load(path, cancel=lambda: state["building"])
                # TempFolder.cleanup removed the folder, so no file stayed open.
                self.assertFalse(Path(folders[-1].name).exists())


class DiskSpaceTests(unittest.TestCase):
    """WLF performance task 7: a full disk fails cleanly, and conversion reports its progress."""

    VCD = "$timescale 1ns $end\n$var wire 1 ! a $end\n$enddefinitions $end\n#0\n0!\n#5\n1!\n"

    class Disk:
        """Injected free-space probe: a fixed amount, or one per call from a list."""

        def __init__(self, *free):
            self.values, self.calls = list(free), []

        def free(self, folder):
            self.calls.append(Path(folder))
            return self.values.pop(0) if len(self.values) > 1 else self.values[0]

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def recorded_folders(self, module):
        folders = []
        original = TempStorage.create

        def recording(*args, **kwargs):
            folders.append(original(*args, **kwargs))
            return folders[-1]

        return folders, patch.object(TempStorage, "create", recording)

    def assert_space_message(self, error):
        text = str(error)
        self.assertIn("Недостаточно места на диске", text)
        self.assertIn(str(Path(tempfile.gettempdir())), text)
        self.assertRegex(text, r"Нужно не меньше \d+(,\d)? [МГ]Б, свободно \d+(,\d)? [МГ]Б\.")

    def test_vcd_refuses_early_when_the_index_cannot_fit(self):
        from ila_viewer import vcd
        from ila_viewer.diskspace import DiskSpaceError
        path = self.folder / "big.vcd"
        path.write_text(self.VCD + "#9\n0!\n" * 2000, encoding="ascii")
        disk = self.Disk(1000)
        folders, recording = self.recorded_folders(vcd)
        with recording, self.assertRaises(DiskSpaceError) as caught:
            VcdCaptureReader(disk=disk).load(path)
        self.assert_space_message(caught.exception)
        self.assertEqual(folders, [])  # refused before any temporary file existed
        self.assertEqual(disk.calls, [Path(tempfile.gettempdir())])

    def test_vcd_full_disk_while_writing_removes_every_file(self):
        from ila_viewer import data, vcd
        from ila_viewer.diskspace import DiskSpaceError
        path = self.folder / "full.vcd"
        path.write_text(self.VCD, encoding="ascii")
        folders, recording = self.recorded_folders(vcd)
        for error in (OSError(28, "No space left on device"), OSError(None, "disk full", None, 112)):
            with self.subTest(error=error), recording, \
                    patch.object(data._Writer, "flush", side_effect=error), self.assertRaises(DiskSpaceError) as caught:
                VcdCaptureReader(disk=self.Disk(10 << 30, 5 << 20)).load(path)
            self.assert_space_message(caught.exception)
            self.assertIs(caught.exception.__cause__, error)
            self.assertFalse(Path(folders[-1].name).exists())
        with patch.object(data._Writer, "flush", side_effect=PermissionError(13, "denied")), \
                self.assertRaises(PermissionError):
            VcdCaptureReader(disk=self.Disk(10 << 30)).load(path)  # other errors are not disk space

    def converter(self, steps, chunk=b"0!\n1!\n" * 100):
        """A QuestaWlfConverter whose runner grows the target in ``steps`` and polls ``cancel``."""
        from ila_viewer.data import LoadCancelled
        from ila_viewer.sources import QuestaWlfConverter
        self.killed = False

        def runner(command, cancel):
            target = Path(command[3])
            target.write_text(self.VCD, encoding="ascii")
            try:
                for _ in range(steps):
                    with target.open("ab") as stream:
                        stream.write(chunk)
                    if cancel():
                        raise LoadCancelled()
            except BaseException:
                self.killed = True  # the real runner kills the process here
                raise
            return 0, ""

        return QuestaWlfConverter("wlf2vcd", None, runner)

    def test_wlf_conversion_reports_its_growing_output(self):
        from ila_viewer.progress import CONVERTING_WLF
        from ila_viewer.sources import ConversionWatch, WlfCaptureSource
        source = self.folder / "capture.wlf"
        source.write_bytes(b"wlf")
        reports = []
        with patch.object(ConversionWatch, "INTERVAL", 0):
            capture = WlfCaptureSource(self.converter(5), disk=self.Disk(10 << 30)).load(
                source, lambda percent, amount, phase=None: reports.append((percent, amount, phase)))
        self.addCleanup(capture.close)
        phases = [(amount, phase) for percent, amount, phase in reports if phase is not None]
        self.assertEqual(len(phases), 5)
        self.assertTrue(all(phase is CONVERTING_WLF for _, phase in phases))
        sizes = [amount for amount, _ in phases]
        self.assertEqual(sizes, sorted(sizes))
        self.assertGreater(sizes[0], 0)
        self.assertTrue(all(percent == -1 for percent, _, phase in reports if phase is not None))
        self.assertEqual(reports[-1][:2], (100, capture.count))  # then the parse reports rows
        self.assertEqual(CONVERTING_WLF.describe(1288490189), "Преобразование WLF: 1,2 ГБ")

    def test_wlf_conversion_stops_before_the_disk_is_full(self):
        from ila_viewer import sources
        from ila_viewer.diskspace import CONVERSION_RESERVE, DiskSpaceError
        source = self.folder / "capture.wlf"
        source.write_bytes(b"wlf")
        folders, recording = self.recorded_folders(sources)
        disk = self.Disk(10 << 30, 10 << 30, CONVERSION_RESERVE - 1)
        with recording, patch.object(sources.ConversionWatch, "INTERVAL", 0), \
                self.assertRaises(DiskSpaceError) as caught:
            sources.WlfCaptureSource(self.converter(50), disk=disk).load(source)
        self.assertTrue(self.killed)
        self.assert_space_message(caught.exception)
        self.assertFalse(Path(folders[-1].name).exists())  # the partial VCD is gone
        # wlf2vcd failing by itself on a nearly full disk gives the same message.

        class Failing:
            def convert(self, original, target, cancel):
                target.write_bytes(b"partial")
                raise RuntimeError("wlf2vcd завершился с кодом 1.")

        with recording, self.assertRaises(DiskSpaceError):
            sources.WlfCaptureSource(Failing(), disk=self.Disk(1 << 20)).load(source)
        self.assertFalse(Path(folders[-1].name).exists())
        with self.assertRaisesRegex(RuntimeError, "кодом 1"):
            sources.WlfCaptureSource(Failing(), disk=self.Disk(10 << 30)).load(source)

    def test_window_shows_a_named_phase(self):
        from PySide6.QtWidgets import QApplication
        from ila_viewer.cpu import CpuTopology
        from ila_viewer.progress import CONVERTING_WLF
        from ila_viewer.window import MainWindow
        from ila_viewer.workers import BackgroundTask
        QApplication.instance() or QApplication([])
        window = MainWindow(preferences_store=PreferencesStore(), cpu=CpuTopology(2, 2))
        self.addCleanup(window.close)
        task = BackgroundTask(lambda task: None)
        received = []
        task.phase.connect(lambda phase, amount: received.append((phase, amount)))
        task.progress.connect(lambda percent, rows: received.append((percent, rows)))
        task.report(-1, 5 << 30, CONVERTING_WLF)
        task.report(40, 1000)
        self.assertEqual(received, [(CONVERTING_WLF, 5 << 30), (40, 1000)])
        window.load_phase(CONVERTING_WLF, 3 << 29)
        self.assertEqual(window.statusBar().currentMessage(), "Преобразование WLF: 1,5 ГБ")
        self.assertEqual((window.progress.minimum(), window.progress.maximum()), (0, 0))


class SlicedWlfConversionTests(unittest.TestCase):
    """WLF performance task 7: time slices converted in parallel join to the one-run capture.

    The fake tools behave as Questa 2024.1 was measured to: ``wlfman filter`` keeps
    ``[begin, end]``; a slice VCD starts with a row at ``begin`` listing every signal
    (then the events at ``begin``) and has no row at its end.
    """

    HEADER = ("$timescale 1ns $end\n$scope module top $end\n$var wire 1 ! a $end\n$var reg 4 \" v $end\n"
              "$var wire 1 # m [0] $end\n$var wire 1 $ m [1] $end\n$upscope $end\n$enddefinitions $end\n")
    END = 1003

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.wlf2vcd = self.folder / "wlf2vcd.exe"
        self.wlfman = self.folder / ("wlfman.exe" if os.name == "nt" else "wlfman")
        self.wlf2vcd.write_bytes(b"")
        self.wlfman.write_bytes(b"")
        self.source = self.folder / "capture.wlf"
        self.source.write_text("full", encoding="ascii")
        self.commands = []
        self.faults = {}
        # Events at multiples of 10 with an idle gap 400..700; a repeated change at 300.
        self.events = {}
        for t in range(0, self.END, 10):
            if 400 < t < 700:
                continue
            changes = [("!", str(t // 10 % 2)), ('"', format(t // 30 % 16, "b")), ("#", str(t // 20 % 2))]
            if t % 50 == 0:
                changes.append(("$", "x" if t == 250 else str(t // 50 % 2)))
            if t == 300:
                changes += [('"', "1111"), ("!", "1")]
            self.events[t] = changes

    def body(self, begin=None, end=None, slice_begin=None):
        lines, state = [], {"!": "x", '"': "x", "#": "x", "$": "x"}
        times = sorted(self.events)
        events = {t: list(changes) for t, changes in self.events.items()}
        # Faults in one slice's output: an event dropped or changed, a signal left out of the first row.
        for t, symbol in self.faults.get("drop_event", {}).get(slice_begin, ()):
            events[t] = [change for change in events[t] if change[0] != symbol]
        for t, symbol, value in self.faults.get("change_event", {}).get(slice_begin, ()):
            events[t] = [(name, value if name == symbol else old) for name, old in events[t]]
        if begin is not None:
            for t in times:
                if t < begin:
                    state.update(events[t])
            lines.append(f"#{begin}")
            for symbol in self.faults.get("drop_row", {}).get(slice_begin, ()):
                state.pop(symbol)
            # The first row writes vectors at full width, which the events need not do.
            lines += [("b" + value.rjust(4, value[0] if value[0] == "x" else "0") + " " + symbol)
                      if symbol == '"' else value + symbol for symbol, value in state.items()]
        for t in times:
            if begin is not None and not begin <= t <= end:
                continue
            if t != begin:
                lines.append(f"#{t}")
            lines += [("b" + value + " " + symbol) if symbol == '"' else value + symbol for symbol, value in events[t]]
        return "\r\n".join(lines) + "\r\n"

    def runner(self, command, cancel):
        self.commands.append(command)
        tool = Path(command[0]).name
        if cancel():
            from ila_viewer.data import LoadCancelled
            raise LoadCancelled()
        if tool.startswith("wlfman") and command[1] == "info":
            return self.faults.get("info", (0, f"# Start Time:  0+0\n# End Time:    {self.END}+0\n"))
        if tool.startswith("wlfman"):
            begin, end, out = int(command[3]), int(command[5]), Path(command[7])
            out.write_text(f"{begin} {end}", encoding="ascii")
            return self.faults.get("filter", {}).get(begin, (0, ""))
        source, out = Path(command[1]), Path(command[3])
        if source.resolve() == self.source.resolve():
            out.write_text(self.HEADER + self.body(), encoding="ascii")
            return 0, ""
        begin, end = map(int, source.read_text(encoding="ascii").split())
        header = self.faults.get("header", {}).get(begin, self.HEADER)
        shift = self.faults.get("shift", {}).get(begin, 0)
        # wlf2vcd writes the conversion time into $date, so slices differ there.
        date = f"$date\n\tSat Nov 14 15:12:{begin % 60:02d} 2026\n$end\n"
        out.write_text(date + header + self.body(begin + shift, end, begin), encoding="ascii")
        return 0, ""

    def convert(self, slices):
        from ila_viewer.sources import QuestaWlfConverter
        target = self.folder / "out" / "converted.vcd"
        target.parent.mkdir(exist_ok=True)
        for old in target.parent.iterdir():
            old.unlink()
        self.commands = []
        QuestaWlfConverter(str(self.wlf2vcd), None, self.runner, slices).convert(self.source, target)
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["converted.vcd"])
        sliced = any(Path(c[0]).name.startswith("wlfman") and c[1] == "filter" for c in self.commands)
        full = any(Path(c[1]) == self.source and Path(c[0]) == self.wlf2vcd for c in self.commands)
        return target, sliced, full

    def assert_same_as_one_run(self, slices, expect_sliced=True):
        from tests.vcd_compare import capture_differences
        reference = self.folder / "reference.vcd"
        reference.write_text(self.HEADER + self.body(), encoding="ascii")
        target, sliced, full = self.convert(slices)
        self.assertEqual((sliced and not full), expect_sliced, self.commands)
        actual, expected = VcdCaptureReader().load(target), VcdCaptureReader().load(reference)
        self.addCleanup(actual.close)
        self.addCleanup(expected.close)
        self.assertEqual(capture_differences(actual, expected), [])

    def test_slices_join_to_the_one_run_capture(self):
        for slices in (2, 3, 4, 7, 13):  # edges at events, between events and inside the idle gap
            with self.subTest(slices=slices):
                self.assert_same_as_one_run(slices)
        self.assertEqual(self.convert(1)[1:], (False, True))  # one slice: no wlfman at all

    def test_unsafe_slices_fall_back_to_one_run(self):
        edges = [self.END * k // 4 for k in range(4)]
        cases = {
            "info": {"info": (1, "error")},
            "no_time": {"info": (0, "# Signals: 4")},
            "filter": {"filter": {edges[2]: (3, "failed")}},
            "header": {"header": {edges[1]: self.HEADER.replace("1ns", "1ps")}},
            "shift": {"shift": {edges[3]: 1}},
        }
        for name, faults in cases.items():
            with self.subTest(name):
                self.faults = faults
                self.assert_same_as_one_run(4, expect_sliced=False)

    def load_through_source(self, slices=4):
        """Load ``self.source`` through WlfCaptureSource; returns (capture, phases reported, source)."""
        from ila_viewer.diskspace import DiskSpace
        from ila_viewer.sources import QuestaWlfConverter, WlfCaptureSource

        class Roomy(DiskSpace):
            def free(self, folder):
                return 1 << 50

        self.commands = []
        source = WlfCaptureSource(QuestaWlfConverter(str(self.wlf2vcd), None, self.runner, slices), disk=Roomy())
        phases = []
        capture = source.load(self.source, lambda p, n, phase=None: phases.append(phase))
        self.addCleanup(capture.close)
        return capture, phases, source

    def reference(self):
        reference = self.folder / "reference.vcd"
        reference.write_text(self.HEADER + self.body(), encoding="ascii")
        expected = VcdCaptureReader().load(reference)
        self.addCleanup(expected.close)
        return expected

    def single_runs(self):
        return sum(1 for c in self.commands if Path(c[0]) == self.wlf2vcd and Path(c[1]).resolve() == self.source.resolve())

    def test_seam_rows_state_the_capture_at_each_begin(self):
        from tests.vcd_compare import capture_differences
        from ila_viewer.progress import RECONVERTING_WLF
        for slices in (2, 4, 7, 13):  # seams at events, between events and in the idle gap
            with self.subTest(slices=slices):
                capture, phases, source = self.load_through_source(slices)
                self.assertEqual(capture_differences(capture, self.reference()), [])
                self.assertEqual((self.single_runs(), capture.notices), (0, []))
                self.assertNotIn(RECONVERTING_WLF, phases)
                self.assertEqual(source.timings["seams"], slices - 1)

    def test_a_seam_mismatch_reloads_through_one_wlf2vcd_run(self):
        from tests.vcd_compare import capture_differences
        from ila_viewer.progress import RECONVERTING_WLF
        edges = [self.END * k // 4 for k in range(4)]  # 0, 250, 501, 752; 501 lies in the idle gap
        cases = {
            # slice [250, 501] loses the last change of "!" before the seam at 501
            "event_removed": {"drop_event": {edges[1]: [(400, "!")]}},
            # ... or writes another value for the last change of the vector
            "event_changed": {"change_event": {edges[1]: [(400, '"', "0")]}},
            # slice [501, 752] does not list "#" in its first row (the joined capture is right)
            "signal_missing": {"drop_row": {edges[2]: ["#"]}},
        }
        for name, faults in cases.items():
            with self.subTest(name):
                self.faults = faults
                capture, phases, source = self.load_through_source(4)
                self.assertEqual(capture_differences(capture, self.reference()), [])
                self.assertEqual(self.single_runs(), 1)
                self.assertIn(RECONVERTING_WLF, phases)
                self.assertEqual(len(capture.notices), 1)
                self.assertTrue(capture.notices[0].startswith("Быстрое преобразование WLF по частям отклонено"),
                                capture.notices[0])
                self.assertIn("стык 2 из 3", capture.notices[0])
                self.assertGreaterEqual(source.timings["seam_mismatches"], 1)

    def test_a_rejected_capture_is_closed_and_never_returned(self):
        loaded = []
        original = VcdCaptureReader.load

        def remember(reader, *args, **kwargs):
            capture = original(reader, *args, **kwargs)
            loaded.append(capture)
            return capture

        self.faults = {"drop_event": {self.END // 4: [(400, "!")]}}
        with patch.object(VcdCaptureReader, "load", remember):
            capture, _, _ = self.load_through_source(4)
        self.assertEqual(len(loaded), 2)
        self.assertIs(loaded[1], capture)
        self.assertTrue(loaded[0].times._mmap.closed)

    def test_sliced_conversion_preference(self):
        from dataclasses import replace
        from PySide6.QtWidgets import QApplication
        from ila_viewer.cpu import CpuTopology
        from ila_viewer.settings_dialog import AppearanceDialog
        from ila_viewer.window import MainWindow
        QApplication.instance() or QApplication([])
        store = PreferencesStore()
        self.assertTrue(store.load().wlf_sliced)
        window = MainWindow(preferences_store=store, cpu=CpuTopology(4, 4))
        self.addCleanup(window.close)
        converter = window.source.readers[".wlf"].converter
        self.assertEqual(converter.slices, window.worker_count(window.preferences))
        dialog = AppearanceDialog(window.preferences, window.themes, window, cpu=CpuTopology(4, 4))
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(dialog.wlf_sliced_box.isChecked())
        dialog.wlf_sliced_box.setChecked(False)
        window.apply_preferences(dialog.result_preferences())
        self.assertEqual(converter.slices, 1)
        window.save_preferences()
        self.assertFalse(store.load().wlf_sliced)
        window.apply_preferences(replace(window.preferences, wlf_sliced=True))
        self.assertEqual(converter.slices, window.worker_count(window.preferences))

    def test_cancel_removes_every_slice(self):
        from ila_viewer.data import LoadCancelled
        from ila_viewer.sources import QuestaWlfConverter
        target = self.folder / "out" / "converted.vcd"
        target.parent.mkdir()
        calls = []

        def cancel():
            calls.append(1)
            return len(calls) > 6  # after info and a few slices started

        with self.assertRaises(LoadCancelled):
            QuestaWlfConverter(str(self.wlf2vcd), None, self.runner, 4).convert(self.source, target, cancel)
        self.assertEqual(list(target.parent.iterdir()), [])


class VcdParallelParseTests(unittest.TestCase):
    """WLF performance task 6: the VCD body parsed in byte ranges equals the one-process parse."""

    HEADER = VcdBusAssemblyTests.HEADER

    @classmethod
    def setUpClass(cls):
        cls.pool = WorkerPool(3)

    @classmethod
    def tearDownClass(cls):
        cls.pool.shutdown()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def write(self, name, variables, body, header=None):
        path = Path(self.temp.name) / name
        text = (header or self.HEADER).format(vars="".join(f"$var {v} $end\n" for v in variables)) + body
        path.write_bytes(text.encode("ascii"))
        return path

    @staticmethod
    def offset(path, marker, occurrence=0):
        """Byte offset of the ``occurrence``-th line ``marker`` (e.g. ``#5``) in ``path``."""
        data, position = path.read_bytes(), -1
        for _ in range(occurrence + 1):
            position = data.index(b"\n" + marker.encode() + b"\n", position + 1)
        return position + 1

    def parallel(self, path, starts=None, ranges=8, segment=3):
        """Load with the pool and forced small ranges; returns (capture or exception, timings)."""
        from ila_viewer import vcd, vcd_chunks
        reader = VcdCaptureReader(self.pool)
        patches = [patch.object(vcd, "MIN_PARALLEL_CHUNK", 1), patch.object(vcd, "RANGES_PER_WORKER", ranges),
                   patch.object(vcd_chunks, "SEGMENT", segment)]
        if starts is not None:
            body = vcd_chunks.header_end(path)
            patches.append(patch.object(vcd_chunks, "range_starts", lambda *args, **kwargs: [body, *starts]))
        for item in patches:
            item.start()
        try:
            capture = reader.load(path)
        finally:
            for item in reversed(patches):
                item.stop()
        self.addCleanup(capture.close)
        return capture, reader.timings

    def assert_parallel_equal(self, path, starts=None, expect_split=True, **kwargs):
        from tests.reference import reference_load_vcd
        from tests.vcd_compare import capture_differences
        actual, timings = self.parallel(path, starts, **kwargs)
        self.assertEqual("plan" in timings, expect_split, timings)
        if expect_split:
            self.assertGreater(timings["ranges"], 1)
            self.assertFalse([p for p in Path(actual.storage.name).iterdir() if p.name.startswith("part")
                              or p.name.endswith((".raw_starts", ".raw_codes"))])
        for oracle in (VcdCaptureReader().load, reference_load_vcd):
            expected = oracle(path)
            self.addCleanup(expected.close)
            self.assertEqual(capture_differences(actual, expected), [])
        return actual, timings

    def assert_same_error(self, path, starts=None, message=None):
        with self.assertRaises(ValueError) as single:
            VcdCaptureReader().load(path)
        from ila_viewer import vcd, vcd_chunks
        body = vcd_chunks.header_end(path)
        with patch.object(vcd, "MIN_PARALLEL_CHUNK", 1), patch.object(vcd_chunks, "SEGMENT", 3), \
                patch.object(vcd_chunks, "range_starts", lambda *args, **kwargs: [body, *starts]):
            reader = VcdCaptureReader(self.pool)
            with self.assertRaises(ValueError) as parallel:
                reader.load(path)
        self.assertEqual(str(parallel.exception), str(single.exception))
        self.assertIn("parse", reader.timings)  # the error came from the parallel path itself
        if message:
            self.assertIn(message, str(single.exception))

    def test_repository_and_synthetic_captures(self):
        from scripts.benchmark_wlf import SyntheticVcd
        synthetic = Path(self.temp.name) / "synthetic.vcd"
        SyntheticVcd(3000).write(synthetic)
        for path in (Path(__file__).resolve().parents[1] / "vsim.vcd", synthetic):
            if path.exists():
                with self.subTest(path=path.name):
                    capture, timings = self.assert_parallel_equal(path, ranges=8, segment=64)
                    self.assertGreaterEqual(timings["ranges"], 20)
                    self.assertTrue(capture.split_buses)

    def test_boundary_rule_skips_identifiers_after_values(self):
        from ila_viewer.vcd_chunks import boundary_in
        cases = {
            b"x\nb0 #\n#10\n": 7,           # '#' after a vector value is an identifier
            b"x\nb0\n#\n#10\n": 7,          # ... also on its own line
            b"x\nr1.5 #\nS7 #\n#3\n": 14,   # after real and string values too
            b"x\nb0 # #5\n": 7,             # an identifier '#' does not hide the timestamp after it
            b"x\n1#\n#4\n": 5,              # scalar change of identifier '#'
            b"x\n#a\n#1x\n#7\n": 9,         # only '#' and digits is a timestamp
            b"x\n\xc2\xa0#5\n#6\n#7\n": 10,  # non-ASCII near a candidate (U+00A0 is whitespace)
            b"x\nB1 #\n#0000000000000000001\n#2\n": 28,  # more than 18 digits is skipped
        }
        for window, expected in cases.items():
            with self.subTest(window=window):
                self.assertEqual(boundary_in(window, 100, 101), 100 + expected)
        self.assertIsNone(boundary_in(b"#5\n", 0, 0))      # no preceding token in the window
        self.assertIsNone(boundary_in(b"x\ny\n#5", 0, 1))  # the token may continue past the window

    def test_boundaries_after_vector_values_with_hash_identifiers(self):
        variables = ["reg 1 # h", "reg 4 #! v", "wire 1 % w", 'reg 3 #" s'] + \
                    [f"wire 1 {chr(40 + n)} bus [{n}]" for n in range(3)]
        lines = []
        for t in range(400):
            lines.append(f"#{t * 10}")
            lines.append(f"b{t % 2} #")
            if t % 3 == 0:
                lines.append(f"b{t % 16:b} #!")
            lines.append(f"{t % 2}%")
            lines.append('bx #"' if t % 7 == 0 else f'b{t % 8:b} #"')
            lines.append(f"{(t >> 1) & 1}(")
            if t % 5 == 0:
                lines.append(f"{(t >> 2) & 1}*")
        path = self.write("hash.vcd", variables, "\r\n".join(lines) + "\r\n")
        capture, timings = self.assert_parallel_equal(path, ranges=16)
        self.assertGreaterEqual(timings["ranges"], 30)
        from ila_viewer import vcd, vcd_chunks
        body = vcd_chunks.header_end(path)
        data = path.read_bytes()
        starts = vcd_chunks.range_starts(path, body, len(data), 48)
        self.assertGreater(len(starts), 30)
        for start in starts[1:]:
            # Every start is a timestamp line, never the identifier of "b0 #".
            self.assertEqual(data[start - 1:start], b"\n")
            self.assertRegex(data[start:data.index(b"\r", start)].decode(), r"^#\d+$")

    def test_last_change_wins_inside_ranges_and_at_seams(self):
        variables = ["wire 1 ! a", "reg 4 \" v", "wire 1 # m [0]", "wire 1 $ m [1]", "wire 1 % w", "wire 1 % w2"]
        body = ("#0\n1!\n0!\nb1 \"\nb1111 \"\n0#\n1#\n0$\n"  # duplicates in the all-X first row
                "#1\n0!\n1!\n0!\n"                         # three changes, the last one wins
                "#2\nb1010 \"\nb0101 \"\n1#\n0#\n1%\n0%\n"  # 1010 only appears replaced: never a value
                "#3\n1!\nb0101 \"\n"
                "#4\n0!\n1!\nb0110 \"\nb0101 \"\n1$\n0$\n"  # seam below: duplicates in a range's last row
                "#5\n1!\n0!\nb1100 \"\nb0011 \"\n0#\n1#\n1%\n0%\n"  # ... and in the next range's first row
                "#6\nb0011 \"\n0!\n"
                "#7\nbx \"\nb11 \"\n1%\n")
        path = self.write("duplicates.vcd", variables, body)
        starts = [self.offset(path, "#2"), self.offset(path, "#5"), self.offset(path, "#7")]
        capture, _ = self.assert_parallel_equal(path, starts, segment=1)
        vector = next(s for s in capture.signals if s.name.endswith("v"))
        self.assertNotIn(0b1010, vector.values)
        self.assertNotIn(0b1100, vector.values)
        # One segment per entry also puts a spill right before each replaced change.
        self.assert_parallel_equal(path, starts, segment=2)

    def test_fallbacks_parse_on_one_process(self):
        variables = ["wire 1 ! a", "reg 2 \" v"]
        body = "#0\n0!\nb0 \"\n#5\n1!\n#5\nb1 \"\n#9\n$comment #12 b1 # $end\n0!\n#12\n1!\n"
        comment = self.write("comment.vcd", variables, body)
        self.assert_parallel_equal(comment, [self.offset(comment, "#5")], expect_split=False)
        repeated = self.write("repeated.vcd", variables, body.replace("$comment #12 b1 # $end\n", ""))
        # A range starting with the timestamp that ended the previous one continues its row.
        self.assert_parallel_equal(repeated, [self.offset(repeated, "#5", 1)], expect_split=False)
        self.assert_parallel_equal(repeated, [self.offset(repeated, "#9")])
        # "$enddefinitions" inside a header comment does not end the header.
        header = self.HEADER.replace("$enddefinitions $end", "$comment $enddefinitions $end\n$enddefinitions $end")
        commented = self.write("header.vcd", variables, body.replace("$comment #12 b1 # $end\n", ""), header)
        self.assert_parallel_equal(commented, [self.offset(commented, "#9")])

    def test_errors_are_the_single_process_errors(self):
        variables = ["wire 1 ! a", "reg 2 \" v"]
        cases = {
            # Timestamps decreasing across a seam.
            "seam": ("#0\n0!\n#5\n1!\n#3\n0!\n#8\n1!\n", "#3", "не по возрастанию"),
            # A token error in a range's first row comes before the seam check ...
            "token_first": ("#0\n0!\n#5\n1!\n#3\nq!\n#8\n1!\n", "#3", "Некорректное изменение"),
            # ... but a value error there is found only when the row is committed, after it.
            "value_first": ("#0\n0!\n#5\n1!\n#3\nb111 \"\n#8\n1!\n", "#3", "не по возрастанию"),
            "width": ("#0\n0!\n#5\n1!\n#7\nb111 \"\n#8\n1!\n", "#7", "выходит за ширину"),
            "unfinished": ("#0\n0!\n#5\n1!\n#7\n0!\nb1", "#7", "Незавершённое изменение"),
            "early_range": ("#0\nb111 \"\n#5\n1!\n#7\nq!\n", "#5", "выходит за ширину"),
        }
        for name, (body, marker, message) in cases.items():
            with self.subTest(name):
                path = self.write(f"{name}.vcd", variables, body)
                self.assert_same_error(path, [self.offset(path, marker)], message)
        # A bad value replaced within its timestamp is no error, in both parsers.
        path = self.write("replaced.vcd", variables, "#0\n0!\n#5\nb111 \"\nb11 \"\n#7\n1!\nb0 \"\n")
        self.assert_parallel_equal(path, [self.offset(path, "#5"), self.offset(path, "#7")])

    def test_cancel_releases_every_file(self):
        from ila_viewer import vcd
        from ila_viewer.data import LoadCancelled
        from scripts.benchmark_wlf import SyntheticVcd
        path = Path(self.temp.name) / "cancel.vcd"
        SyntheticVcd(4000).write(path)
        folders = []
        original = TempStorage.create

        def recording(*args, **kwargs):
            folders.append(original(*args, **kwargs))
            return folders[-1]

        submit = self.pool.submit
        for phase in ("parse", "buses"):
            state = {"cancel": False}

            def submitting(function, *args):
                # Cancel as soon as the first range (or the first bus) is handed to a worker.
                if phase == "parse" or function is vcd.build_bus:
                    state["cancel"] = True
                return submit(function, *args)

            with self.subTest(phase=phase), patch.object(TempStorage, "create", recording), \
                    patch.object(vcd, "MIN_PARALLEL_CHUNK", 1), patch.object(vcd, "RANGES_PER_WORKER", 8), \
                    patch.object(self.pool, "submit", submitting):
                reader = VcdCaptureReader(self.pool)
                with self.assertRaises(LoadCancelled):
                    reader.load(path, cancel=lambda: state["cancel"])
                self.assertEqual("place" in reader.timings, phase == "buses")
                # The folder is gone, so every worker closed its part and index files.
                self.assertFalse(Path(folders[-1].name).exists())
        capture, _ = self.parallel(path)  # the pool still works afterwards
        self.assertEqual(capture.count, 4000)

    def test_full_disk_before_placement_refuses_cleanly(self):
        from ila_viewer import vcd
        from ila_viewer.diskspace import DiskSpaceError
        from scripts.benchmark_wlf import SyntheticVcd
        path = Path(self.temp.name) / "space.vcd"
        SyntheticVcd(3000).write(path)
        checks = []

        class Disk:
            def free(self, folder):
                checks.append(Path(folder))
                return 10 << 30 if len(checks) == 1 else 1 << 20  # enough at the start, then not

        folders = []
        original = TempStorage.create

        def recording(*args, **kwargs):
            folders.append(original(*args, **kwargs))
            return folders[-1]

        with patch.object(vcd, "MIN_PARALLEL_CHUNK", 1), patch.object(TempStorage, "create", recording):
            reader = VcdCaptureReader(self.pool, Disk())
            with self.assertRaises(DiskSpaceError) as caught:
                reader.load(path)
        self.assertIn("parse", reader.timings)
        self.assertNotIn("place", reader.timings)  # refused before placement started
        self.assertIn(str(Path(tempfile.gettempdir())), str(caught.exception))
        self.assertIn("Нужно не меньше", str(caught.exception))
        self.assertFalse(Path(folders[-1].name).exists())

    def test_sources_pass_the_pool_to_the_vcd_reader(self):
        from ila_viewer.sources import AutoCaptureSource
        source = AutoCaptureSource(pool=self.pool)
        self.assertIs(source.readers[".vcd"].reader.pool, self.pool)
        self.assertIs(source.readers[".wlf"].reader.pool, self.pool)
        self.assertIsNone(AutoCaptureSource().readers[".vcd"].reader.pool)


class TempStorageTests(unittest.TestCase):
    """WLF performance task 10: temporary folders carry a lock, and stale ones are swept.

    Every test works on its own root, never on the system temporary folder.
    """

    CHILD = """
import sys, time
sys.path.insert(0, {repo!r})
from ila_viewer.tempstore import TempStorage
storage = TempStorage({root!r})
folder = storage.create("ila_vcd_")
open(folder.name + "/0.codes", "wb").write(b"x" * 100)
report = storage.sweep() if {sweep!r} else None
print(folder.name, sorted(report.kept) if report else [], flush=True)
sys.stdin.readline()
"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.root.mkdir()
        self.outside = Path(self.temp.name) / "outside"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("keep", encoding="ascii")

    def storage(self, age_offset=0):
        from ila_viewer.tempstore import TempStorage
        return TempStorage(self.root, clock=lambda: time.time() + age_offset)

    def child(self, sweep=False):
        import subprocess
        import sys
        code = self.CHILD.format(repo=str(Path(__file__).resolve().parents[1]), root=str(self.root), sweep=sweep)
        process = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   text=True)
        self.addCleanup(lambda: (process.kill(), process.wait(), process.stdin.close(), process.stdout.close()))
        line = process.stdout.readline().split(" ", 1)
        return process, Path(line[0]), line[1].strip()

    def unmarked(self, name, files=("0.codes",)):
        folder = self.root / name
        folder.mkdir()
        for file in files:
            (folder / file).write_bytes(b"data")
        return folder

    def link(self, name, target):
        """A directory junction on Windows (no privilege needed), a symbolic link elsewhere."""
        import subprocess
        path = self.root / name if not isinstance(name, Path) else name
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(target)], check=True, capture_output=True)
        else:
            os.symlink(target, path, target_is_directory=True)
        return path

    def test_a_folder_of_a_killed_owner_is_removed(self):
        process, folder, _ = self.child()
        self.assertEqual(self.storage().sweep().kept, {folder.name: "owner alive"})
        self.assertTrue((folder / "0.codes").exists())
        process.kill()
        process.wait()
        report = self.storage().sweep()
        self.assertEqual(report.removed, [folder.name])
        self.assertGreaterEqual(report.removed_bytes, 100)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_folders_of_live_owners_survive(self):
        own = self.storage().create("ila_wave_")
        self.addCleanup(own.cleanup)
        # Another instance sweeps while this one holds a folder, and the other way round.
        process, theirs, kept_by_child = self.child(sweep=True)
        self.assertEqual(kept_by_child, str(sorted([Path(own.name).name, theirs.name])))
        report = self.storage().sweep()
        self.assertEqual(report.removed, [])
        self.assertEqual(report.kept, {Path(own.name).name: "owner alive", theirs.name: "owner alive"})
        self.assertTrue(Path(own.name).is_dir() and theirs.is_dir())
        own.cleanup()
        self.assertFalse(Path(own.name).exists())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [theirs.name])

    def test_unmarked_folders_go_only_when_old_and_unused(self):
        old = self.unmarked("ila_vcd_old")
        recent = self.unmarked("ila_wave_recent")
        os.utime(recent / "0.codes", (time.time() + 2 * 24 * 3600,) * 2)  # changed "now" for the later clock
        mapped = self.unmarked("ila_wlf_convert_mapped", ("a.vcd", "b.i64"))
        data = np.memmap(mapped / "b.i64", dtype=np.uint8, mode="r")
        self.addCleanup(lambda: data._mmap.close())
        report = self.storage(age_offset=25 * 3600).sweep()
        self.assertIn("ila_vcd_old", report.removed)
        self.assertFalse(old.exists())
        self.assertEqual(report.kept["ila_wave_recent"], "recent, unmarked")
        self.assertTrue(recent.is_dir())
        if os.name == "nt":  # a mapped file blocks the rename, so nothing in the folder is deleted
            self.assertEqual(report.kept["ila_wlf_convert_mapped"], "in use")
            self.assertEqual(sorted(p.name for p in mapped.iterdir()), ["a.vcd", "b.i64"])

    def test_other_names_and_links_are_untouched(self):
        other = self.unmarked("tmp_other")
        bench = self.unmarked("ila_bench")  # the kept benchmark data never matches a prefix
        (self.root / "ila_vcd_file").write_bytes(b"not a folder")
        link = self.link("ila_vcd_link", self.outside)
        inner = self.unmarked("ila_wave_with_link")
        self.link(inner / "escape", self.outside)
        report = self.storage(age_offset=25 * 3600).sweep()
        self.assertEqual(report.removed, ["ila_wave_with_link"])
        self.assertEqual(report.kept, {"ila_vcd_file": "not a folder", "ila_vcd_link": "not a folder"})
        self.assertTrue(other.is_dir() and bench.is_dir() and link.exists())
        self.assertEqual((self.outside / "keep.txt").read_text(encoding="ascii"), "keep")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["ila_bench", "ila_vcd_file", "ila_vcd_link", "tmp_other"])

    def test_a_permission_error_never_stops_the_sweep(self):
        from ila_viewer import tempstore
        self.unmarked("ila_vcd_a")
        self.unmarked("ila_vcd_b")

        def denied(path):
            raise PermissionError(13, "Access is denied", str(path))

        with patch.object(tempstore, "_remove_tree", denied):
            report = self.storage(age_offset=25 * 3600).sweep()
        self.assertEqual(report.removed, [])
        self.assertTrue(all(reason.startswith("partly removed") for reason in report.kept.values()))
        with patch.object(tempstore.TempStorage, "_newest", staticmethod(denied)):
            report = self.storage(age_offset=25 * 3600).sweep()
        self.assertEqual(len(report.errors), 2)
        with patch.object(tempstore.os, "scandir", denied):
            self.assertEqual(list(self.storage().sweep().errors), [str(self.root)])
        # The renamed folders keep their prefix and go on the next sweep.
        self.assertEqual(len(self.storage(age_offset=25 * 3600).sweep().removed), 2)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_and_cancelled_loads_leave_no_folder(self):
        from ila_viewer.data import LoadCancelled
        from ila_viewer.diskspace import DiskSpaceError
        from ila_viewer.sources import WlfCaptureSource
        from scripts.benchmark_wlf import SyntheticVcd
        storage = self.storage()
        vcd_path = Path(self.temp.name) / "capture.vcd"
        SyntheticVcd(3000).write(vcd_path)
        broken = Path(self.temp.name) / "broken.vcd"
        broken.write_text("$timescale 1ns $end\n$var wire 1 ! a $end\n$enddefinitions $end\n#5\n#3\n1!\n",
                          encoding="ascii")
        csv_path = Path(self.temp.name) / "capture.csv"
        csv_path.write_text("Sample in Buffer,a\nRadix - UNSIGNED,BINARY\n" +
                            "".join(f"{i},{i % 2}\n" for i in range(5000)), encoding="ascii")
        bad_csv = Path(self.temp.name) / "bad.csv"
        bad_csv.write_text("Sample in Buffer,a\nRadix - UNSIGNED,BINARY\n0,1\n1,7\n", encoding="ascii")

        class Full:
            def free(self, folder):
                return 0

        class Converter:
            def __init__(self, fail):
                self.fail = fail

            def convert(self, source, target, cancel):
                Path(target).write_bytes(vcd_path.read_bytes())
                if self.fail:
                    raise RuntimeError("wlf2vcd завершился с кодом 3.")

        calls = {"n": 0}

        def cancel_later():
            calls["n"] += 1
            return calls["n"] > 2

        wlf = Path(self.temp.name) / "capture.wlf"
        wlf.write_bytes(b"wlf")
        cases = {
            "vcd cancelled": lambda: VcdCaptureReader(temp=storage).load(vcd_path, cancel=cancel_later),
            "vcd malformed": lambda: VcdCaptureReader(temp=storage).load(broken),
            "vcd full disk": lambda: VcdCaptureReader(disk=Full(), temp=storage).load(vcd_path),
            "csv cancelled": lambda: load_csv(csv_path, cancel=cancel_later, temp=storage),
            "csv malformed": lambda: load_csv(bad_csv, temp=storage),
            "wlf failed": lambda: WlfCaptureSource(Converter(True), temp=storage).load(wlf),
            "wlf cancelled": lambda: WlfCaptureSource(Converter(False), temp=storage).load(wlf, cancel=cancel_later),
        }
        for name, load in cases.items():
            with self.subTest(name):
                calls["n"] = 0
                with self.assertRaises((LoadCancelled, ValueError, DiskSpaceError, RuntimeError)):
                    load()
                self.assertEqual(list(self.root.iterdir()), [])
        capture = load_csv(csv_path, temp=storage)  # a good load keeps its folder until close
        self.assertEqual(len(list(self.root.iterdir())), 1)
        capture.close()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_the_window_sweeps_in_the_background_and_stops_on_close(self):
        from PySide6.QtWidgets import QApplication
        from ila_viewer.cpu import CpuTopology
        from ila_viewer.window import MainWindow
        QApplication.instance() or QApplication([])
        self.unmarked("ila_vcd_old")
        window = MainWindow(preferences_store=PreferencesStore(), cpu=CpuTopology(2, 2))
        self.assertIsNone(window.temp_sweep)  # a window alone never sweeps the real folder
        task = window.start_temp_sweep(self.storage(age_offset=25 * 3600))
        self.assertTrue(task.wait(10000))
        self.assertEqual(task.result.removed, ["ila_vcd_old"])
        self.assertIsNone(task.error)
        window.close()
        self.assertFalse(task.isRunning())


if __name__ == "__main__":
    unittest.main()
