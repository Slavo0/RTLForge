# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Editors and compact viewport panel for capture-scoped highlight groups."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QSpinBox, QWidget)

from .copy_dialog import CopyConditionDialog


class HighlightConditionDialog(CopyConditionDialog):
    def __init__(self, signals, compiler, palette, opacities, parent=None, group=None, draft=None, full_names=True):
        draft = draft or {}
        super().__init__(signals, compiler, parent, full_names,
                         expression_text=draft.get("expression", group.expression if group else ""),
                         title="Редактировать подсветку" if group else "Новая подсветка",
                         description="Условие проверяется в выбранной фазе периода. Совпавшие участки закрашиваются на диаграмме.")
        self.name_edit = QLineEdit(draft.get("name", group.name if group else "Подсветка"))
        self.layout().insertWidget(1, QLabel("Название группы:"))
        self.layout().insertWidget(2, self.name_edit)
        self.palette = palette
        self.opacities = opacities
        self.style_box = QComboBox()
        for index, color in enumerate(palette):
            swatch = QPixmap(18, 18)
            swatch.fill(QColor(color))
            self.style_box.addItem(QIcon(swatch), f"Подсветка {index + 1} · {opacities[index]} %", index)
        self.style_box.setCurrentIndex(min(draft.get("style", 0), len(palette) - 1))
        self.layout().insertWidget(3, QLabel("Стиль подсветки:"))
        self.layout().insertWidget(4, self.style_box)
        self.color_button = None
        self.opacity_box = None
        if group:
            self.edited_color = draft.get("color", group.color)
            self.color_button = QPushButton()
            self.color_button.clicked.connect(self.choose_color)
            self.opacity_box = QSpinBox()
            self.opacity_box.setRange(0, 100)
            self.opacity_box.setSuffix(" %")
            self.opacity_box.setValue(draft.get("opacity", group.opacity))
            self.layout().insertWidget(self.layout().count() - 2, QLabel("Изменить цвет и прозрачность этой группы:"))
            self.layout().insertWidget(self.layout().count() - 2, self.color_button)
            self.layout().insertWidget(self.layout().count() - 2, self.opacity_box)
            self.update_color_button()
            self.style_box.currentIndexChanged.connect(self.apply_preset_to_edit)

    @property
    def color(self):
        return self.edited_color if self.color_button else self.palette[self.style_box.currentIndex()]

    @property
    def opacity(self):
        return self.opacity_box.value() if self.opacity_box else self.opacities[self.style_box.currentIndex()]

    def apply_preset_to_edit(self):
        self.edited_color = self.palette[self.style_box.currentIndex()]
        self.opacity_box.setValue(self.opacities[self.style_box.currentIndex()])
        self.update_color_button()

    def choose_color(self):
        color = QColorDialog.getColor(QColor(self.edited_color), self, "Цвет подсветки")
        if color.isValid():
            self.edited_color = color.name()
            self.update_color_button()

    def update_color_button(self):
        self.color_button.setText(f"Цвет группы: {self.edited_color}")
        self.color_button.setStyleSheet(f"background: {self.edited_color}; color: {'#111111' if QColor(self.edited_color).lightness() > 140 else '#ffffff'}")

    def draft(self):
        return {"name": self.name_edit.text().strip(), "expression": self.condition_text(),
                "style": self.style_box.currentIndex(), "color": self.color, "opacity": self.opacity}

    def validate_and_accept(self):
        if not self.name_edit.text().strip():
            self.error.setText("Введите название группы.")
            self.name_edit.setFocus()
            return
        super().validate_and_accept()


class HighlightChip(QFrame):
    editRequested = Signal(object)

    def __init__(self, model, group, parent=None):
        super().__init__(parent)
        self.model = model
        self.group = group
        row = QHBoxLayout(self)
        row.setContentsMargins(3, 0, 3, 0)
        row.setSpacing(3)
        self.enabled_box = QCheckBox()
        self.enabled_box.toggled.connect(self.set_visible)
        row.addWidget(self.enabled_box)
        self.color_button = QPushButton("Цвет")
        self.color_button.clicked.connect(self.choose_color)
        row.addWidget(self.color_button)
        edit = QPushButton("Изменить")
        edit.clicked.connect(lambda: self.editRequested.emit(self.group))
        row.addWidget(edit)
        remove = QPushButton("×")
        remove.setToolTip("Удалить группу подсветки")
        remove.clicked.connect(lambda: self.model.remove(self.group))
        row.addWidget(remove)
        self.sync(group)

    def sync(self, group):
        self.group = group
        self.enabled_box.blockSignals(True)
        self.enabled_box.setText(group.name)
        self.enabled_box.setChecked(group.visible)
        self.enabled_box.blockSignals(False)
        foreground = "#111111" if QColor(group.color).lightness() > 140 else "#ffffff"
        self.color_button.setStyleSheet(f"background: {group.color}; color: {foreground}")
        self.setToolTip(f"{group.expression or '(без условия)'} · {group.intervals.count} участков · {group.opacity} %")

    def set_visible(self, checked):
        self.group.visible = checked
        self.model.changed.emit()

    def choose_color(self):
        color = QColorDialog.getColor(QColor(self.group.color), self, "Цвет подсветки")
        if color.isValid():
            self.group.color = color.name()
            self.model.changed.emit()


class HighlightPanel(QWidget):
    createRequested = Signal()
    editRequested = Signal(object)

    def __init__(self, model, parent=None):
        super().__init__(parent)
        self.model = model
        self._chips = {}
        self.setFixedHeight(40)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(4, 1, 4, 1)
        outer.setSpacing(3)
        self.master_box = QCheckBox()
        self.master_box.setToolTip("Показать или скрыть все подсветки")
        self.master_box.setChecked(model.master_enabled)
        self.master_box.toggled.connect(self.toggle_master)
        outer.addWidget(self.master_box)
        button = QPushButton("+ Подсветка")
        button.clicked.connect(self.createRequested.emit)
        outer.addWidget(button)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.chips = QHBoxLayout(self.content)
        self.chips.setContentsMargins(2, 0, 2, 0)
        self.chips.setSpacing(3)
        self.chips.addStretch(1)
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll, 1)
        model.changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        self.master_box.blockSignals(True)
        self.master_box.setChecked(self.model.master_enabled)
        self.master_box.blockSignals(False)
        current = {group.id for group in self.model.groups}
        for group_id in list(self._chips):
            if group_id not in current:
                chip = self._chips.pop(group_id)
                self.chips.removeWidget(chip)
                chip.deleteLater()
        for index, group in enumerate(self.model.groups):
            chip = self._chips.get(group.id)
            if chip is None:
                chip = HighlightChip(self.model, group, self.content)
                chip.editRequested.connect(self.editRequested.emit)
                self._chips[group.id] = chip
                self.chips.insertWidget(index, chip)
            else:
                chip.sync(group)
        self.content.setMinimumWidth(self.chips.sizeHint().width())
        self.content.updateGeometry()

    def toggle_master(self, checked):
        view = self.parent()
        restore = view.preferences.restore_highlight_visibility if hasattr(view, "preferences") else True
        self.model.set_master_enabled(checked, restore)
