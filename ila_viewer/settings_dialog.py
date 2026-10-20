# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""General appearance settings, deliberately separate from signal context actions."""
from dataclasses import replace
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox,
                              QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                              QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget)
from .formats import FORMATS
from .preferences import ViewerPreferences


def _paint_swatch(button, color):
    button.setText(color)
    button.setStyleSheet(f"background: {color}; color: {'#111111' if QColor(color).lightness() > 140 else '#ffffff'}")


def _note(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setObjectName("settingsNote")
    return label


class AppearanceDialog(QDialog):
    """Tabs follow the user's mental model: general → signals → highlights → sources."""

    def __init__(self, preferences, themes, parent=None):
        super().__init__(parent)
        self.preferences = preferences
        self.themes = themes
        self.signal_colors = {key: dict(colors) for key, colors in preferences.signal_colors.items()}
        self.highlight_palette = list(preferences.highlight_palette)
        self.highlight_opacities = list(preferences.highlight_opacities)
        self.setWindowTitle("Параметры")
        self.resize(560, 380)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._general_page(), "Общие")
        self.tabs.addTab(self._signals_page(), "Сигналы")
        self.tabs.addTab(self._highlight_page(), "Подсветка")
        self.tabs.addTab(self._sources_page(), "Источники")
        layout.addWidget(self.tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
                                   | QDialogButtonBox.StandardButton.RestoreDefaults)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setText("По умолчанию")
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setToolTip(
            "Вернуть значения по умолчанию на текущей вкладке")
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self.restore_defaults)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.refresh_signal_colors()
        self.refresh_highlight_colors()

    def _general_page(self):
        page = QWidget()
        form = QFormLayout(page)
        self.theme_box = QComboBox()
        for theme in self.themes.themes.values():
            self.theme_box.addItem(theme.label, theme.key)
        self.theme_box.setCurrentIndex(max(0, self.theme_box.findData(self.preferences.theme)))
        form.addRow("Тема", self.theme_box)
        self.radix_box = QComboBox()
        self.radix_box.addItem("Как в файле захвата", "DEFAULT")
        for strategy in FORMATS.formats.values():
            if strategy.key != "REAL":
                self.radix_box.addItem(strategy.label, strategy.key)
        self.radix_box.setCurrentIndex(max(0, self.radix_box.findData(self.preferences.global_radix)))
        form.addRow("Radix по умолчанию", self.radix_box)
        self.names_box = QCheckBox("Показывать полные иерархические имена сигналов")
        self.names_box.setChecked(self.preferences.full_names)
        form.addRow(self.names_box)
        form.addRow(_note("Radix из меню сигнала применяется к выделенным строкам; "
                          "пункт Default возвращает сигнал к этому общему формату. "
                          "Общие параметры сохраняются для следующего запуска."))
        return page

    def _signals_page(self):
        page = QWidget()
        column = QVBoxLayout(page)
        colors = QGroupBox("Цвета (для выбранной темы)")
        form = QFormLayout(colors)
        self.separate_box = QCheckBox("Разные цвета для шин и однобитных сигналов")
        self.separate_box.setChecked(self.preferences.separate_signal_styles)
        form.addRow(self.separate_box)
        self.bus_color_button = QPushButton()
        self.bit_color_button = QPushButton()
        self.bus_color_button.clicked.connect(lambda: self.choose_signal_color("bus"))
        self.bit_color_button.clicked.connect(lambda: self.choose_signal_color("bit"))
        self.bus_color_label = QLabel()
        form.addRow(self.bus_color_label, self.bus_color_button)
        form.addRow("Однобитные сигналы", self.bit_color_button)
        reset_colors = QPushButton("Цвета темы")
        reset_colors.setToolTip("Сбросить собственные цвета сигналов для выбранной темы")
        reset_colors.clicked.connect(self.reset_signal_colors)
        form.addRow("", reset_colors)
        column.addWidget(colors)
        fill = QGroupBox("Заливка")
        fill_form = QVBoxLayout(fill)
        self.fill_box = QCheckBox("Закрашивать уровень 1 однобитных сигналов")
        self.fill_box.setChecked(self.preferences.fill_high)
        self.fill_bus_box = QCheckBox("Закрашивать шины")
        self.fill_bus_box.setChecked(self.preferences.fill_bus)
        fill_form.addWidget(self.fill_box)
        fill_form.addWidget(self.fill_bus_box)
        column.addWidget(fill)
        column.addStretch(1)
        self.theme_box.currentIndexChanged.connect(self.refresh_signal_colors)
        self.separate_box.toggled.connect(self.refresh_signal_colors)
        return page

    def _highlight_page(self):
        page = QWidget()
        column = QVBoxLayout(page)
        behaviour = QGroupBox("Поведение")
        behaviour_layout = QVBoxLayout(behaviour)
        self.hide_empty_highlights_box = QCheckBox("Скрывать панель, когда нет подсветок")
        self.hide_empty_highlights_box.setChecked(self.preferences.hide_empty_highlight_panel)
        self.restore_highlight_box = QCheckBox("После общего выключения восстановить только ранее включённые подсветки")
        self.restore_highlight_box.setChecked(self.preferences.restore_highlight_visibility)
        behaviour_layout.addWidget(self.hide_empty_highlights_box)
        behaviour_layout.addWidget(self.restore_highlight_box)
        column.addWidget(behaviour)
        styles = QGroupBox("Стили новых подсветок")
        styles_form = QFormLayout(styles)
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
            opacity.setToolTip("Непрозрачность заливки")
            opacity.setValue(self.highlight_opacities[index])
            self.highlight_opacity_boxes.append(opacity)
            row.addWidget(button, 1)
            row.addWidget(opacity)
            styles_form.addRow(f"Стиль {index + 1}", row)
        styles_form.addRow(_note("Созданную группу можно настроить отдельно кнопками «Цвет» и «Изменить» на панели подсветок."))
        column.addWidget(styles)
        column.addStretch(1)
        return page

    def _sources_page(self):
        page = QWidget()
        form = QFormLayout(page)
        converter_row = QHBoxLayout()
        self.wlf_converter_edit = QLineEdit(self.preferences.wlf2vcd_path)
        self.wlf_converter_edit.setPlaceholderText("Из PATH или переменной WLF2VCD, если поле пустое")
        converter_row.addWidget(self.wlf_converter_edit, 1)
        browse_converter = QPushButton("Обзор…")
        browse_converter.clicked.connect(self.choose_wlf_converter)
        converter_row.addWidget(browse_converter)
        form.addRow("Questa wlf2vcd", converter_row)
        form.addRow(_note("Vivado ILA CSV и VCD открываются без дополнительных программ. "
                          "Для .wlf укажите wlf2vcd.exe из установленного Questa/ModelSim."))
        return page

    def result_preferences(self):
        return replace(self.preferences, theme=self.theme_box.currentData(),
                       global_radix=self.radix_box.currentData(), fill_high=self.fill_box.isChecked(),
                       separate_signal_styles=self.separate_box.isChecked(), fill_bus=self.fill_bus_box.isChecked(),
                       signal_colors=self.signal_colors, highlight_palette=self.highlight_palette,
                       highlight_opacities=[box.value() for box in self.highlight_opacity_boxes],
                       restore_highlight_visibility=self.restore_highlight_box.isChecked(),
                       hide_empty_highlight_panel=self.hide_empty_highlights_box.isChecked(),
                       full_names=self.names_box.isChecked(),
                       wlf2vcd_path=self.wlf_converter_edit.text().strip())

    def restore_defaults(self):
        defaults = ViewerPreferences()
        page = self.tabs.currentIndex()
        if page == 0:
            self.theme_box.setCurrentIndex(max(0, self.theme_box.findData(defaults.theme)))
            self.radix_box.setCurrentIndex(max(0, self.radix_box.findData(defaults.global_radix)))
            self.names_box.setChecked(defaults.full_names)
        elif page == 1:
            self.signal_colors.clear()
            self.separate_box.setChecked(defaults.separate_signal_styles)
            self.fill_box.setChecked(defaults.fill_high)
            self.fill_bus_box.setChecked(defaults.fill_bus)
            self.refresh_signal_colors()
        elif page == 2:
            self.hide_empty_highlights_box.setChecked(defaults.hide_empty_highlight_panel)
            self.restore_highlight_box.setChecked(defaults.restore_highlight_visibility)
            # Keep the number of styles the user already has; only their values reset.
            count = len(self.highlight_color_buttons)
            palette, opacities = defaults.highlight_palette, defaults.highlight_opacities
            self.highlight_palette = [palette[index % len(palette)] for index in range(count)]
            for index, box in enumerate(self.highlight_opacity_boxes):
                box.setValue(opacities[index % len(opacities)])
            self.refresh_highlight_colors()
        else:
            self.wlf_converter_edit.clear()

    def choose_wlf_converter(self):
        path, _ = QFileDialog.getOpenFileName(self, "Выбрать wlf2vcd", self.wlf_converter_edit.text(),
                                              "Программы (*.exe);;Все файлы (*)")
        if path:
            self.wlf_converter_edit.setText(path)

    def refresh_signal_colors(self, *_):
        theme = self.themes.themes[self.theme_box.currentData()]
        saved = self.signal_colors.get(theme.key, {})
        separate = self.separate_box.isChecked()
        for kind, button, fallback in (("bus", self.bus_color_button, theme.bus),
                                       ("bit", self.bit_color_button, theme.bit)):
            color = (saved.get("bus", theme.bus) if kind == "bit" and not separate
                     else saved.get(kind, fallback))
            _paint_swatch(button, color)
        self.bit_color_button.setEnabled(separate)
        self.bus_color_label.setText("Шины" if separate else "Все сигналы")

    def reset_signal_colors(self):
        self.signal_colors.pop(self.theme_box.currentData(), None)
        self.refresh_signal_colors()

    def choose_signal_color(self, kind):
        theme = self.themes.themes[self.theme_box.currentData()]
        current = self.signal_colors.get(theme.key, {}).get(kind, getattr(theme, kind))
        color = QColorDialog.getColor(QColor(current), self, "Цвет сигнала")
        if color.isValid():
            self.signal_colors.setdefault(theme.key, {})[kind] = color.name()
            self.refresh_signal_colors()

    def refresh_highlight_colors(self):
        for button, color in zip(self.highlight_color_buttons, self.highlight_palette):
            _paint_swatch(button, color)

    def choose_highlight_color(self, index):
        color = QColorDialog.getColor(QColor(self.highlight_palette[index]), self, "Цвет подсветки")
        if color.isValid():
            self.highlight_palette[index] = color.name()
            self.refresh_highlight_colors()
