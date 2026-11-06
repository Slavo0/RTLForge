# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Inline controls for choosing the sampling clock: markers 1/2 or a clock signal's edges.

Layout: one row of choices (source, signal, edge, sampling moment, actions) and one
readout row that shows what the choice gives. Gestures for markers are keycaps, not
instructions; everything the user must decide is a visible control.
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QVBoxLayout)

from .measurements import format_frequency
from .sampling import EdgeSampling, clock_candidates, clock_score
from .timebase import format_seconds, group_digits, parse_clock
from .widgets import SegmentedChoice, keycap, muted, plain_container


class CopyPeriodBanner(QFrame):
    confirmed = Signal(int, int)        # period and phase from markers 1/2
    clockConfirmed = Signal(object)     # EdgeSampling on a clock signal
    cancelled = Signal()

    MARKERS = "markers"
    SIGNAL = "signal"

    def __init__(self, view, parent=None, signals=(), current=None, edge="rising",
                 before_edge=True, sample_period_ns=0.0):
        super().__init__(parent)
        self.view = view
        self.setObjectName("copyPeriodBanner")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.headline = QLabel("Такт выборки")
        self.headline.setObjectName("bannerTitle")
        row.addWidget(self.headline)
        row.addSpacing(8)

        # The combo keeps every source (tests and callers use it); the markers entry is
        # chosen with the segmented switch, so it is hidden from the drop-down list.
        self.source_box = QComboBox()
        self.source_box.addItem("Маркеры 1 и 2", self.MARKERS)
        for signal in clock_candidates(signals):
            label = signal.short_name + ("  — похоже на такт" if clock_score(signal) else "")
            self.source_box.addItem(label, signal)
            self.source_box.setItemData(self.source_box.count() - 1, signal.name, 3)  # tooltip role
        self.source_box.view().setRowHidden(0, True)
        self.source_box.setMinimumContentsLength(18)
        has_clock = self.source_box.count() > 1
        self.mode_box = SegmentedChoice([
            (self.SIGNAL, "По сигналу такта", "Выборка на фронтах выбранного однобитного сигнала"),
            (self.MARKERS, "По маркерам 1–2", "Период — расстояние между точками 1 и 2 на диаграмме")])
        self.mode_box.setItemEnabled(0, has_clock)
        row.addWidget(self.mode_box)
        row.addSpacing(12)

        # Clock-signal controls.
        self.signal_controls = plain_container()
        signal_row = QHBoxLayout(self.signal_controls)
        signal_row.setContentsMargins(0, 0, 0, 0)
        signal_row.setSpacing(8)
        signal_row.addWidget(self.source_box)
        signal_row.addSpacing(8)
        signal_row.addWidget(muted("Фронт"))
        self.edge_box = SegmentedChoice([
            ("rising", "↑", "Передний фронт"), ("falling", "↓", "Задний фронт"), ("both", "↕", "Оба фронта")], edge)
        signal_row.addWidget(self.edge_box)
        signal_row.addSpacing(8)
        signal_row.addWidget(muted("Значение"))
        self.timing_box = SegmentedChoice([
            (True, "до фронта", "Значения, которые видит регистр в момент фронта. В симуляции регистры "
                                "меняются в тот же момент времени, поэтому «на фронте» значение уже новое."),
            (False, "на фронте", "Значения в момент фронта")], before_edge)
        signal_row.addWidget(self.timing_box)
        row.addWidget(self.signal_controls)

        # Marker controls: the two gestures as keycaps.
        self.marker_controls = plain_container()
        marker_row = QHBoxLayout(self.marker_controls)
        marker_row.setContentsMargins(0, 0, 0, 0)
        marker_row.setSpacing(6)
        marker_row.addWidget(keycap("ЛКМ"))
        marker_row.addWidget(QLabel("точка 1"))
        marker_row.addSpacing(12)
        marker_row.addWidget(keycap("Shift + ЛКМ"))
        marker_row.addWidget(QLabel("точка 2"))
        marker_row.addSpacing(12)
        marker_row.addWidget(muted("линии можно перетаскивать"))
        self.marker_controls.setToolTip("Точка 2 — конец полного периода, начиная от точки 1. "
                                        "Ctrl+колесо меняет масштаб.")
        row.addWidget(self.marker_controls)

        row.addStretch(1)
        cancel_button = QPushButton("Отмена")
        cancel_button.setAutoDefault(False)
        cancel_button.clicked.connect(self.cancelled)
        row.addWidget(cancel_button)
        self.confirm_button = QPushButton("Применить")
        self.confirm_button.setProperty("primary", True)
        self.confirm_button.clicked.connect(self.confirm)
        row.addWidget(self.confirm_button)
        layout.addLayout(row)

        # Readout: what the current choice gives, plus the optional clock frequency for CSV.
        readout = QHBoxLayout()
        readout.setSpacing(8)
        self.status = QLabel()
        self.status.setProperty("role", "readout")
        self.status.setWordWrap(True)
        readout.addWidget(self.status, 1)
        self.time_label = muted("Частота такта")
        readout.addWidget(self.time_label)
        self.clock_edit = QLineEdit()
        self.clock_edit.setPlaceholderText("100 MHz")
        self.clock_edit.setToolTip("Необязательно. Частота или период такта (100 MHz, 10 ns) задаёт шкалу "
                                   "времени CSV: длительность одного отсчёта ILA.")
        self.clock_edit.setFixedWidth(self.clock_edit.fontMetrics().horizontalAdvance("0" * 14) + 16)
        self.clock_edit.textChanged.connect(self.update_status)
        readout.addWidget(self.clock_edit)
        layout.addLayout(readout)
        self.has_time = view.capture is not None and view.capture.times is not None
        self.previous_sample_period = sample_period_ns
        for widget in (self.time_label, self.clock_edit):
            widget.setVisible(not self.has_time)

        self._sampling = None
        if isinstance(current, EdgeSampling):
            index = next((i for i in range(1, self.source_box.count())
                          if self.source_box.itemData(i).name == current.signal.name), 0)
            self.source_box.setCurrentIndex(index)
        elif current is None and has_clock and clock_score(self.source_box.itemData(1)):
            # Simulator dumps usually contain a clock: offer it instead of manual markers.
            self.source_box.setCurrentIndex(1)
        self._last_signal = max(1, self.source_box.currentIndex()) if has_clock else 0
        self.mode_box.setCurrentData(self.MARKERS if self.uses_markers else self.SIGNAL)
        self.mode_box.currentIndexChanged.connect(self.mode_changed)
        self.source_box.currentIndexChanged.connect(self.source_changed)
        self.edge_box.currentIndexChanged.connect(self.source_changed)
        self.timing_box.currentIndexChanged.connect(self.update_status)
        view.periodMarkerPlaced.connect(self.update_status)
        self.source_changed()

    @property
    def uses_markers(self):
        return self.source_box.currentData() == self.MARKERS

    def mode_changed(self, *_):
        """The segmented switch picks markers or the last chosen clock signal."""
        if self.mode_box.currentData() == self.MARKERS:
            self.source_box.setCurrentIndex(0)
        elif self.source_box.count() > 1:
            self.source_box.setCurrentIndex(self._last_signal)

    def sampling(self):
        if self.uses_markers:
            return None
        return EdgeSampling(self.source_box.currentData(), self.edge_box.currentData(),
                            self.timing_box.currentData())

    def source_changed(self, *_):
        markers = self.uses_markers
        if not markers:
            self._last_signal = self.source_box.currentIndex()
        if self.mode_box.currentData() != (self.MARKERS if markers else self.SIGNAL):
            self.mode_box.blockSignals(True)
            self.mode_box.setCurrentData(self.MARKERS if markers else self.SIGNAL)
            self.mode_box.blockSignals(False)
        self.signal_controls.setVisible(not markers)
        self.marker_controls.setVisible(markers)
        self.time_label.setText("Длительность 1→2" if markers else "Частота такта")
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

    def _set_status(self, text, error=False):
        self.status.setText(text)
        self.status.setProperty("role", "error" if error else "readout")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def update_status(self, *_):
        valid_clock = True
        try:
            sample_period = self.sample_period_ns()
        except ValueError as exc:
            sample_period, valid_clock = None, False
            clock_note = f"  ·  частота: {exc}"
        else:
            clock_note = f"  ·  1 отсчёт = {format_seconds(sample_period * 1e-9)}" if sample_period else ""
        if self.uses_markers:
            first, second = self.view.period_markers
            if first is None or second is None or first == second:
                self._set_status("Поставьте точки 1 и 2 на диаграмме")
                self.confirm_button.setEnabled(False)
                return
            period = abs(second - first)
            self._set_status(f"1: {first:,}  →  2: {second:,}  ·  период {period:,} отсч.{clock_note}",
                             not valid_clock)
            self.confirm_button.setEnabled(valid_clock)
            return
        sampling = self._sampling
        edges = sampling.count_edges() if sampling else 0
        if not edges:
            self._set_status("У этого сигнала нет выбранных фронтов", True)
            self.confirm_button.setEnabled(False)
            return
        typical = sampling.typical_interval()
        parts = [f"фронтов {edges:,}"]
        if typical:
            parts.append(f"шаг {group_digits(typical, 2)} отсч.")
            axis = self.view.time_axis()
            if axis is not None and self.view.capture.times is not None:
                rising = EdgeSampling(sampling.signal, "rising").typical_interval()
                start = self.view.period_markers[0] or 0
                if rising:
                    ticks = abs(axis.to_ticks(start + rising) - axis.to_ticks(start))
                    seconds = ticks * axis.tick_seconds
                    if seconds > 0:
                        parts.append(f"T ≈ {format_seconds(seconds, axis.resolution)}  ·  f ≈ {format_frequency(1 / seconds)}")
        self._set_status("  ·  ".join(parts) + clock_note, not valid_clock)
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
