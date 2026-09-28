# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""One-time, per-session sample-phase and period calibration."""
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPushButton, QVBoxLayout


class CopyPeriodDialog(QDialog):
    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self.setWindowTitle("Настройка периода копирования")
        self.setModal(False)
        self.resize(440, 190)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Один раз за запуск отметьте на диаграмме две одинаковые точки соседних "
            "периодов. Разница отсчётов задаст период и фазу выборки."
        ))
        self.marker1 = QPushButton("1. Поставить первую точку")
        self.marker2 = QPushButton("2. Поставить вторую точку")
        self.marker2.setEnabled(False)
        self.marker1.clicked.connect(lambda: self.view.request_period_marker(1))
        self.marker2.clicked.connect(lambda: self.view.request_period_marker(2))
        layout.addWidget(self.marker1)
        layout.addWidget(self.marker2)
        self.status = QLabel("Сначала нажмите первую кнопку, затем щёлкните по диаграмме.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        view.periodMarkerPlaced.connect(self.marker_placed)
        self.finished.connect(self.cleanup)

    def cleanup(self, _result):
        self.view.request_period_marker(None)
        try:
            self.view.periodMarkerPlaced.disconnect(self.marker_placed)
        except RuntimeError:
            pass
        self.deleteLater()

    def marker_placed(self, marker, sample):
        if marker == 1:
            self.marker1.setText(f"1. Первая точка: отсчёт {sample:,} ✓")
            self.marker2.setEnabled(True)
            self.status.setText("Нажмите кнопку 2 и щёлкните в той же фазе следующего периода.")
            return
        first = self.view.period_markers[0]
        if first is None or first == sample:
            self.view.period_markers[1] = None
            self.view.request_period_marker(2)
            self.marker2.setText("2. Поставить вторую точку")
            self.status.setText("Точки должны быть на разных отсчётах. Поставьте вторую точку ещё раз.")
            self.view.viewport().update()
            return
        self.accepted_period = abs(sample - first)
        self.phase = first
        self.marker2.setText(f"2. Вторая точка: отсчёт {sample:,} ✓")
        self.status.setText(f"Период: {self.accepted_period:,} отсчётов.")
        self.accept()
