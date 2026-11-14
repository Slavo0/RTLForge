# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Deterministic waveform frames for pixel-identity tests of rendering optimizations.

The default-style PNGs in ``tests/fixtures/frames`` were rendered by the renderer as it
was before the WLF performance work (``docs/WLF_PERF_TASKS.md``, tasks 2-4); the
``-styled`` ones (fills, separate styles, a row color, the light theme) were added in
task 4 from the task 3 renderer, which the default frames had proven unchanged.

Text is drawn with the fonts installed on the machine, so ``environment.json`` records
where the frames were made. On another environment the pixel test is skipped; regenerate
the frames there (``python -m tests.frames``) only from a revision known to be right.
"""
import dataclasses
import json
import os
import platform
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

FRAMES = Path(__file__).resolve().parent / "fixtures" / "frames"
ENVIRONMENT = FRAMES / "environment.json"
WINDOW = (1100, 640)
STYLED = {"fill_high": True, "fill_bus": True, "separate_signal_styles": True, "theme": "light"}


class NullClipboard:
    def write(self, text):
        pass


def irregular_vcd(path, count=6000, seed=7):
    """A VCD with irregular time steps, a clock, vectors, X values and a split bus."""
    rng = np.random.default_rng(seed)
    steps = rng.integers(1, 38, count)
    steps[0] = 0
    times = np.cumsum(steps) + 1000
    lines = ["$timescale 1ns $end", "$scope module top $end",
             "$var wire 1 ! clk $end", "$var reg 8 # data [7:0] $end", "$var reg 1 $ valid $end",
             "$var integer 32 % count $end",
             *[f"$var wire 1 {chr(38 + bit)} addr [{bit}] $end" for bit in (3, 2, 1, 0)],
             "$var reg 1 * rare $end", "$upscope $end", "$enddefinitions $end"]
    count_value = 0
    for row, time in enumerate(times):
        lines.append(f"#{int(time)}")
        lines.append(f"{row % 2}!")
        if row == 0:
            lines += ["bx #", "bx $", "b0 %", "x&", "x'", "x(", "x)", "b0 *"]
            continue
        if rng.random() < 0.3:
            lines.append("bx #" if rng.random() < 0.02 else f"b{int(rng.integers(0, 256)):b} #")
        if rng.random() < 0.05:
            lines.append(f"b{int(rng.integers(0, 2))} $")
        if rng.random() < 0.5:
            count_value += 1
            lines.append(f"b{count_value:b} %")
        for bit in range(4):
            if rng.random() < 0.08:
                lines.append(f"{int(rng.integers(0, 2))}{chr(41 - bit)}")
        if row % 1500 == 0:
            lines.append(f"b{row // 1500 % 2} *")
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii")
    return [int(t) for t in times]


def dense_csv(path, count=20000):
    """A Vivado CSV with a fast bit, a bus and a slow bit (uniform sample axis)."""
    with Path(path).open("w", newline="") as stream:
        stream.write("Sample in Buffer,Sample in Window,TRIGGER,bus[8:0],fast_bit,slow_bit\n")
        stream.write("Radix - UNSIGNED,UNSIGNED,UNSIGNED,HEX,HEX,HEX\n")
        for i in range(count):
            stream.write(f"{i},{i % 1024},{int(i == count // 2)},{(i * 7 // 32) % 512:03X},{i % 2},{(i // 900) % 2}\n")


def views(count):
    """(name, left, span) zoom levels: whole, dense, mid, detailed and a fractional window."""
    return [("whole", 0, count), ("dense", count * 0.21, count * 0.3), ("mid", count * 0.5, 700),
            ("detail", count * 0.5 + 3, 40), ("fraction", count * 0.37 + 0.37, 97.3)]


class FrameRenderer:
    """A real MainWindow under the offscreen platform; renders its waveform viewport."""

    def __init__(self):
        from PySide6.QtGui import QFont, QFontDatabase
        from PySide6.QtWidgets import QApplication
        from ila_viewer.cpu import CpuTopology
        from ila_viewer.preferences import PreferencesStore
        from ila_viewer.window import MainWindow
        self.app = QApplication.instance() or QApplication([])
        if os.name == "nt":
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
            QFontDatabase.addApplicationFont("C:/Windows/Fonts/consola.ttf")
        self.app.setFont(QFont("Segoe UI", 10))
        self.window = MainWindow(preferences_store=PreferencesStore(), clipboard=NullClipboard(),
                                 cpu=CpuTopology(2, 2))
        self.window.resize(*WINDOW)
        self.window.show()
        self.app.processEvents()

    def close(self):
        self.window.close()
        self.app.processEvents()

    def environment(self):
        """What decides the rasterized text: platform, Qt and the font the view resolved."""
        from PySide6 import __version__ as pyside
        from PySide6.QtCore import qVersion
        from PySide6.QtGui import QFontInfo
        info = QFontInfo(self.window.view.viewport().font())
        return {"platform": f"{platform.system()} {platform.release()} {platform.machine()}",
                "qpa": self.app.platformName(), "qt": qVersion(), "pyside": pyside,
                "font_family": info.family(), "font_point_size": info.pointSizeF(),
                "font_pixel_size": info.pixelSize()}

    def style(self, styled):
        """Default preferences, or every optional fill and style on, in the light theme."""
        from ila_viewer.preferences import ViewerPreferences
        self.window.apply_preferences(dataclasses.replace(ViewerPreferences(), **(STYLED if styled else {})))

    def frames(self, capture, highlight=None, styled=False):
        """Yield (name, QImage) for each zoom level of ``views``."""
        from ila_viewer.conditions import ConditionCompiler
        from ila_viewer.highlights import HighlightGroup
        window, view = self.window, self.window.view
        self.style(styled)
        window.loaded(capture)
        if styled:
            view.tree.root.children[1].options.color = "#d04a9c"
        if highlight:
            condition = ConditionCompiler(capture.signals).compile(highlight)
            intervals = window.highlight_service.build(capture, condition, 0, capture.count - 1, 1, 0)
            window.highlights.add(HighlightGroup(highlight, highlight, "#ffcc74", intervals,
                                                 0, capture.count - 1, 1, 0))
        view.set_cursor(capture.count // 3, reveal=False)
        view.set_cursor(capture.count // 3 * 2, secondary=True, reveal=False)
        self.app.processEvents()
        for name, left, span in views(capture.count):
            view.set_range(left, span)
            self.app.processEvents()
            yield name, view.viewport().grab().toImage()


def render_all(folder):
    """(every fixture frame as {file name: QImage}, the rendering environment)."""
    from ila_viewer.data import load_csv
    from ila_viewer.vcd import VcdCaptureReader
    folder = Path(folder)
    irregular_vcd(folder / "irregular.vcd")
    dense_csv(folder / "dense.csv")
    renderer = FrameRenderer()
    images = {}
    try:
        environment = renderer.environment()
        for suffix, styled in (("", False), ("-styled", True)):
            # Each pass loads its own capture: installing a new one closes the previous.
            captures = [("vcd", lambda: VcdCaptureReader().load(folder / "irregular.vcd"), "valid"),
                        ("csv", lambda: load_csv(folder / "dense.csv"), "slow_bit")]
            for prefix, load, highlight in captures:
                for name, image in renderer.frames(load(), highlight, styled):
                    images[f"{prefix}{suffix}-{name}.png"] = image.copy()
        renderer.style(False)
    finally:
        renderer.close()
    return images, environment


def recorded_environment():
    try:
        return json.loads(ENVIRONMENT.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


if __name__ == "__main__":
    import sys
    import tempfile
    FRAMES.mkdir(parents=True, exist_ok=True)
    only_missing = "--missing" in sys.argv[1:]
    with tempfile.TemporaryDirectory() as folder:
        images, environment = render_all(folder)
        recorded = recorded_environment()
        if only_missing and recorded not in (None, environment):
            sys.exit(f"frames were recorded in {recorded}, this is {environment}")
        for name, image in images.items():
            if only_missing and (FRAMES / name).exists():
                continue
            assert image.save(str(FRAMES / name)), name
            print(name, image.width(), image.height())
        ENVIRONMENT.write_text(json.dumps(environment, indent=2) + "\n", encoding="utf-8")
