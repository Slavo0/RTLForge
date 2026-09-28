# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Inline controls for calibrating the copy period on the waveform."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


class CopyPeriodBanner(QFrame):
    confirmed = Signal(int, int)
    cancelled = Signal()

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self.setObjectName("copyPeriodBanner")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.headline = QLabel("● РЕЖИМ ВЫБОРА ПЕРИОДА — шаг 1 из 2")
        self.headline.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(self.headline)
        instruction = QLabel("На диаграмме: ЛКМ — точка 1 (начало), Shift+ЛКМ — точка 2 (конец полного периода). Линии можно перетаскивать. Ctrl+колесо меняет масштаб.")
        instruction.setWordWrap(True)
        layout.addWidget(instruction)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Масштаб диаграммы:"))
        zoom_in = QPushButton("+")
        zoom_in.clicked.connect(lambda: view.zoom_toolbar(0.5))
        controls.addWidget(zoom_in)
        zoom_out = QPushButton("−")
        zoom_out.clicked.connect(lambda: view.zoom_toolbar(2))
        controls.addWidget(zoom_out)
        controls.addStretch(1)
        self.confirm_button = QPushButton("Подтвердить период")
        self.confirm_button.clicked.connect(self.confirm)
        controls.addWidget(self.confirm_button)
        cancel_button = QPushButton("Отмена")
        cancel_button.clicked.connect(self.cancelled)
        controls.addWidget(cancel_button)
        layout.addLayout(controls)
        view.periodMarkerPlaced.connect(self.update_status)
        self.update_status()

    def update_status(self, *_):
        first, second = self.view.period_markers
        if first is None or second is None or first == second:
            self.status.setText("Пока не выбраны две разные точки. Нажмите ЛКМ для точки 1 и Shift+ЛКМ для точки 2.")
            self.confirm_button.setEnabled(False)
            return
        period = abs(second - first)
        self.status.setText(
            f"Точка 1: {first:,}  →  точка 2: {second:,}  ·  ПОЛНЫЙ ПЕРИОД: {period:,} отсчётов. "
            "После проверки нажмите «Подтвердить период»."
        )
        self.confirm_button.setEnabled(True)

    def confirm(self):
        first, second = self.view.period_markers
        if first is not None and second is not None and first != second:
            self.confirmed.emit(abs(second - first), first)

    def detach(self):
        try:
            self.view.periodMarkerPlaced.disconnect(self.update_status)
        except RuntimeError:
            pass
