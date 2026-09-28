# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Per-export trigger editor with a signal picker that preserves the text caret."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout, QCheckBox)


class CopyConditionDialog(QDialog):
    def __init__(self, signals, compiler, parent=None, full_names=True):
        super().__init__(parent)
        self.setWindowTitle("Условие Copy Values")
        self.resize(620, 440)
        self.compiler = compiler
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Условие проверяется в заданной фазе через каждый период. Пустое условие копирует всё."))
        self.expression = QLineEdit()
        self.expression.setPlaceholderText("например: tx_ready && tx_we && tx_data == FE")
        self.expression.setClearButtonEnabled(True)
        layout.addWidget(self.expression)
        filters = QHBoxLayout()
        self.name_filter = QLineEdit()
        self.name_filter.setPlaceholderText("Фильтр сигналов по имени…")
        self.name_filter.setClearButtonEnabled(True)
        self.full_names = QCheckBox("Показывать полные имена")
        self.full_names.setChecked(full_names)
        filters.addWidget(self.name_filter, 1)
        filters.addWidget(self.full_names)
        layout.addLayout(filters)
        layout.addWidget(QLabel("Двойной щелчок добавит имя и пробел в позицию курсора:"))
        self.signal_list = QListWidget()
        self.signals = list(signals)
        self.name_filter.textChanged.connect(self.refresh_signal_list)
        self.full_names.toggled.connect(self.refresh_signal_list)
        self.refresh_signal_list()
        self.signal_list.itemDoubleClicked.connect(lambda item: self.insert_signal(item.data(Qt.ItemDataRole.UserRole)))
        layout.addWidget(self.signal_list, 1)
        controls = QHBoxLayout()
        self.add_button = QPushButton("Добавить сигнал")
        self.add_button.clicked.connect(self.add_selected)
        controls.addWidget(self.add_button)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.error = QLabel("Операторы: &&, ||, !, ~, ==, !=, <, >, <=, >= и скобки.")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def add_selected(self):
        item = self.signal_list.currentItem()
        if item:
            self.insert_signal(item.data(Qt.ItemDataRole.UserRole))

    def refresh_signal_list(self, *_):
        query = self.name_filter.text().casefold().strip()
        self.signal_list.clear()
        for signal in self.signals:
            if query and query not in signal.name.casefold() and query not in signal.short_name.casefold():
                continue
            item = QListWidgetItem(signal.name if self.full_names.isChecked() else signal.short_name)
            item.setData(Qt.ItemDataRole.UserRole, signal)
            self.signal_list.addItem(item)

    def insert_signal(self, signal):
        token = self.compiler.token_for(signal) + " "
        position = self.expression.cursorPosition()
        text = self.expression.text()
        self.expression.setText(text[:position] + token + text[position:])
        self.expression.setFocus(Qt.FocusReason.OtherFocusReason)
        self.expression.setCursorPosition(position + len(token))

    def validate_and_accept(self):
        try:
            self.compiler.compile(self.expression.text())
        except ValueError as exc:
            self.error.setText(str(exc))
            self.expression.setFocus()
            return
        self.accept()

    def condition_text(self):
        return self.expression.text().strip()
