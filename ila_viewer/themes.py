# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Replaceable theme objects and background-aware signal palettes."""
from dataclasses import dataclass
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    background: str
    panel: str
    alternate: str
    selected: str
    grid: str
    text: str
    muted: str
    bus: str
    bit: str
    marker_a: str
    marker_b: str
    unknown: str

    def stylesheet(self):
        return f"""
QMainWindow, QWidget {{ background: {self.panel}; color: {self.text}; }}
QFrame#copyPeriodBanner {{ background: {self.selected}; border: 1px solid {self.marker_a}; border-radius: 5px; }}
QToolBar {{ border: 0; spacing: 5px; padding: 6px; }}
QToolButton, QPushButton {{ background: {self.alternate}; border: 1px solid {self.grid}; padding: 6px 10px; border-radius: 4px; }}
QToolButton:hover, QPushButton:hover {{ background: {self.selected}; }}
QToolButton:disabled, QPushButton:disabled {{ color: {self.muted}; }}
QLineEdit, QComboBox, QSpinBox {{ background: {self.background}; border: 1px solid {self.grid}; border-radius: 4px; padding: 5px; }}
QStatusBar {{ background: {self.background}; }}
QProgressBar {{ border: 1px solid {self.grid}; text-align: center; }}
QProgressBar::chunk {{ background: {self.selected}; }}
QScrollBar:horizontal {{ background: {self.panel}; height: 16px; }}
QScrollBar:vertical {{ background: {self.panel}; width: 16px; }}
QScrollBar::handle {{ background: {self.muted}; min-width: 24px; min-height: 24px; border-radius: 4px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QMenu {{ border: 1px solid {self.grid}; }}
QMenu::item:selected {{ background: {self.selected}; }}
QMenu::separator {{ height: 1px; background: {self.muted}; margin: 5px 0px; }}
QToolTip {{ color: {self.text}; background: {self.panel}; border: 1px solid {self.muted}; }}
"""


BLUE = Theme("blue", "Синяя", "#101722", "#1b2737", "#152030", "#243d57", "#34485e", "#d9e5f5", "#91a2ba", "#74b6ff", "#61dfb5", "#ffcc74", "#da9cff", "#ffc857")
DARK = Theme("dark", "Тёмная", "#161618", "#232326", "#1c1c1f", "#39393f", "#49494f", "#e6e6e8", "#a0a0aa", "#74b6ff", "#61dfb5", "#ffcc74", "#da9cff", "#ffc857")
LIGHT = Theme("light", "Светлая", "#f7f9fc", "#e7edf5", "#edf2f9", "#d3e3f7", "#b5c4d5", "#172b45", "#536780", "#1558a6", "#087b55", "#965d00", "#7c31ab", "#a84d00")


class ThemeManager(QObject):
    changed = Signal()

    def __init__(self, themes=None):
        super().__init__()
        self.themes = {t.key: t for t in (themes or [DARK, LIGHT, BLUE])}
        self.current = next(iter(self.themes.values()))

    def select(self, key):
        self.current = self.themes[key]
        self.changed.emit()


class SignalPalette:
    # Twenty-four colors, including light and dark alternatives for each hue.
    colors = ("#61dfb5", "#74b6ff", "#ffcc74", "#da9cff", "#ff7f96", "#65d6e8",
              "#b9dc72", "#ffab75", "#a8adff", "#f69cdb", "#e5e9f0", "#a8c3d9",
              "#087b55", "#1558a6", "#965d00", "#7c31ab", "#b72648", "#00768a",
              "#527900", "#a34d00", "#514bb0", "#a62d82", "#25344a", "#5a697a")

    @staticmethod
    def luminance(color):
        c = QColor(color)
        rgb = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4
               for v in (c.redF(), c.greenF(), c.blueF())]
        return sum(v * w for v, w in zip(rgb, (.2126, .7152, .0722)))

    def contrast(self, color, background):
        a, b = sorted((self.luminance(color), self.luminance(background)))
        return (b + .05) / (a + .05)

    @staticmethod
    def lab(color):
        c = QColor(color)
        rgb = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4
               for v in (c.redF(), c.greenF(), c.blueF())]
        x = (rgb[0] * .4124 + rgb[1] * .3576 + rgb[2] * .1805) / .95047
        y = rgb[0] * .2126 + rgb[1] * .7152 + rgb[2] * .0722
        z = (rgb[0] * .0193 + rgb[1] * .1192 + rgb[2] * .9505) / 1.08883
        f = lambda v: v ** (1 / 3) if v > .008856 else 7.787 * v + 16 / 116
        fx, fy, fz = f(x), f(y), f(z)
        return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)

    def distance(self, first, second):
        a, b = self.lab(first), self.lab(second)
        return sum((x - y) ** 2 for x, y in zip(a, b)) ** .5

    def recommended(self, theme, against=()):
        # Require contrast against the canvas and perceptual separation from
        # the signal colors already visible in the current waveform.
        return {c for c in self.colors
                if min(self.contrast(c, theme.background), self.contrast(c, theme.selected)) >= 3.0
                and all(self.distance(c, other) >= 42 for other in against)}


STYLE = DARK.stylesheet()
