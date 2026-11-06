# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Replaceable theme objects and background-aware signal palettes."""
from dataclasses import dataclass
from pathlib import Path
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor

ASSETS = Path(__file__).resolve().parent / "assets"


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

    # The waveform keeps its many meaningful colors; the chrome around it uses the
    # neutral ramp of the theme plus one accent (the theme's bus color).
    @property
    def accent(self):
        return self.bus

    @property
    def dark(self):
        return QColor(self.background).lightnessF() < 0.5

    @property
    def on_accent(self):
        """Text and tick color on an accent fill."""
        return "#14161a" if QColor(self.accent).lightnessF() > 0.55 else "#ffffff"

    def a_b_band(self):
        """Fill between markers A and B: marker A's hue, faint enough to read waves through."""
        color = QColor(self.marker_a)
        color.setAlpha(16 if self.dark else 24)
        return color

    def stylesheet(self):
        tick = (ASSETS / ("check_dark.svg" if self.on_accent == "#14161a" else "check_light.svg")).as_posix()
        return f"""
QMainWindow, QWidget {{ background: {self.panel}; color: {self.text}; }}
QLabel, QCheckBox, QRadioButton {{ background: transparent; }}
QWidget#plain {{ background: transparent; }}
QLabel:disabled, QCheckBox:disabled {{ color: {self.muted}; }}

QFrame#copyPeriodBanner {{ background: {self.alternate}; border: 1px solid {self.grid};
    border-left: 3px solid {self.marker_a}; border-radius: 6px; }}
QFrame#copyPeriodBanner QLabel#bannerTitle {{ font-weight: 600; }}
QLabel[role="muted"] {{ color: {self.muted}; }}
QLabel[role="readout"] {{ font-family: Consolas, "Cascadia Mono", monospace; }}
QLabel[role="error"] {{ color: {self.unknown}; }}
QLabel[keycap="true"] {{ background: {self.background}; border: 1px solid {self.grid};
    border-bottom-width: 2px; border-radius: 4px; padding: 0px 6px; color: {self.text}; }}

QToolBar {{ border: 0; spacing: 2px; padding: 4px 8px; }}
QToolBar::separator {{ background: {self.grid}; width: 1px; margin: 6px 8px; }}
QToolBar QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 4px;
    padding: 4px 10px; }}
QToolBar QToolButton:hover {{ background: {self.selected}; }}
QToolBar QToolButton:pressed {{ background: {self.grid}; }}
QToolBar QToolButton:disabled {{ color: {self.muted}; }}

QPushButton, QToolButton {{ background: {self.alternate}; border: 1px solid {self.grid};
    border-radius: 4px; padding: 4px 12px; min-height: 20px; }}
QPushButton:hover, QToolButton:hover {{ background: {self.selected}; }}
QPushButton:pressed, QToolButton:pressed {{ background: {self.grid}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {self.muted}; }}
QPushButton:focus, QToolButton:focus {{ border-color: {self.accent}; }}
QPushButton:default {{ border-color: {self.muted}; }}
QPushButton[primary="true"] {{ background: {self.accent}; color: {self.on_accent}; border-color: {self.accent}; }}
QPushButton[primary="true"]:hover {{ background: {self.accent}; border-color: {self.text}; }}
QPushButton[primary="true"]:disabled {{ background: {self.alternate}; color: {self.muted}; border-color: {self.grid}; }}
QPushButton[segment="true"] {{ border-radius: 0px; padding: 4px 10px; margin: 0px; }}
QPushButton[segment="true"][first="true"] {{ border-top-left-radius: 4px; border-bottom-left-radius: 4px; }}
QPushButton[segment="true"][last="true"] {{ border-top-right-radius: 4px; border-bottom-right-radius: 4px; }}
QPushButton[segment="true"]:checked {{ background: {self.selected}; border-color: {self.accent}; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{ background: {self.background};
    border: 1px solid {self.grid}; border-radius: 4px; padding: 4px 6px; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {self.accent}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ color: {self.muted}; }}
QComboBox QAbstractItemView {{ background: {self.background}; border: 1px solid {self.grid};
    selection-background-color: {self.selected}; selection-color: {self.text}; }}

QCheckBox {{ spacing: 6px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {self.muted};
    border-radius: 3px; background: {self.background}; }}
QCheckBox::indicator:hover {{ border-color: {self.accent}; }}
QCheckBox::indicator:checked {{ background: {self.accent}; border-color: {self.accent}; image: url("{tick}"); }}
QCheckBox::indicator:disabled {{ border-color: {self.grid}; }}

QGroupBox {{ border: 1px solid {self.grid}; border-radius: 6px; margin-top: 10px; padding: 8px 8px 6px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0px 4px; color: {self.muted}; }}
QTabWidget::pane {{ border: 1px solid {self.grid}; border-radius: 4px; }}
QHeaderView::section {{ background: {self.panel}; color: {self.muted}; border: 0;
    border-right: 1px solid {self.grid}; border-bottom: 1px solid {self.grid}; padding: 3px 6px; }}
QTableView, QTableWidget, QListView, QTreeView {{ background: {self.background}; gridline-color: {self.grid};
    selection-background-color: {self.selected}; selection-color: {self.text}; border: 1px solid {self.grid}; }}

QStatusBar {{ background: {self.background}; }}
QProgressBar {{ background: {self.background}; border: 1px solid {self.grid}; border-radius: 4px;
    text-align: center; }}
QProgressBar::chunk {{ background: {self.accent}; border-radius: 3px; }}

QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0px; }}
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0px; }}
QScrollBar::handle {{ background: {self.grid}; border-radius: 4px; margin: 2px; }}
QScrollBar::handle:horizontal {{ min-width: 32px; }}
QScrollBar::handle:vertical {{ min-height: 32px; }}
QScrollBar::handle:hover, QScrollBar::handle:pressed {{ background: {self.muted}; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; border: 0; }}

QMenu {{ background: {self.panel}; border: 1px solid {self.grid}; padding: 4px 0px; }}
QMenu::item {{ padding: 4px 24px 4px 12px; }}
QMenu::item:selected {{ background: {self.selected}; }}
QMenu::item:disabled {{ color: {self.muted}; }}
QMenu::separator {{ height: 1px; background: {self.grid}; margin: 4px 8px; }}
QMenuBar::item {{ padding: 4px 8px; background: transparent; }}
QMenuBar::item:selected {{ background: {self.selected}; border-radius: 4px; }}
QToolTip {{ color: {self.text}; background: {self.panel}; border: 1px solid {self.grid}; padding: 4px; }}
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
