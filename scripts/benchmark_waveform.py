# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Reproducible synthetic large-capture benchmark; leaves only a JSON report."""
import json
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from ila_viewer.data import load_csv
from ila_viewer.window import MainWindow
from ila_viewer.themes import STYLE
from ila_viewer.preferences import PreferencesStore
from ila_viewer.bits import BitExpansionService


def main():
    app = QApplication([])
    if os.name == "nt":
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
        QFontDatabase.addApplicationFont("C:/Windows/Fonts/consola.ttf")
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(STYLE)
    window = MainWindow(preferences_store=PreferencesStore())
    window.resize(1280, 760)
    window.show()
    with tempfile.TemporaryDirectory(prefix="ila_benchmark_") as folder:
        path = Path(folder) / "million.csv"
        count = 1_000_000
        with path.open("w", newline="") as stream:
            stream.write("Sample in Buffer,Sample in Window,TRIGGER,bus[8:0],fast_bit,slow_bit,constant[31:0]\n")
            stream.write("Radix - UNSIGNED,UNSIGNED,UNSIGNED,HEX,HEX,HEX,HEX\n")
            for i in range(count):
                stream.write(f"{i},{i % 1024},{int(i == 500000)},{(i // 32) % 512:03X},{i % 2},{(i // 4000) % 2},1234ABCD\n")
        start = time.perf_counter()
        cap = load_csv(path)
        load_seconds = time.perf_counter() - start
        disk_bytes = sum(p.stat().st_size for p in Path(cap.storage.name).iterdir())
        window.loaded(cap)
        app.processEvents()
        v = window.view
        render_ms = []
        for left, span in [(0, count), (400000, 500000), (450000, 1000), (499950, 100)]:
            v.set_range(left, span)
            app.processEvents()
            start = time.perf_counter()
            for _ in range(10):
                v.viewport().grab()
            render_ms.append({"span": span, "mean_ms": round((time.perf_counter() - start) * 100, 2)})
        start = time.perf_counter()
        found = cap.signals[1].search(0xAC, 0, 1)
        search_ms = (time.perf_counter() - start) * 1000
        assert found == (5504, False), found
        assert cap.signals[2].transition(999998, 1) == 999999
        start = time.perf_counter()
        bus_node = v.tree.root.children[1]
        bits = BitExpansionService().build(cap.signals[1], cap.storage.name)
        cap.derived.append(bits)
        v.tree.add_bits(bus_node, bits.signals)
        expand_seconds = time.perf_counter() - start
        v.refresh_rows(bus_node)
        v.fit()
        app.processEvents()
        start = time.perf_counter()
        for _ in range(10):
            v.viewport().grab()
        expanded_render_ms = (time.perf_counter() - start) * 100
        assert bits.signals[-1].transition(0, 1) == 32
        assert bits.signals[0].transition(0, 1) == 8192
        v.set_cursor(0)
        v.set_cursor(count - 1, True)
        request = window.copy_service.snapshot(v)
        start = time.perf_counter()
        copied = window.copy_service.build(request)
        copy_seconds = time.perf_counter() - start
        assert copied.count == count - 1  # B is an exclusive copy boundary.
        assert copied.text.startswith("000 000 000")
        report = {"samples": count, "signals": len(cap.signals), "csv_bytes": path.stat().st_size,
                  "index_bytes": disk_bytes, "load_seconds": round(load_seconds, 3),
                  "render": render_ms, "search_ms": round(search_ms, 3),
                  "expand_9_bits_seconds": round(expand_seconds, 3),
                  "expanded_full_capture_render_ms": round(expanded_render_ms, 2),
                  "copy_million_values_seconds": round(copy_seconds, 3),
                  "copy_characters": len(copied.text),
                  "note": "Synthetic capture with alternating bit and 9-bit bus; offscreen render on this machine."}
        artifacts = Path(__file__).resolve().parents[1] / "tests" / "artifacts"
        artifacts.mkdir(exist_ok=True)
        (artifacts / "benchmark.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
        window.close()
        app.processEvents()


if __name__ == "__main__":
    main()
