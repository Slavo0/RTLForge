"""Independent regressions for the November 2026 correctness review."""
import io
import tempfile
import unittest
import threading
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from ila_viewer.advanced import AdvancedHighlightService
from ila_viewer.conditions import ConditionCompiler
from ila_viewer.csv_chunks import iter_blocks
from ila_viewer.data import LoadCancelled, load_csv
from ila_viewer.highlights import HighlightGroup, HighlightService
from ila_viewer.sampling import EdgeSampling
from ila_viewer.vcd import VcdCaptureReader


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def capture(self, text, suffix="csv"):
        path = Path(self.temp.name) / f"capture.{suffix}"
        path.write_text(text, encoding="utf-8")
        cap = load_csv(path) if suffix == "csv" else VcdCaptureReader().load(path)
        self.addCleanup(cap.close)
        return cap

    def test_highlights_use_ordered_half_open_boundaries(self):
        cap = self.capture("clk\nRadix - HEX\n0\n1\n0\n1\n0\n1\n")
        pattern = ConditionCompiler(cap.signals).compile_pattern("clk -> !clk")
        for a, b in ((1, 4), (4, 1)):
            for service, condition, grid, expected in (
                (HighlightService(), None, 1, [(1, 4)]),
                (AdvancedHighlightService(), pattern, 1, [(1, 3)]),
                (HighlightService(), None, EdgeSampling(cap.signals[0]), [(1, 3)]),
            ):
                result = service.build(cap, condition, a, b, grid)
                try:
                    self.assertEqual(list(result.visible(0, 6)), expected)
                finally:
                    result.close()
        result = HighlightService().build(cap, None, 2, 2, 1)
        self.assertEqual(result.count, 0)
        result.close()

    def test_explicit_full_width_selection_is_unsigned_scalar_and_vector(self):
        cap = self.capture("s[7:0]\nRadix - SIGNED\n-1\n0\n")
        compiler = ConditionCompiler(cap.signals, {"s[7:0]": "SIGNED"})
        for selection in ("7:0", "7,6,5,4,3,2,1,0"):
            for comparison, expected in (("< d0", [False, False]), ("== d255", [True, False])):
                condition = compiler.compile(f"`s[7:0]`[{selection}] {comparison}")
                self.assertEqual([condition.matches(i) for i in range(2)], expected)
                self.assertEqual(condition.evaluate(np.arange(2), np.array([-1, 0])).tolist(), expected)

    def test_vcd_body_is_independent_of_line_layout(self):
        header = "$var wire 1 ! clk $end $var wire 4 ? data $end $enddefinitions $end\n"
        for body in ("$dumpvars 0! b0011\t? $end\n#5 1! b0101 ?\n",
                     "#0\t0!\tb0011\n?\n#5\n1!\nb0101\t?\n",
                     "$dumpvars\n0! b0011 ?\n$end #5 $comment 0! #99 $end 1! b0101 ?"):
            cap = self.capture(header + body, "vcd")
            self.assertEqual(cap.times.tolist(), [0, 5])
            self.assertEqual([[s.value_at(i) for i in range(2)] for s in cap.signals], [[0, 1], [3, 5]])

    def test_multiline_csv_record_survives_every_block_boundary(self):
        data = b'0," \nF"\n1,"A"\n'
        from ila_viewer.csv_chunks import split_block
        for size in range(1, len(data) + 1):
            values, lines = [], 0
            for block, count in iter_blocks(io.BytesIO(data), 0, None, size):
                cells, numbers = split_block(block, 2, lines)
                values.extend(int(cell.strip(), 16) for cell in cells[1].tolist())
                lines += count
            self.assertEqual(values, [15, 10], size)
            self.assertEqual(lines, 3)

    def test_parallel_quoted_fallback_preserves_multiline_records(self):
        from ila_viewer.cpu import WorkerPool
        from ila_viewer import data
        path = Path(self.temp.name) / "quoted.csv"
        path.write_text('Sample in Buffer,q[3:0]\nRadix - UNSIGNED,HEX\n0," \nF"\n1,"A"\n2,1\n', encoding="utf-8")
        pool = WorkerPool(2)
        self.addCleanup(pool.shutdown)
        real = iter_blocks
        def small(stream, start, end, **kwargs):
            return real(stream, start, end, block_bytes=5, **kwargs)
        with patch.object(data, "MIN_PARALLEL_CHUNK", 1), patch.object(data, "iter_blocks", side_effect=small):
            cap = load_csv(path, pool=pool)
        self.addCleanup(cap.close)
        self.assertEqual([cap.signals[0].value_at(i) for i in range(cap.count)], [15, 10, 1])
        self.assertFalse(list(Path(cap.storage.name).glob("part*")))

    def test_vcd_token_carry_and_cancellation_are_read_bounded(self):
        text = "$dumpvars 0! b101010 ? $end #999 1!"
        for size in range(1, len(text)):
            self.assertEqual(list(VcdCaptureReader._tokens(io.StringIO(text), block_chars=size)), text.split())
        tokens = VcdCaptureReader._tokens(io.StringIO("x" * 100000), cancel=lambda: True)
        with self.assertRaises(LoadCancelled):
            next(tokens)

    def test_large_simulator_timestamps_keep_tick_precision(self):
        origin = 2**53
        cap = self.capture("$var wire 1 ! clk $end $enddefinitions $end\n" +
                           "".join(f"#{origin + i}\n{i % 2}!\n" for i in range(3)), "vcd")
        self.assertEqual([cap.time_at(i) for i in range(3)], [origin, origin + 1, origin + 2])
        self.assertEqual([cap.sample_at_time(origin + i) for i in range(3)], [0, 1, 2])
        np.testing.assert_equal(cap.samples_at_times(np.array([origin, origin + 1, origin + 2], dtype=np.int64)), [0, 1, 2])
        from ila_viewer.view import WaveformView
        from ila_viewer.rendering import WaveformRenderer
        from ila_viewer.timebase import capture_axis
        view = SimpleNamespace(capture=cap, left=0.25, span=1.5, plot_width=600, plot_left=100)
        self.assertAlmostEqual(WaveformView.sample_x(view, 1), 400)
        self.assertAlmostEqual(WaveformView.x_sample(view, 400), 1)
        edges = WaveformRenderer._time_edges(view)
        self.assertTrue(np.all(np.diff(edges) > 0))
        self.assertEqual((edges[0], edges[-1]), (0.25, 1.75))
        axis = capture_axis(cap)
        self.assertEqual(axis.delta(0, 1), "1 ns")
        self.assertNotEqual(axis.readout(0), axis.readout(1))
        self.assertEqual([sample for sample, _ in axis.ruler(0, 2, 300)], [0, 1, 2])
        # A long idle gap must not destroy tick precision when zoomed into its far side.
        cap.times = np.array([0, origin, origin + 1, origin + 2], dtype=np.int64)
        cap.count = 4
        np.testing.assert_equal(cap.samples_at_times(cap.times), [0, 1, 2, 3])
        view.left = 1.25
        self.assertAlmostEqual(WaveformView.sample_x(view, 2), 400)
        self.assertAlmostEqual(WaveformView.x_sample(view, 400), 2)
        self.assertTrue(np.all(np.diff(WaveformRenderer._time_edges(view)) > 0))

    def test_single_cell_exceeding_gather_scratch_budget(self):
        with patch("ila_viewer.csv_chunks.GATHER_BUDGET", 64):
            capture = self.capture("s\nRadix - HEX\n0000000F\n")
        signal = capture.signals[0]
        self.assertEqual(signal.values, [15])
        np.testing.assert_array_equal(signal.starts, [0])
        np.testing.assert_array_equal(signal.codes, [0])

    def test_skewed_csv_cells_have_bounded_allocations(self):
        from ila_viewer.csv_chunks import split_block
        block = b"0,1\n" * 100000 + b"1," + b"0" * 1023 + b"F\n"
        tracemalloc.start()
        try:
            columns, _ = split_block(block, 2)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 32 << 20)  # old gather index alone exceeded 800 MB
        self.assertEqual(columns[1][-1], b"0" * 1023 + b"F")

    def test_csv_merge_is_chunked_and_cancellable(self):
        from ila_viewer.csv_chunks import place_range
        folder = Path(self.temp.name)
        source = np.arange(17, dtype=np.int64)
        source.tofile(folder / "part0_0.starts")
        (source % 2).astype(np.uint32).tofile(folder / "part0_0.codes")
        (folder / "0.starts").write_bytes(bytes(16 * 8))
        (folder / "0.codes").write_bytes(bytes(16 * 4))
        jobs = [(0, np.array([10, 20], dtype=np.uint32), 100, 1, 0)]
        real = np.fromfile
        sizes = []
        def bounded(*args, **kwargs):
            sizes.append(kwargs.get("count", -1))
            return real(*args, **kwargs)
        with patch("ila_viewer.csv_chunks.np.fromfile", side_effect=bounded):
            place_range(folder, "part0", jobs, chunk_runs=3)
        self.assertTrue(all(0 < size <= 3 for size in sizes))
        np.testing.assert_equal(real(folder / "0.starts", dtype=np.int64), source[1:] + 100)
        np.testing.assert_equal(real(folder / "0.codes", dtype=np.uint32), np.where(source[1:] % 2, 20, 10))
        flag = folder / "cancel.flag"
        flag.touch()
        source.tofile(folder / "part0_0.starts")
        (source % 2).astype(np.uint32).tofile(folder / "part0_0.codes")
        with self.assertRaises(LoadCancelled):
            place_range(folder, "part0", jobs, flag, chunk_runs=3)

    def test_csv_value_table_merge_cancels_between_batches(self):
        from ila_viewer.csv_chunks import ValueTable
        for exported in (("ints", np.arange(10, dtype=np.int64)), ("list", list(range(10))),
                         ("mixed", np.arange(9), np.arange(9), [(9, "X")])):
            table = ValueTable()
            calls = 0
            def cancel():
                nonlocal calls
                calls += 1
                return calls > 1
            with self.assertRaises(LoadCancelled):
                table.lut_for(exported, cancel, chunk_values=3)

    def test_spi_frame_discovery_streams_across_blocks_and_cancels(self):
        from ila_viewer.spi import SpiDecoder, SpiPins, LineRef
        cap = self.capture("cs\nRadix - HEX\n0\n1\n0\n0\n1\n0\n1\n0\n")
        decoder = SpiDecoder()
        decoder.BLOCK = 2
        pins = SpiPins(cs=LineRef("cs"), sck=LineRef("cs"))
        self.assertEqual(list(decoder._frames(cap, pins)), [(0, 1), (2, 4), (5, 6), (7, 8)])
        calls = 0
        def cancel():
            nonlocal calls
            calls += 1
            return calls > 2
        frames = decoder._frames(cap, pins, cancel)
        self.assertEqual(next(frames), (0, 1))
        with self.assertRaises(LoadCancelled):
            list(frames)

    def test_unknown_edge_scan_is_chunked_and_cancellable(self):
        from ila_viewer.measurements import EdgeMeasurementService
        cap = self.capture("clk\nRadix - BINARY\n0\n1\nX\n0\n1\n0\n1\n")
        service = EdgeMeasurementService()
        service.CHUNK = 2
        result = service.measure(cap.signals[0], 0, 7)
        self.assertEqual((result.rising, result.falling, result.first_rise, result.last_rise), (3, 1, 1, 6))
        with self.assertRaises(LoadCancelled):
            service.measure(cap.signals[0], 0, 7, cancel=lambda: True)

    def test_background_measurements_reject_stale_results_and_join_before_cleanup(self):
        from PySide6.QtWidgets import QApplication
        from ila_viewer.measurements import EdgeMeasurementService
        from ila_viewer.measurements_ui import EdgeMeasurementController
        app = QApplication.instance() or QApplication([])
        cap = self.capture("clk\nRadix - BINARY\n0\n1\nX\n0\n1\n0\n1\n")
        entered, release = threading.Event(), threading.Event()
        service = EdgeMeasurementService()
        original = service.measure
        def slow(signal, start, end, cancel=lambda: False):
            entered.set()
            release.wait(2)
            return original(signal, start, end)  # simulate successful stale completion
        service.measure = slow
        controller = EdgeMeasurementController(service)
        changes = []
        controller.changed.connect(lambda: changes.append(True))
        try:
            self.assertIsNone(controller.request(cap.signals[0], 0, 7))
            controller.timer.stop()
            controller._launch()
            self.assertTrue(entered.wait(2))
            self.assertIsNone(controller.request(cap.signals[0], 3, 5))
            release.set()
            deadline = time.monotonic() + 3
            while not changes and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)
            self.assertEqual(len(changes), 1)
            result = controller.request(cap.signals[0], 3, 5)
            self.assertEqual((result.start, result.end, result.rising), (3, 5, 1))
            # Returning to a cached range also invalidates a different in-flight range.
            entered.clear()
            release.clear()
            controller.request(cap.signals[0], 0, 7)
            controller.timer.stop()
            controller._launch()
            self.assertTrue(entered.wait(2))
            self.assertIs(controller.request(cap.signals[0], 3, 5), result)
            release.set()
            deadline = time.monotonic() + 3
            while controller.worker and time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.005)
            self.assertEqual(len(changes), 1)
            self.assertEqual(controller.key, (id(cap.signals[0]), 3, 5))
        finally:
            release.set()
            controller.clear()
        self.assertIsNone(controller.worker)
        self.assertIsNone(controller.request(cap.signals[0], 0, 7))
        self.assertIsNone(controller.pending)
        cap.close()
        app.processEvents()

    def test_window_close_cannot_restart_measurement_from_status_signals(self):
        from PySide6.QtWidgets import QApplication
        from PySide6.QtTest import QTest
        from ila_viewer.window import MainWindow
        from ila_viewer.preferences import PreferencesStore
        from ila_viewer.cpu import CpuTopology
        app = QApplication.instance() or QApplication([])
        cap = self.capture("clk\nRadix - BINARY\n0\n1\nX\n0\n1\n")
        window = MainWindow(preferences_store=PreferencesStore(), cpu=CpuTopology(2, 2))
        try:
            window.loaded(cap)
            window.view.set_cursor(0)
            window.view.set_cursor(4, True)
            self.assertIsNotNone(window.edge_measurements.pending)
            window.close()
            # highlights.reset() emits changed while closeEvent still owns the capture.
            app.processEvents()
            QTest.qWait(100)
            self.assertIsNone(window.edge_measurements.worker)
            self.assertIsNone(window.edge_measurements.pending)
            self.assertIsNone(window.capture)
        finally:
            window.close()

    def test_close_disposes_completed_restored_groups_before_capture(self):
        from ila_viewer.window import MainWindow
        cap = self.capture("clk\nRadix - HEX\n0\n1\n")
        intervals = HighlightService().build(cap, None, 0, 2, 1)
        group = HighlightGroup("restored", "clk", "#ffffff", intervals, 0, 2, 1, 0)
        path = intervals.path
        widget = SimpleNamespace(setEnabled=lambda _: None, hide=lambda: None)
        closed = []
        def close():
            self.assertIsNone(intervals.path)
            cap.close()
            closed.append(True)
        window = SimpleNamespace(worker=None, closing=True, capture=cap, open_action=widget,
                                 refresh_action=widget, previous_button=widget, next_button=widget,
                                 progress=widget, cancel_button=widget, close=close)
        task = SimpleNamespace(result=[group], deleteLater=lambda: None)
        try:
            MainWindow.finish_work(window, task, lambda _: self.fail("Closing must not transfer ownership"))
            self.assertTrue(closed)
            self.assertFalse(path.exists())
        finally:
            intervals.close()
