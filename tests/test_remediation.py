"""Regressions for the review of 8dbe201 (HANDOFF.md items 1-6)."""
import json
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
from ila_viewer.spi import (DATA, FIELD_DTYPE, PARTIAL, RESET_FRAME, UNKNOWN, UNKNOWN_COMMAND, BusWidths,
                            CommandSpec, FixedFormat, SpiDecoder, SpiDecoderConfig, SpiExportRequest,
                            SpiExportService, SpiSettings, config_to_dict, default_commands, guess_pins,
                            pins_from_dict, settings_from_dict)
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


if __name__ == "__main__":
    unittest.main()
