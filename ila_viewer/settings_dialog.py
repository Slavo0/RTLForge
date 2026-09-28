# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""General appearance settings, deliberately separate from signal context actions."""
from dataclasses import replace
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                              QFormLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox,
                              QTabWidget, QVBoxLayout, QWidget)
from .formats import FORMATS


class AppearanceDialog(QDialog):
    def __init__(self, preferences, themes, parent=None):
        super().__init__(parent)
        self.preferences = preferences
        self.themes = themes
        self.signal_colors = {key: dict(colors) for key, colors in preferences.signal_colors.items()}
        self.highlight_palette = list(preferences.highlight_palette)
        self.highlight_opacities = list(preferences.highlight_opacities)
        self.setWindowTitle("Настройки")
        self.resize(520, 300)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        page = QWidget()
        form = QFormLayout(page)
        self.theme_box = QComboBox()
        for theme in themes.themes.values():
            self.theme_box.addItem(theme.label, theme.key)
        self.theme_box.setCurrentIndex(max(0, self.theme_box.findData(preferences.theme)))
        form.addRow("Тема", self.theme_box)
        self.radix_box = QComboBox()
        self.radix_box.addItem("Исходный формат CSV", "DEFAULT")
        for strategy in FORMATS.formats.values():
            if strategy.key != "REAL":
                self.radix_box.addItem(strategy.label, strategy.key)
        self.radix_box.setCurrentIndex(max(0, self.radix_box.findData(preferences.global_radix)))
        form.addRow("Общий Radix", self.radix_box)
        self.separate_box = QCheckBox("Раздельный вид шин и однобитных сигналов")
        self.separate_box.setChecked(preferences.separate_signal_styles)
        form.addRow(self.separate_box)
        self.bus_color_button = QPushButton()
        self.bit_color_button = QPushButton()
        self.bus_color_button.clicked.connect(lambda: self.choose_signal_color("bus"))
        self.bit_color_button.clicked.connect(lambda: self.choose_signal_color("bit"))
        form.addRow("Цвет шин / общий цвет", self.bus_color_button)
        form.addRow("Цвет однобитных", self.bit_color_button)
        self.fill_box = QCheckBox("Закрашивать уровень 1 / общий вид")
        self.fill_box.setChecked(preferences.fill_high)
        form.addRow(self.fill_box)
        self.fill_bus_box = QCheckBox("Закрашивать шины")
        self.fill_bus_box.setChecked(preferences.fill_bus)
        form.addRow(self.fill_bus_box)
        self.theme_box.currentIndexChanged.connect(self.refresh_signal_colors)
        self.separate_box.toggled.connect(self.refresh_signal_colors)
        self.refresh_signal_colors()
        self.names_box = QCheckBox("Полные имена сигналов")
        self.names_box.setChecked(preferences.full_names)
        form.addRow(self.names_box)
        note = QLabel("Общие настройки сохраняются для следующего запуска.\n"
                      "Radix в меню сигналов применяется к текущему выделению.\n"
                      "Default в меню сигнала возвращает общий формат.")
        note.setWordWrap(True)
        form.addRow(note)
        tabs.addTab(page, "Вид")
        highlight_page = QWidget()
        highlight_form = QFormLayout(highlight_page)
        self.hide_empty_highlights_box = QCheckBox("Скрывать панель, когда нет подсветок")
        self.hide_empty_highlights_box.setChecked(preferences.hide_empty_highlight_panel)
        highlight_form.addRow(self.hide_empty_highlights_box)
        self.restore_highlight_box = QCheckBox("После общего выключения восстановить только ранее включённые подсветки")
        self.restore_highlight_box.setChecked(preferences.restore_highlight_visibility)
        highlight_form.addRow(self.restore_highlight_box)
        self.highlight_color_buttons = []
        self.highlight_opacity_boxes = []
        for index in range(len(self.highlight_palette)):
            row = QHBoxLayout()
            button = QPushButton()
            button.clicked.connect(lambda _checked=False, i=index: self.choose_highlight_color(i))
            self.highlight_color_buttons.append(button)
            opacity = QSpinBox()
            opacity.setRange(0, 100)
            opacity.setSuffix(" %")
            opacity.setValue(self.highlight_opacities[index])
            self.highlight_opacity_boxes.append(opacity)
            row.addWidget(button)
            row.addWidget(opacity)
            highlight_form.addRow(f"Стиль {index + 1}", row)
        self.refresh_highlight_colors()
        highlight_form.addRow(QLabel("Цвет и прозрачность стилей сохраняются. Созданную группу можно дополнительно настроить отдельно."))
        tabs.addTab(highlight_page, "Подсветка")
        layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_preferences(self):
        return replace(self.preferences, theme=self.theme_box.currentData(),
                       global_radix=self.radix_box.currentData(), fill_high=self.fill_box.isChecked(),
                       separate_signal_styles=self.separate_box.isChecked(), fill_bus=self.fill_bus_box.isChecked(),
                       signal_colors=self.signal_colors, highlight_palette=self.highlight_palette,
                       highlight_opacities=[box.value() for box in self.highlight_opacity_boxes],
                       restore_highlight_visibility=self.restore_highlight_box.isChecked(),
                       hide_empty_highlight_panel=self.hide_empty_highlights_box.isChecked(),
                       full_names=self.names_box.isChecked())

    def refresh_signal_colors(self, *_):
        theme = self.themes.themes[self.theme_box.currentData()]
        saved = self.signal_colors.get(theme.key, {})
        for kind, button, fallback in (("bus", self.bus_color_button, theme.bus),
                                       ("bit", self.bit_color_button, theme.bit)):
            color = (saved.get("bus", theme.bus) if kind == "bit" and not self.separate_box.isChecked()
                     else saved.get(kind, fallback))
            button.setText(color)
            button.setStyleSheet(f"background: {color}; color: {'#111111' if QColor(color).lightness() > 140 else '#ffffff'}")
        self.bit_color_button.setEnabled(self.separate_box.isChecked())

    def choose_signal_color(self, kind):
        theme = self.themes.themes[self.theme_box.currentData()]
        current = self.signal_colors.get(theme.key, {}).get(kind, getattr(theme, kind))
        color = QColorDialog.getColor(QColor(current), self, "Цвет сигнала")
        if color.isValid():
            self.signal_colors.setdefault(theme.key, {})[kind] = color.name()
            self.refresh_signal_colors()

    def refresh_highlight_colors(self):
        for button, color in zip(self.highlight_color_buttons, self.highlight_palette):
            button.setText(color)
            button.setStyleSheet(f"background: {color}; color: {'#111111' if QColor(color).lightness() > 140 else '#ffffff'}")

    def choose_highlight_color(self, index):
        color = QColorDialog.getColor(QColor(self.highlight_palette[index]), self, "Цвет подсветки")
        if color.isValid():
            self.highlight_palette[index] = color.name()
            self.refresh_highlight_colors()
