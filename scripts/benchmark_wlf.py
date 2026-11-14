# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Load and repaint benchmark for long simulator captures (VCD/WLF); JSON on stdout.

Without ``--file`` it generates a synthetic VCD in the textual layout of Questa
``wlf2vcd`` that imitates the workload of a large WLF: many timestamps, several
copies of a clock toggling at every timestamp, buses declared bit by bit and some
X/Z values. ``--file`` measures a real ``.vcd`` or ``.wlf`` instead.

    python -m scripts.benchmark_wlf
    python -m scripts.benchmark_wlf --timestamps 500000 --profile repaint --zoom whole --size 1920x1080
    python -m scripts.benchmark_wlf --file vsim.wlf
    python -m scripts.benchmark_wlf --file converted.vcd --skip-repaint --workers 0 --disk
    python -m scripts.benchmark_wlf --verify-sliced vsim.wlf
    python -m scripts.benchmark_wlf --check-scaling

``--check-scaling`` loads synthetic captures of two lengths and fails (exit status 1)
when the whole-capture FHD repaint of the longer one is more than ``--max-ratio`` times
slower: repaint is bounded by the viewport, so it must not grow with capture length.

``--verify-sliced`` loads a WLF twice through ``WlfCaptureSource``, converted in time
slices (with its seam check) and in one ``wlf2vcd`` run, and prints
``capture_differences`` of the two captures.
"""
import argparse
import contextlib
import cProfile
import ctypes
import io
import json
import os
import pstats
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

SIZES = {"800x500": (800, 500), "1920x1080": (1920, 1080)}
ZOOMS = ("whole", "1%", "2000", "100")


# --------------------------------------------------------------------------- generator

def _identifier(index):
    """Printable VCD identifier, first character fastest, as Questa writes them."""
    digits = []
    while True:
        digits.append(chr(33 + index % 94))
        index //= 94
        if not index:
            return "".join(digits)
        index -= 1


class SyntheticVcd:
    """A deterministic VCD whose layout and per-timestamp workload follow the large vsim.wlf.

    Layout as written by Questa 2024.1 ``wlf2vcd``: CRLF line endings, one change per
    line, ``#t`` on its own line, ``wire 1`` changes in scalar form (``0>``) and every
    ``reg``/``integer`` change in vector form with leading zeros dropped (``b0 !``,
    ``b11111110 #``), even for 1-bit regs. Three clock copies are ``integer 32``, two are
    ``wire 1`` and two are ``reg 1``; buses declared bit by bit are ``wire 1``.
    """

    SPLIT_BUSES = (("rx_data", 9), ("tx_data", 9), ("ram_r_addr", 15), ("ram_r_data", 16),
                   ("ram_w_data", 16), ("status", 40), ("ctrl_bits", 8))   # 7 buses, 113 bits, as in the real file
    CLOCKS = (("tb_top", "integer", 32), ("tb_top/intf_fifo", "integer", 32), ("tb_top/intf_ram", "integer", 32),
              ("tb_top/dut", "wire", 1), ("tb_top/dut/crc8", "wire", 1),
              ("tb_top/skid_buffer_rx", "reg", 1), ("tb_top/skid_buffer_tx", "reg", 1))
    # Widths of the remaining reg vectors in the real file (41 of them).
    VECTOR_WIDTHS = (2, 2, 4, 4, 5, 5, 5, *[8] * 7, *[9] * 13, 11, 11, 11, 15, 15, 15, *[16] * 5, 40, 48, 48)
    WIRE_SCALARS = 8
    NOISY = 9                     # 1-bit regs changing at about every other timestamp
    VALUES_PER_VECTOR = 64
    UNKNOWN = 0.0002              # probability that a change writes X (and half that for Z / mixed X)

    def __init__(self, timestamps=2_000_000, timestep=10, signals=237, one_bit=191, seed=2026):
        self.timestamps = timestamps
        self.timestep = timestep
        self.seed = seed
        split_bits = sum(width for _, width in self.SPLIT_BUSES)
        one_bit_clocks = sum(width == 1 for *_, width in self.CLOCKS)
        regs = one_bit - one_bit_clocks - split_bits - self.WIRE_SCALARS
        vectors = signals - one_bit - (len(self.CLOCKS) - one_bit_clocks)
        if regs < self.NOISY or vectors < 4:
            raise ValueError("too few signals for the fixed clocks and split buses")
        # (scope, name, var type, width, change probability per timestamp, role)
        decl = []
        clocks = [(scope, "clk", kind, width, 1.0, "clock") for scope, kind, width in self.CLOCKS]
        # The first screen shows busy rows, as in the real capture: clocks, noisy regs and a counter.
        decl.extend(clocks[:3])
        decl.append(("tb_top/intf_fifo", "rx_data [8:0]", "reg", 9, 0.05, "vector"))
        decl.extend(("tb_top/intf_fifo", f"ready_{index}", "reg", 1, 0.5, "scalar") for index in range(self.NOISY))
        decl.append(("tb_top/dut", "timeout_ctr_ff [15:0]", "reg", 16, 0.5, "vector"))
        decl.extend(clocks[3:])
        for name, width in self.SPLIT_BUSES:
            decl.extend(("tb_top/dut", f"{name} [{bit}]", "wire", 1, 0.05, "scalar")
                        for bit in range(width - 1, -1, -1))
        decl.extend(("tb_top/dut", f"wire_{index}", "wire", 1, 0.04, "scalar") for index in range(self.WIRE_SCALARS))
        decl.extend(("tb_top/ctrl", f"flag_{index}", "reg", 1, 0.04, "scalar") for index in range(regs - self.NOISY))
        decl.extend(("tb_top/ctrl", f"count_{index}", "integer", 32, 0.05, "vector") for index in range(2))
        for index in range(vectors - 4):  # rx_data and timeout_ctr_ff are two of them
            width = self.VECTOR_WIDTHS[index % len(self.VECTOR_WIDTHS)]
            decl.append(("tb_top/ctrl", f"reg_{index} [{width - 1}:0]", "reg", width, 0.05, "vector"))
        self.decl = decl

    @property
    def signal_count(self):
        return len(self.decl)

    def write(self, path, block=16384):
        """Write the file; returns the number of change lines written."""
        rng = np.random.default_rng(self.seed)
        ids = [_identifier(index) for index in range(len(self.decl))]
        # One flat table of every line a signal can print; per signal an offset into it.
        # Single-bit entries: 0, 1, x, z. Vector entries: 64 values, then all-X, then mixed X.
        table, offsets, initial = [], [], []
        for index, (_, _, kind, width, _, role) in enumerate(self.decl):
            offsets.append(len(table))
            ident = ids[index]
            if kind == "wire" and width == 1:
                table.extend(f"{value}{ident}\r\n".encode() for value in "01xz")
            elif width == 1 or role == "clock":
                table.extend(f"b{value} {ident}\r\n".encode() for value in "01xz")
            else:
                values = rng.integers(0, 1 << min(width, 62), self.VALUES_PER_VECTOR)
                table.extend(f"b{int(value):b} {ident}\r\n".encode() for value in values)
                table.append(f"bx {ident}\r\n".encode())
                mixed = f"bx{'0' * (width - 2)}x" if width > 2 else "bz"
                table.append(f"{mixed} {ident}\r\n".encode())
            if role == "clock":
                initial.append(table[offsets[-1]])
            elif width == 1:
                initial.append(table[offsets[-1] + (2 if index % 3 == 0 else 0)])
            else:
                initial.append(table[offsets[-1] + self.VALUES_PER_VECTOR])
        table = np.array(table, dtype=object)
        offsets = np.array(offsets, dtype=np.int64)
        probability = np.array([item[4] for item in self.decl])
        scalar = np.array([item[3] == 1 or item[5] == "clock" for item in self.decl])
        clock = np.array([item[5] == "clock" for item in self.decl])
        parity = np.zeros(len(self.decl), dtype=np.int64)
        changes = 0
        with open(path, "wb") as stream:
            stream.write(self._header(ids).encode())
            stream.write(b"#0\r\n" + b"".join(initial))
            changes += len(initial)
            row = 1
            while row < self.timestamps:
                rows = min(block, self.timestamps - row)
                mask = rng.random((rows, len(self.decl))) < probability
                mask[:, clock] = True
                toggles = (np.cumsum(mask, axis=0) + parity) % 2
                parity = toggles[-1].copy()
                choice = np.where(scalar, toggles, rng.integers(0, self.VALUES_PER_VECTOR, (rows, len(self.decl))))
                unknown = rng.random((rows, len(self.decl)))
                rare = self.UNKNOWN
                choice = np.where(~clock & (unknown < rare), np.where(scalar, 2, self.VALUES_PER_VECTOR), choice)
                choice = np.where(~clock & (unknown >= rare) & (unknown < rare * 1.5),
                                  np.where(scalar, 3, self.VALUES_PER_VECTOR + 1), choice)
                where_rows, where_cols = np.nonzero(mask)
                lines = table[offsets[where_cols] + choice[where_rows, where_cols]]
                counts = mask.sum(axis=1)
                stamp_positions = np.arange(rows) + np.concatenate(([0], np.cumsum(counts)[:-1]))
                out = np.empty(rows + len(lines), dtype=object)
                is_stamp = np.zeros(len(out), dtype=bool)
                is_stamp[stamp_positions] = True
                out[is_stamp] = [b"#%d\r\n" % ((row + offset) * self.timestep) for offset in range(rows)]
                out[~is_stamp] = lines
                stream.write(b"".join(out.tolist()))
                changes += len(lines)
                row += rows
        return changes

    def _header(self, ids):
        lines = ["$date\n\tFri Nov 13 00:00:00 2026\n$end\n",
                 "$version\n\tQuestaSim Version 2024.1\n$end\n",
                 "$timescale\n\t1ns\n$end\n"]
        current = []
        for index, (scope, name, kind, width, _, _) in enumerate(self.decl):
            path = scope.split("/")
            common = 0
            while common < min(len(path), len(current)) and path[common] == current[common]:
                common += 1
            lines.extend("$upscope $end\n" for _ in current[common:])
            lines.extend(f"$scope module {part} $end\n" for part in path[common:])
            current = path
            lines.append(f"$var {kind} {width} {ids[index]} {name} $end\n")
        lines.extend("$upscope $end\n" for _ in current)
        lines.append("$enddefinitions $end\n")
        return "".join(lines).replace("\n", "\r\n")


# --------------------------------------------------------------------------- measurements

def peak_memory_mb():
    """Peak resident memory of this process (Windows working set or ru_maxrss)."""
    if os.name == "nt":
        class Counters(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL("kernel32")
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.K32GetProcessMemoryInfo.argtypes = (ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong)
        if not kernel.K32GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return None
        return round(counters.PeakWorkingSetSize / 2**20, 1)
    import resource
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


class Profiler:
    """Optional cProfile around one phase; prints the top 25 functions by own time to stderr."""

    def __init__(self, enabled):
        self.profile = cProfile.Profile() if enabled else None

    def __enter__(self):
        if self.profile:
            self.profile.enable()
        return self

    def __exit__(self, *exc):
        if self.profile:
            self.profile.disable()

    def report(self, title):
        if not self.profile:
            return
        text = io.StringIO()
        pstats.Stats(self.profile, stream=text).sort_stats("tottime").print_stats(25)
        print(f"===== profile: {title} =====\n{text.getvalue()}", file=sys.stderr)


def find_wlf2vcd(explicit=None):
    if explicit:
        return explicit
    if os.environ.get("WLF2VCD"):
        return os.environ["WLF2VCD"]
    found = shutil.which("wlf2vcd")
    if found:
        return found
    from ila_viewer.preferences import PreferencesStore
    return PreferencesStore.for_application().load().wlf2vcd_path or None


class DiskSampler:
    """Peak total size of the loader's temporary capture folders (``ila_vcd_*`` in %TEMP%)."""

    def __init__(self, interval=0.2):
        self.interval = interval
        self.peak = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def size(self):
        total = 0
        with os.scandir(tempfile.gettempdir()) as entries:
            for entry in entries:
                if entry.name.startswith("ila_vcd_") and entry.is_dir():
                    try:
                        with os.scandir(entry.path) as files:
                            total += sum(f.stat().st_size for f in files if f.is_file())
                    except OSError:
                        pass
        return total

    def run(self):
        while not self.stop.is_set():
            self.peak = max(self.peak, self.size())
            self.stop.wait(self.interval)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()


def load_capture(path, profiler, converter_path=None, workers=1, disk=False):
    """Load a .vcd/.wlf; returns (capture, phases) with the conversion timed separately.

    ``workers`` above 1 parses on a WorkerPool of that size (0: physical cores - 1, the
    application default); 1 parses on this process.
    """
    from ila_viewer.cpu import WorkerPool, available_cpus, detect
    from ila_viewer.sources import QuestaWlfConverter
    from ila_viewer.vcd import VcdCaptureReader
    if not workers:
        workers = detect().recommended_workers(available_cpus())
    pool = WorkerPool(workers) if workers > 1 else None
    path = Path(path).resolve()
    phases = {"file_mb": round(path.stat().st_size / 2**20, 1)}
    folder = None
    try:
        vcd = path
        if path.suffix.lower() == ".wlf":
            folder = tempfile.TemporaryDirectory(prefix="ila_benchmark_wlf_")
            vcd = Path(folder.name) / "converted.vcd"
            start = time.perf_counter()
            check = QuestaWlfConverter(find_wlf2vcd(converter_path), slices=workers).convert(path, vcd)
            phases["wlf2vcd_seconds"] = round(time.perf_counter() - start, 2)
            phases["sliced"] = check is not None
        size = vcd.stat().st_size
        phases["vcd_mb"] = round(size / 2**20, 1)
        phases["workers"] = workers
        sampler = DiskSampler() if disk else contextlib.nullcontext()
        start = time.perf_counter()
        reader = VcdCaptureReader(pool)
        with profiler, sampler:
            capture = reader.load(vcd, source_path=path)
        seconds = time.perf_counter() - start
        if reader.timings:
            phases["phase_seconds"] = reader.timings
        if path.suffix.lower() == ".wlf" and check is not None:
            # The check WlfCaptureSource runs on every sliced load; timed apart from the parse.
            start = time.perf_counter()
            mismatches = check.mismatches(capture)
            phases.update(seam_check_seconds=round(time.perf_counter() - start, 4), seams=len(check.seams),
                          seam_mismatches=[m.describe() for m in mismatches[:10]])
        if disk:
            phases["temp_peak_mb"] = round(sampler.peak / 2**20, 1)
            phases["temp_after_mb"] = round(sampler.size() / 2**20, 1)
        phases.update(parse_seconds=round(seconds, 2),
                      timestamps_per_second=round(capture.count / seconds),
                      mb_per_second=round(size / 2**20 / seconds, 1),
                      timestamps=capture.count, signals=len(capture.signals),
                      changes=int(sum(len(signal.starts) for signal in capture.signals)),
                      peak_memory_mb=peak_memory_mb())
        if "wlf2vcd_seconds" in phases:
            phases["total_seconds"] = round(phases["wlf2vcd_seconds"] + seconds + phases.get("seam_check_seconds", 0), 2)
        return capture, phases
    finally:
        if pool is not None:
            pool.shutdown()
        if folder is not None:
            folder.cleanup()


def verify_sliced(path, converter_path=None, workers=0):
    """Load ``path`` sliced and in one wlf2vcd run through WlfCaptureSource; compare the captures."""
    from ila_viewer.cpu import WorkerPool, available_cpus, detect
    from ila_viewer.sources import QuestaWlfConverter, WlfCaptureSource
    from tests.vcd_compare import capture_differences
    if not workers:
        workers = detect().recommended_workers(available_cpus())
    executable = find_wlf2vcd(converter_path)
    pool = WorkerPool(workers) if workers > 1 else None
    report = {"source": str(Path(path).resolve()), "workers": workers}
    captures = []
    try:
        for name, slices in (("sliced", workers), ("single_run", 1)):
            source = WlfCaptureSource(QuestaWlfConverter(executable, slices=slices), pool=pool)
            start = time.perf_counter()
            capture = source.load(path)
            captures.append(capture)
            report[name] = {"seconds": round(time.perf_counter() - start, 2), "timestamps": capture.count,
                            "signals": len(capture.signals), "notices": capture.notices, **source.timings}
        differences = capture_differences(*captures)
        report["capture_differences"] = differences[:50]
        report["difference_count"] = len(differences)
    finally:
        for capture in captures:
            capture.close()
        if pool is not None:
            pool.shutdown()
    return report


class RepaintBench:
    """A real MainWindow under the offscreen platform with the capture installed."""

    def __init__(self, capture):
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import QApplication
        from ila_viewer.cpu import CpuTopology
        from ila_viewer.preferences import PreferencesStore
        from ila_viewer.themes import STYLE
        from ila_viewer.window import MainWindow
        self.app = QApplication.instance() or QApplication([])
        if os.name == "nt":
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/consola.ttf")
        self.app.setFont(QFont("Segoe UI", 10))
        self.app.setStyleSheet(STYLE)
        self.window = MainWindow(preferences_store=PreferencesStore(), cpu=CpuTopology(2, 2))
        self.window.show()
        self.window.loaded(capture)
        self.app.processEvents()
        self.view = self.window.view
        self.count = capture.count

    def close(self):
        self.window.close()
        self.app.processEvents()

    def resize(self, size):
        self.window.resize(*SIZES[size])
        self.app.processEvents()

    def zoom_range(self, zoom):
        middle = self.count // 2
        if zoom == "whole":
            return 0, self.count
        span = max(2, self.count // 100) if zoom == "1%" else min(self.count, int(zoom))
        return max(0, middle - span // 2), span

    def best(self, action, repeats=5, setup=None):
        times = []
        for _ in range(repeats):
            if setup:
                setup()
                self.app.processEvents()
            start = time.perf_counter()
            action()
            self.view.viewport().repaint()
            times.append(time.perf_counter() - start)
        return round(min(times) * 1000, 1)

    def rows_on_screen(self):
        v = self.view
        # The same row range WaveformRenderer.paint walks (partly visible rows included).
        return min(len(v.rows), (v.viewport().height() - v.HEADER) // v.ROW + 2)

    def table(self, sizes, zooms, profiler):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        v = self.view
        report = []
        for size in sizes:
            self.resize(size)
            row = {"window": size, "viewport": f"{v.viewport().width()}x{v.viewport().height()}",
                   "rows": self.rows_on_screen(), "repaint_ms": {}}
            for zoom in zooms:
                v.set_range(*self.zoom_range(zoom))
                self.app.processEvents()
                with profiler:
                    row["repaint_ms"][zoom] = self.best(lambda: None)
            base = self.zoom_range("1%")
            reset = lambda: v.set_range(*base)
            x = v.plot_left + v.plot_width // 2
            point = QPoint(int(x), v.HEADER + v.ROW // 2)
            row["at_1%_ms"] = {
                "marker_click": self.best(lambda: QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton,
                                                                   Qt.KeyboardModifier.NoModifier, point), setup=reset),
                "zoom_step": self.best(lambda: v.zoom(0.8, v.left + v.span / 2), setup=reset),
                "scroll_step": self.best(lambda: v.set_range(v.left + v.span * 0.12, v.span), setup=reset),
            }
            report.append(row)
        return report


def check_scaling(lengths=(250_000, 4_000_000), max_ratio=3.0, workers=0, size="1920x1080", seed=2026):
    """Whole-capture repaint (best of 5) per capture length; ``passed`` when the ratio holds.

    The float-key ``searchsorted`` regression made this grow with the capture: 223 ms for
    24.4 M timestamps against 4-30 ms for 1 M (docs/WLF_PERF_TASKS.md, task 1).
    """
    rows = []
    with tempfile.TemporaryDirectory(prefix="ila_benchmark_scaling_") as folder:
        for timestamps in lengths:
            path = Path(folder) / f"synthetic_{timestamps}.vcd"
            SyntheticVcd(timestamps, seed=seed).write(path)
            capture, _ = load_capture(path, contextlib.nullcontext(), workers=workers)
            bench = RepaintBench(capture)
            try:
                bench.resize(size)
                bench.view.set_range(0, capture.count)
                bench.app.processEvents()
                rows.append({"timestamps": timestamps, "whole_repaint_ms": bench.best(lambda: None)})
            finally:
                bench.close()
            path.unlink()
    ratio = rows[-1]["whole_repaint_ms"] / max(rows[0]["whole_repaint_ms"], 0.1)
    return {"size": size, "rows": rows, "ratio": round(ratio, 2), "max_ratio": max_ratio,
            "length_ratio": round(lengths[-1] / lengths[0], 1), "passed": ratio <= max_ratio}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", help="measure a real .vcd or .wlf instead of the synthetic capture")
    parser.add_argument("--wlf2vcd", help="path to wlf2vcd (default: WLF2VCD, PATH, then app settings)")
    parser.add_argument("--timestamps", type=int, default=2_000_000)
    parser.add_argument("--timestep", type=int, default=10)
    parser.add_argument("--signals", type=int, default=237)
    parser.add_argument("--one-bit", type=int, default=191)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--profile", choices=("load", "repaint"), help="print the top 25 functions by own time")
    parser.add_argument("--size", action="append", choices=tuple(SIZES), help="window size(s); default both")
    parser.add_argument("--zoom", action="append", choices=ZOOMS, help="zoom level(s); default all four")
    parser.add_argument("--skip-repaint", action="store_true")
    parser.add_argument("--workers", type=int, default=1,
                        help="parse processes: 1 parses in this process (default), 0 uses physical cores - 1")
    parser.add_argument("--disk", action="store_true", help="sample the peak size of the temporary capture folder")
    parser.add_argument("--block-cells", type=int, help="override SplitBusAssembler.BLOCK_CELLS")
    parser.add_argument("--ranges-per-worker", type=int, help="override vcd.RANGES_PER_WORKER")
    parser.add_argument("--verify-sliced", metavar="PATH.wlf",
                        help="load a WLF sliced and in one wlf2vcd run, print capture_differences, and exit")
    parser.add_argument("--check-scaling", action="store_true",
                        help="fail when whole-capture repaint grows with capture length (synthetic captures)")
    parser.add_argument("--max-ratio", type=float, default=3.0,
                        help="allowed repaint ratio of the longer to the shorter capture for --check-scaling")
    args = parser.parse_args(argv)
    if args.check_scaling:
        result = check_scaling(max_ratio=args.max_ratio, workers=args.workers if args.workers > 1 else 0,
                               seed=args.seed)
        print(json.dumps(result, indent=2))
        if not result["passed"]:
            raise SystemExit(f"whole-capture repaint grew {result['ratio']}x with a "
                             f"{result['length_ratio']}x longer capture (limit {result['max_ratio']}x)")
        return
    if args.verify_sliced:
        print(json.dumps(verify_sliced(args.verify_sliced, args.wlf2vcd, args.workers if args.workers > 1 else 0),
                         indent=2, ensure_ascii=False))
        return
    if args.ranges_per_worker:
        from ila_viewer import vcd
        vcd.RANGES_PER_WORKER = args.ranges_per_worker
    if args.block_cells:
        from ila_viewer.vcd import SplitBusAssembler
        SplitBusAssembler.BLOCK_CELLS = args.block_cells
    report = {}
    with tempfile.TemporaryDirectory(prefix="ila_benchmark_vcd_") as folder:
        load_profiler = Profiler(args.profile == "load")
        if args.file:
            path = Path(args.file)
            report["source"] = str(path)
        else:
            synthetic = SyntheticVcd(args.timestamps, args.timestep, args.signals, args.one_bit, args.seed)
            path = Path(folder) / "synthetic.vcd"
            start = time.perf_counter()
            changes = synthetic.write(path)
            report["source"] = "synthetic"
            report["generator"] = {"timestamps": args.timestamps, "timestep": args.timestep,
                                   "declared_signals": synthetic.signal_count, "one_bit": args.one_bit,
                                   "changes_written": changes, "seed": args.seed,
                                   "seconds": round(time.perf_counter() - start, 2)}
        capture, report["load"] = load_capture(path, load_profiler, args.wlf2vcd, args.workers, args.disk)
        load_profiler.report("load")
        try:
            if not args.skip_repaint:
                repaint_profiler = Profiler(args.profile == "repaint")
                bench = RepaintBench(capture)
                capture = None  # the window owns and closes it
                try:
                    report["repaint"] = bench.table(args.size or tuple(SIZES), args.zoom or ZOOMS, repaint_profiler)
                finally:
                    bench.close()
                repaint_profiler.report("repaint")
        finally:
            if capture is not None:
                capture.close()
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
