# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""General appearance settings, deliberately separate from signal context actions."""
from dataclasses import replace
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                              QFormLayout, QLabel, QTabWidget, QVBoxLayout, QWidget)
from .formats import FORMATS


class AppearanceDialog(QDialog):
    def __init__(self, preferences, themes, parent=None):
        super().__init__(parent)
        self.preferences = preferences
        self.setWindowTitle("Настройки вида")
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
        self.fill_box = QCheckBox("Закрашивать уровень 1 однобитных сигналов")
        self.fill_box.setChecked(preferences.fill_high)
        form.addRow(self.fill_box)
        self.names_box = QCheckBox("Полные имена сигналов")
        self.names_box.setChecked(preferences.full_names)
        form.addRow(self.names_box)
        note = QLabel("Общие настройки сохраняются для следующего запуска.\n"
                      "Radix в меню сигналов применяется к текущему выделению.\n"
                      "Default в меню сигнала возвращает общий формат.")
        note.setWordWrap(True)
        form.addRow(note)
        tabs.addTab(page, "Вид")
        copy_page = QWidget()
        copy_form = QFormLayout(copy_page)
        self.separator_box = QComboBox()
        for label, key in (("Пробелы", "space"), ("Запятые", "comma"), ("Единый текст", "text"),
                           ("Табуляция", "tab"), ("Новая строка", "newline")):
            self.separator_box.addItem(label, key)
        self.separator_box.setCurrentIndex(self.separator_box.findData(preferences.copy_separator))
        copy_form.addRow("Разделитель значений", self.separator_box)
        self.samples_box = QComboBox()
        self.samples_box.addItem("Каждый отсчёт, включая повторы", "all")
        self.samples_box.addItem("Только изменения значений", "changes")
        self.samples_box.setCurrentIndex(self.samples_box.findData(preferences.copy_samples))
        copy_form.addRow("Копировать", self.samples_box)
        copy_note = QLabel("Отсчёт A включается, B служит исключающей конечной границей.\n"
                           "Биты одной шины собираются в одно значение, старший выбранный бит слева.\n"
                           "Для нескольких сигналов — отдельная строка на каждый.\n"
                           "Radix выбирается в меню Copy Values; по умолчанию используется формат шины.")
        copy_note.setWordWrap(True)
        copy_form.addRow(copy_note)
        tabs.addTab(copy_page, "Копирование")
        layout.addWidget(tabs)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_preferences(self):
        return replace(self.preferences, theme=self.theme_box.currentData(),
                       global_radix=self.radix_box.currentData(), fill_high=self.fill_box.isChecked(),
                       full_names=self.names_box.isChecked(), copy_separator=self.separator_box.currentData(),
                       copy_samples=self.samples_box.currentData())
