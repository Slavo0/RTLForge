"""Regressions for the review of 8dbe201 (HANDOFF.md items 1-6)."""
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from types import SimpleNamespace

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
from ila_viewer.spi import DATA, FIELD_DTYPE, UNKNOWN, SpiExportRequest, SpiExportService
from ila_viewer.vcd import VcdCaptureReader


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


if __name__ == "__main__":
    unittest.main()
