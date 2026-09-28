# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Per-export trigger editor with a signal picker that preserves the text caret."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton,
                               QVBoxLayout, QCheckBox, QComboBox, QTableWidget,
                               QTableWidgetItem, QWidget)

from .bits import BitCodec
from .formats import FORMATS


class CopyConditionDialog(QDialog):
    def __init__(self, signals, compiler, parent=None, full_names=True, expression_text="",
                 title="Условие Copy Values", description=None):
        super().__init__(parent)
        self.reselect_period = False
        self.setWindowTitle(title)
        screen = self.screen()
        self.resize(760, min(780, screen.availableGeometry().height() - 80) if screen else 780)
        self.compiler = compiler
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(description or "Условие проверяется в заданной фазе через каждый период. Пустое условие копирует всё."))
        self.expression = QLineEdit()
        self.expression.setPlaceholderText("например: tx_ready && tx_we && tx_data == FE")
        self.expression.setClearButtonEnabled(True)
        self.expression.setText(expression_text)
        layout.addWidget(self.expression)
        self.picker_toggle = QPushButton("Сигналы для условия ▾")
        self.picker_toggle.clicked.connect(self.toggle_picker)
        layout.addWidget(self.picker_toggle)
        self.picker_panel = QWidget()
        picker = QVBoxLayout(self.picker_panel)
        picker.setContentsMargins(0, 0, 0, 0)
        filters = QHBoxLayout()
        self.name_filter = QLineEdit()
        self.name_filter.setPlaceholderText("Фильтр сигналов по имени…")
        self.name_filter.setClearButtonEnabled(True)
        self.full_names = QCheckBox("Показывать полные имена")
        self.full_names.setChecked(full_names)
        filters.addWidget(self.name_filter, 1)
        filters.addWidget(self.full_names)
        picker.addLayout(filters)
        picker.addWidget(QLabel("Двойной щелчок добавит имя и пробел в позицию курсора:"))
        self.signal_list = QListWidget()
        self.signal_list.setMinimumHeight(300)
        self.signals = list(signals)
        self.name_filter.textChanged.connect(self.refresh_signal_list)
        self.full_names.toggled.connect(self.refresh_signal_list)
        self.refresh_signal_list()
        self.signal_list.itemDoubleClicked.connect(lambda item: self.insert_signal(item.data(Qt.ItemDataRole.UserRole)))
        picker.addWidget(self.signal_list, 1)
        controls = QHBoxLayout()
        self.add_button = QPushButton("Добавить сигнал")
        self.add_button.clicked.connect(self.add_selected)
        controls.addWidget(self.add_button)
        controls.addStretch(1)
        picker.addLayout(controls)
        layout.addWidget(self.picker_panel, 1)
        self.period_button = QPushButton("Выбрать период заново")
        self.period_button.clicked.connect(self.request_period_change)
        layout.addWidget(self.period_button)
        self.error = QLabel("Операторы: &&, ||, !, ~, = / ==, !=, <, >, <=, >= и скобки.")
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

    def toggle_picker(self):
        visible = not self.picker_panel.isVisible()
        self.picker_panel.setVisible(visible)
        self.picker_toggle.setText("Сигналы для условия ▴" if visible else "Сигналы для условия ▾")

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

    def request_period_change(self):
        self.reselect_period = True
        self.reject()


class CopyOptionsDialog(CopyConditionDialog):
    """Per-export options; each selected bus can use its own radix and bit range."""

    def __init__(self, signals, compiler, targets, service, parent=None, full_names=True,
                 expression_text="", separator="space", available_targets=None):
        super().__init__(signals, compiler, parent, full_names, expression_text,
                         title="Копирование значений")
        self.service = service
        self.targets = list(targets)
        selected_ids = {node.id for node, _ in targets}
        if available_targets:
            self.targets.extend((node, offsets) for node, offsets in available_targets if node.id not in selected_ids)
        self.resize(900, self.height())
        self.channels = QTableWidget(len(self.targets), 4)
        self.channels.setHorizontalHeaderLabels(("Копировать", "Сигнал", "Radix", "Биты шины (например 7-4 2-1)"))
        self.channels.horizontalHeader().setStretchLastSection(True)
        self.channels.setColumnWidth(0, 90)
        self.channels.setColumnWidth(1, 255)
        self.channels.setColumnWidth(2, 155)
        self.channels.verticalHeader().setDefaultSectionSize(25)
        self.channel_radices = []
        self.channel_bits = []
        for row, (node, offsets) in enumerate(self.targets):
            include = QTableWidgetItem()
            include.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            include.setCheckState(Qt.CheckState.Checked if node.id in selected_ids else Qt.CheckState.Unchecked)
            self.channels.setItem(row, 0, include)
            name = QTableWidgetItem(node.signal.source.short_name)
            name.setToolTip(node.signal.source.name)
            self.channels.setItem(row, 1, name)
            box = QComboBox()
            box.addItem("Как у сигнала", "DEFAULT")
            for strategy in FORMATS.formats.values():
                box.addItem(strategy.label, strategy.key)
            self.channels.setCellWidget(row, 2, box)
            self.channel_radices.append(box)
            bits = QLineEdit()
            if offsets is not None:
                labels = BitCodec.labels(node.signal.source)
                values = [str(label) for index, label in enumerate(labels)
                          if node.signal.width - 1 - index in offsets]
                bits.setText(" ".join(values))
            bits.setEnabled(node.signal.width > 1)
            bits.setPlaceholderText("Все биты")
            self.channels.setCellWidget(row, 3, bits)
            self.channel_bits.append(bits)
        self.layout().insertWidget(self.layout().count() - 2, QLabel("Формат для каждого выбранного сигнала:"))
        self.layout().insertWidget(self.layout().count() - 2, self.channels)
        self.channels.setMinimumHeight(320)
        self.picker_panel.hide()
        self.picker_toggle.setText("Сигналы для условия ▾")
        self.picker_toggle.clicked.connect(lambda: self.channels.setMinimumHeight(
            120 if self.picker_panel.isVisible() else 320))
        options = QHBoxLayout()
        options.addWidget(QLabel("Разделитель:"))
        self.separator_box = QComboBox()
        for label, key in (("Пробелы", "space"), ("Запятые", "comma"),
                           ("Единый текст", "text"), ("Табуляция", "tab"),
                           ("Новая строка", "newline")):
            self.separator_box.addItem(label, key)
        self.separator_box.setCurrentIndex(max(0, self.separator_box.findData(separator)))
        options.addWidget(self.separator_box)
        self.unique_box = QCheckBox("Только уникальные значения")
        options.addWidget(self.unique_box)
        self.sort_box = QComboBox()
        self.sort_box.addItem("По появлению", "original")
        self.sort_box.addItem("По возрастанию", "ascending")
        self.sort_box.addItem("По убыванию", "descending")
        self.sort_box.setEnabled(False)
        self.unique_box.toggled.connect(self.sort_box.setEnabled)
        options.addWidget(self.sort_box)
        self.layout().insertLayout(self.layout().count() - 2, options)

    def channel_options(self):
        return {node.id: {"radix": self.channel_radices[index].currentData(),
                          "bits": self.channel_bits[index].text()}
                for index, (node, _) in enumerate(self.targets)
                if self.channels.item(index, 0).checkState() == Qt.CheckState.Checked}

    def selected_targets(self):
        return [(node, offsets) for index, (node, offsets) in enumerate(self.targets)
                if self.channels.item(index, 0).checkState() == Qt.CheckState.Checked]

    def validate_and_accept(self):
        try:
            for index, (node, _) in enumerate(self.targets):
                expression = self.channel_bits[index].text()
                if expression:
                    self.service.parse_bit_ranges(expression, BitCodec.labels(node.signal.source))
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        super().validate_and_accept()
