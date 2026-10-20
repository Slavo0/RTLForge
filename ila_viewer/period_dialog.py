# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Inline controls for choosing the sampling clock: markers 1/2 or a clock signal's edges."""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout)

from .measurements import format_frequency
from .sampling import EdgeSampling, clock_candidates, clock_score
from .timebase import format_seconds, group_digits, parse_clock


class CopyPeriodBanner(QFrame):
    confirmed = Signal(int, int)        # period and phase from markers 1/2
    clockConfirmed = Signal(object)     # EdgeSampling on a clock signal
    cancelled = Signal()

    MARKERS = "markers"

    def __init__(self, view, parent=None, signals=(), current=None, edge="rising",
                 before_edge=True, sample_period_ns=0.0):
        super().__init__(parent)
        self.view = view
        self.setObjectName("copyPeriodBanner")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.headline = QLabel("● ВЫБОР ТАКТА ВЫБОРКИ")
        self.headline.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(self.headline)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Такт:"))
        self.source_box = QComboBox()
        self.source_box.addItem("Маркеры 1 и 2 (период в отсчётах)", self.MARKERS)
        for signal in clock_candidates(signals):
            label = signal.short_name + ("  (похоже на такт)" if clock_score(signal) else "")
            self.source_box.addItem(label, signal)
            self.source_box.setItemData(self.source_box.count() - 1, signal.name, 3)  # tooltip role
        source_row.addWidget(self.source_box, 1)
        source_row.addWidget(QLabel("Фронт:"))
        self.edge_box = QComboBox()
        for key, label in (("rising", "↑ передний"), ("falling", "↓ задний"), ("both", "↕ оба")):
            self.edge_box.addItem(label, key)
        self.edge_box.setCurrentIndex(max(0, self.edge_box.findData(edge)))
        source_row.addWidget(self.edge_box)
        source_row.addWidget(QLabel("Значения:"))
        self.timing_box = QComboBox()
        self.timing_box.addItem("до фронта (как регистр)", True)
        self.timing_box.addItem("в момент фронта", False)
        self.timing_box.setCurrentIndex(0 if before_edge else 1)
        self.timing_box.setToolTip("До фронта — значения, которые видит триггер в момент фронта. "
                                   "В симуляции регистры меняются в тот же момент времени, поэтому "
                                   "значение «в момент фронта» уже новое.")
        source_row.addWidget(self.timing_box)
        layout.addLayout(source_row)

        self.instruction = QLabel("На диаграмме: ЛКМ — точка 1 (начало), Shift+ЛКМ — точка 2 (конец полного периода). "
                                  "Линии можно перетаскивать. Ctrl+колесо меняет масштаб.")
        self.instruction.setWordWrap(True)
        layout.addWidget(self.instruction)

        self.time_row = QHBoxLayout()
        self.time_label = QLabel("Частота такта:")
        self.time_row.addWidget(self.time_label)
        self.clock_edit = QLineEdit()
        self.clock_edit.setPlaceholderText("необязательно: 100 MHz или 10 ns — задаёт шкалу времени")
        self.clock_edit.textChanged.connect(self.update_status)
        self.time_row.addWidget(self.clock_edit, 1)
        layout.addLayout(self.time_row)
        self.has_time = view.capture is not None and view.capture.times is not None
        self.previous_sample_period = sample_period_ns
        for widget in (self.time_label, self.clock_edit):
            widget.setVisible(not self.has_time)

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

        self._sampling = None
        if isinstance(current, EdgeSampling):
            index = next((i for i in range(1, self.source_box.count())
                          if self.source_box.itemData(i).name == current.signal.name), 0)
            self.source_box.setCurrentIndex(index)
        elif current is None and self.source_box.count() > 1 and clock_score(self.source_box.itemData(1)):
            # Simulator dumps usually contain a clock: offer it instead of manual markers.
            self.source_box.setCurrentIndex(1)
        self.source_box.currentIndexChanged.connect(self.source_changed)
        self.edge_box.currentIndexChanged.connect(self.source_changed)
        self.timing_box.currentIndexChanged.connect(self.update_status)
        view.periodMarkerPlaced.connect(self.update_status)
        self.source_changed()

    @property
    def uses_markers(self):
        return self.source_box.currentData() == self.MARKERS

    def sampling(self):
        if self.uses_markers:
            return None
        return EdgeSampling(self.source_box.currentData(), self.edge_box.currentData(),
                            self.timing_box.currentData())

    def source_changed(self, *_):
        markers = self.uses_markers
        self.instruction.setVisible(markers)
        self.edge_box.setEnabled(not markers)
        self.timing_box.setEnabled(not markers)
        self.confirm_button.setText("Подтвердить период" if markers else "Подтвердить такт")
        self.time_label.setText("Длительность периода 1→2:" if markers else "Частота этого такта:")
        self._sampling = self.sampling()
        if self._sampling is not None:
            self._show_edges(self._sampling)
        self.update_status()

    def _show_edges(self, sampling):
        """Put markers 1/2 on two visible clock edges so the choice can be checked by eye."""
        left = max(0, int(self.view.left))
        found = []
        for chunk in sampling.edges(left, self.view.capture.count):
            found.extend(int(edge) for edge in chunk[:2 - len(found)])
            if len(found) == 2:
                break
        if found:
            self.view.period_markers = (found + [None])[:2]
            self.view.viewport().update()

    def _interval(self):
        """Samples per full clock period, for deriving the ILA sample period."""
        if self.uses_markers:
            first, second = self.view.period_markers
            return abs(second - first) if first is not None and second is not None else None
        rising = EdgeSampling(self.source_box.currentData(), "rising")
        return rising.typical_interval()

    def sample_period_ns(self):
        """ILA sample period derived from the entered clock, None if not entered or not needed."""
        text = self.clock_edit.text().strip()
        if self.has_time or not text:
            return None
        interval = self._interval()
        if not interval:
            return None
        return parse_clock(text) / interval

    def update_status(self, *_):
        valid_clock = True
        try:
            sample_period = self.sample_period_ns()
        except ValueError as exc:
            sample_period, valid_clock = None, False
            clock_note = f" ⚠ {exc}"
        else:
            clock_note = (f" · 1 отсчёт = {format_seconds(sample_period * 1e-9)}" if sample_period else "")
        if self.uses_markers:
            first, second = self.view.period_markers
            if first is None or second is None or first == second:
                self.status.setText("Пока не выбраны две разные точки. Нажмите ЛКМ для точки 1 и Shift+ЛКМ для точки 2.")
                self.confirm_button.setEnabled(False)
                return
            period = abs(second - first)
            self.status.setText(f"Точка 1: {first:,}  →  точка 2: {second:,}  ·  ПОЛНЫЙ ПЕРИОД: {period:,} отсчётов."
                                f"{clock_note} После проверки нажмите «Подтвердить период».")
            self.confirm_button.setEnabled(valid_clock)
            return
        sampling = self._sampling
        edges = sampling.count_edges() if sampling else 0
        if not edges:
            self.status.setText("У этого сигнала нет выбранных фронтов в захвате.")
            self.confirm_button.setEnabled(False)
            return
        typical = sampling.typical_interval()
        parts = [f"Фронтов: {edges:,}"]
        if typical:
            parts.append(f"типичный шаг: {group_digits(typical, 2)} отсч.")
            axis = self.view.time_axis()
            if axis is not None and self.view.capture.times is not None:
                rising = EdgeSampling(sampling.signal, "rising").typical_interval()
                start = self.view.period_markers[0] or 0
                if rising:
                    ticks = abs(axis.to_ticks(start + rising) - axis.to_ticks(start))
                    seconds = ticks * axis.tick_seconds
                    if seconds > 0:
                        parts.append(f"T ≈ {format_seconds(seconds, axis.resolution)} · f ≈ {format_frequency(1 / seconds)}")
        self.status.setText(" · ".join(parts) + clock_note +
                            ". Неактивный такт (как SCK в SPI) учитывается: выборка только на реальных фронтах.")
        self.confirm_button.setEnabled(valid_clock)

    def confirm(self):
        if not self.confirm_button.isEnabled():
            return
        if self.uses_markers:
            first, second = self.view.period_markers
            if first is not None and second is not None and first != second:
                self.confirmed.emit(abs(second - first), first)
        elif self._sampling is not None:
            self.clockConfirmed.emit(self._sampling)

    def detach(self):
        try:
            self.view.periodMarkerPlaced.disconnect(self.update_status)
        except RuntimeError:
            pass
