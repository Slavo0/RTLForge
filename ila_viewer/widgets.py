# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Small reusable controls: segmented choice and keycaps.

``SegmentedChoice`` replaces a combo box when there are two to four short options
that should all be visible at once (edge, sampling moment, mode). It mirrors the
part of the ``QComboBox`` API the dialogs use, so callers stay unchanged.
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget


class SegmentedChoice(QWidget):
    currentIndexChanged = Signal(int)

    def __init__(self, items, current=None, parent=None):
        """``items``: (data, text) or (data, text, tooltip)."""
        super().__init__(parent)
        self.setObjectName("plain")
        # Segments hug their labels; a form row must not stretch them across the dialog.
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self._data = []
        for index, item in enumerate(items):
            data, text, *tip = item
            button = QPushButton(text)
            button.setCheckable(True)
            button.setAutoDefault(False)
            button.setProperty("segment", True)
            button.setProperty("first", index == 0)
            button.setProperty("last", index == len(items) - 1)
            if tip:
                button.setToolTip(tip[0])
            self.group.addButton(button, index)
            layout.addWidget(button)
            self._data.append(data)
        self.group.idToggled.connect(self._toggled)
        self.setCurrentIndex(max(0, self.findData(current)) if current is not None else 0)

    def _toggled(self, index, checked):
        if checked:
            self.currentIndexChanged.emit(index)

    def buttons(self):
        return [self.group.button(i) for i in range(len(self._data))]

    def count(self):
        return len(self._data)

    def findData(self, data):
        return next((i for i, value in enumerate(self._data) if value == data), -1)

    def currentIndex(self):
        return self.group.checkedId()

    def currentData(self):
        index = self.currentIndex()
        return self._data[index] if index >= 0 else None

    def setCurrentIndex(self, index):
        if 0 <= index < len(self._data):
            self.group.button(index).setChecked(True)

    def setCurrentData(self, data):
        self.setCurrentIndex(self.findData(data))

    def setItemEnabled(self, index, enabled):
        self.group.button(index).setEnabled(enabled)


def keycap(text):
    """A keyboard or mouse gesture drawn as a small key (styled by the theme)."""
    label = QLabel(text)
    label.setProperty("keycap", True)
    return label


def muted(text):
    label = QLabel(text)
    label.setProperty("role", "muted")
    return label


def plain_container(parent=None):
    """A layout-only widget that shows its parent's background."""
    widget = QWidget(parent)
    widget.setObjectName("plain")
    return widget
