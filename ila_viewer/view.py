# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Viewport-based waveform drawing and navigation."""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QAbstractScrollArea, QApplication

from .data import Capture
from .tree import SignalTree
from .themes import ThemeManager
from .actions import ContextMenuController
from .sources import UnconfiguredSourceNavigator
from .icons import RowIconPainter
from .rendering import WaveformRenderer
from .preferences import ViewerPreferences
from .selection import RowSelection


class WaveformView(QAbstractScrollArea):
    cursorChanged = Signal()
    selectionChanged = Signal()
    viewChanged = Signal()
    message = Signal(str)
    findRequested = Signal()
    expansionRequested = Signal(object)
    columnsChanged = Signal()
    copyRequested = Signal(str)
    cellCopyRequested = Signal(str)
    highlightRequested = Signal()
    periodMarkerPlaced = Signal(int, int)
    HEADER = 38
    ROW = 48
    SCROLL_STEPS = 1_000_000
    INDENT = 28

    def __init__(self, parent=None, themes=None, menus=None, source_navigator=None, preferences=None):
        super().__init__(parent)
        self.capture: Capture | None = None
        self.tree = SignalTree()
        self.rows = []
        self.filter_text = ""
        self.themes = themes or ThemeManager()
        self.menus = menus or ContextMenuController()
        self.source_navigator = source_navigator or UnconfiguredSourceNavigator()
        self.icons = RowIconPainter()
        self.renderer = WaveformRenderer(self)
        self.highlight_model = None
        self.highlight_panel = None
        self.themes.changed.connect(self.viewport().update)
        self.visible_signals = []
        self.selected = 0
        self.selection = RowSelection()
        self.preferences = preferences or ViewerPreferences()
        self.left = 0.0
        self.span = 100.0
        self.name_width = self.preferences.name_width
        self.value_width = self.preferences.value_width
        self.cursor_a = 0
        self.cursor_b = None
        self.period_markers = [None, None]
        self.period_marker_target = None
        self.period_mode = False
        self.interaction_region = "names"
        self.context_column = None
        self.drag = None
        self.drop_target = None
        self.drag_position = None
        self.zoom_band = None
        self.auto_scroll = QTimer(self)
        self.auto_scroll.setInterval(40)
        self.auto_scroll.timeout.connect(self._drag_scroll)
        self.period_ns = 0.0
        self.full_names = self.preferences.full_names
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.horizontalScrollBar().valueChanged.connect(self._horizontal_scroll)
        self.verticalScrollBar().valueChanged.connect(lambda _: self.viewport().update())
        self.setFont(QFont("Consolas", 10))

    @property
    def plot_left(self):
        return self.name_width + self.value_width

    @property
    def plot_width(self):
        return max(1, self.viewport().width() - self.plot_left)

    @property
    def current_signal(self):
        return self.current_node.signal if self.current_node else None

    @property
    def selected(self):
        return self._selected

    @selected.setter
    def selected(self, index):
        self._selected = index
        if hasattr(self, "selection"):
            self.selection.select(self.rows, index)

    @property
    def current_node(self):
        return self.rows[self.selected].node if 0 <= self.selected < len(self.rows) else None

    @property
    def selected_nodes(self):
        return [row.node for row in self.rows if row.node.id in self.selection.ids]

    @property
    def selected_signal_nodes(self):
        result = []
        def visit(node):
            if node.signal:
                result.append(node)
            elif node.kind == "group":
                for child in node.children:
                    visit(child)
        for node in self.selected_nodes:
            visit(node)
        return list(dict.fromkeys(result))

    def set_capture(self, capture):
        self.auto_scroll.stop()
        self.drag = None
        self.drop_target = None
        self.zoom_band = None
        self.capture = capture
        self.period_markers = [None, None]
        self.period_marker_target = None
        self.period_mode = False
        self.tree = SignalTree(capture.signals, capture.metadata)
        self.filter_text = ""
        self.selected = 0
        self.selection = RowSelection()
        self.cursor_a, self.cursor_b = 0, None
        self.refresh_rows()
        self.verticalScrollBar().setValue(0)
        self.fit()
        self.selectionChanged.emit()
        self.cursorChanged.emit()

    def filter_signals(self, text):
        self.filter_text = text
        self.refresh_rows()

    def refresh_rows(self, selected=None):
        explicit = selected is not None
        selected = selected or self.current_node
        self.rows = self.tree.rows(self.filter_text)
        self.visible_signals = [row.node for row in self.rows]
        self._selected = next((i for i, row in enumerate(self.rows) if row.node is selected), 0)
        self.selection.retain_visible(self.rows)
        if explicit or not self.selection.ids:
            self.selection.select(self.rows, self.selected)
        self.update_scrollbars()
        self.ensure_value_width()
        self.selectionChanged.emit()
        self.viewport().update()

    def display_radix(self, sig=None):
        sig = sig or self.current_signal
        local = sig.options.radix if sig else "DEFAULT"
        return self.preferences.global_radix if local == "DEFAULT" else local

    def row_name(self, node):
        sig = node.signal
        return (sig.name if self.full_names else sig.short_name) if sig and node.kind != "bit" else node.label

    def row_value(self, node):
        sig = node.signal
        if sig:
            return sig.format_value(sig.value_at(self.cursor_a), self.display_radix(sig))
        return str(int(node.values[self.cursor_a])) if node.kind == "parameter" else ""

    def column_at(self, x):
        if abs(x - self.name_width) <= 5:
            return "name"
        if abs(x - self.plot_left) <= 5:
            return "value"
        return None

    def set_column_width(self, column, width):
        if column == "name":
            self.name_width = max(100, min(10000, int(width)))
        else:
            self.value_width = max(60, min(10000, int(width)))
        self.columnsChanged.emit()
        self.viewport().update()

    def auto_fit_column(self, column):
        metrics = self.fontMetrics()
        if column == "name":
            width = max([100] + [60 + row.depth * self.INDENT + metrics.horizontalAdvance(self.row_name(row.node)) for row in self.rows])
        else:
            width = max([70] + [24 + metrics.horizontalAdvance(self.row_value(row.node)) for row in self.rows])
        self.set_column_width(column, width)

    def ensure_value_width(self):
        if not self.capture or not self.rows:
            return
        first = self.verticalScrollBar().value() // self.ROW
        count = max(1, self.viewport().height() // self.ROW + 2)
        visible = self.rows[first:first + count]
        metrics = self.fontMetrics()
        needed = max((metrics.horizontalAdvance(self.row_value(row.node)) + 20 for row in visible), default=0)
        if needed <= self.value_width:
            return
        limit = max(self.value_width, int(self.viewport().width() * 0.45))
        width = self.value_width
        while width < needed and width < limit:
            width = int(width * 1.5) + 1
        self.set_column_width("value", min(width, limit))

    def marker_at(self, x):
        if not self.capture or x < self.plot_left or self.period_mode:
            return None
        hits = [(abs(x - self.sample_x(sample)), name) for name, sample in (("A", self.cursor_a), ("B", self.cursor_b))
                if sample is not None and self.left <= sample <= self.left + self.span]
        return min(hits)[1] if hits and min(hits)[0] <= 6 else None

    def period_marker_at(self, x):
        if not self.period_mode or x < self.plot_left:
            return None
        hits = [(abs(x - self.sample_x(sample)), index + 1)
                for index, sample in enumerate(self.period_markers) if sample is not None]
        nearest = min(hits, key=lambda hit: (hit[0], hit[1] != self.period_marker_target)) if hits else None
        return nearest[1] if nearest and nearest[0] <= 8 else None

    def hover_cursor(self, pos):
        resize = self.column_at(pos.x()) or self.marker_at(pos.x()) or self.period_marker_at(pos.x())
        self.viewport().setCursor(Qt.CursorShape.SizeHorCursor if resize else Qt.CursorShape.ArrowCursor)

    def set_radix(self, radix):
        nodes = self.selected_signal_nodes
        if nodes:
            for node in nodes:
                node.options.radix = radix
            self.ensure_value_width()
            self.selectionChanged.emit()
            self.viewport().update()

    def set_color(self, color):
        if self.selected_nodes:
            for node in self.selected_nodes:
                node.options.color = color
            self.viewport().update()

    def signal_color(self, sig):
        theme = self.themes.current
        kind = "bus" if sig.is_bus or sig.node.kind == "bus" else "bit"
        if not self.preferences.separate_signal_styles:
            kind = "bus"
        saved = self.preferences.signal_colors.get(theme.key, {})
        return QColor(self.tree.inherited_color(sig.node) or saved.get(kind) or (theme.bus if kind == "bus" else theme.bit))

    def reverse_bits(self, checked):
        if self.current_node and self.current_node.kind == "bus":
            self.current_node.options.reverse = bool(checked)
            self.viewport().update()
            self.selectionChanged.emit()

    def toggle_node(self, node):
        if not node or not node.expandable:
            return
        if node.kind == "bus" and not node.children:
            self.expansionRequested.emit(node)
        else:
            node.expanded = not node.expanded
            self.refresh_rows(node)

    def update_scrollbars(self):
        if not hasattr(self, "span"):
            return
        vertical = self.verticalScrollBar()
        vertical.setPageStep(max(1, self.viewport().height() - self.HEADER))
        vertical.setRange(0, max(0, len(self.visible_signals) * self.ROW - vertical.pageStep()))
        horizontal = self.horizontalScrollBar()
        total = self.capture.count if self.capture else self.span
        remaining = max(0, total - self.span)
        horizontal.blockSignals(True)
        horizontal.setRange(0, self.SCROLL_STEPS if remaining > 0 else 0)
        page = self.SCROLL_STEPS * self.span / remaining if remaining else self.SCROLL_STEPS
        horizontal.setPageStep(max(1, min(2_000_000_000, int(page))))
        horizontal.setSingleStep(max(1, horizontal.pageStep() // 20))
        horizontal.setValue(round(self.left / remaining * self.SCROLL_STEPS) if remaining else 0)
        horizontal.blockSignals(False)

    def _horizontal_scroll(self, value):
        if self.capture:
            self.left = value / self.SCROLL_STEPS * max(0, self.capture.count - self.span)
            self.viewport().update()
            self.viewChanged.emit()

    def scrollContentsBy(self, dx, dy):
        self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_scrollbars()
        self.layout_highlight_panel()
        self.columnsChanged.emit()

    def attach_highlight_panel(self, model, panel):
        self.highlight_model = model
        self.highlight_panel = panel
        model.changed.connect(self.layout_highlight_panel)
        model.changed.connect(self.viewport().update)
        self.layout_highlight_panel()

    def layout_highlight_panel(self):
        if self.highlight_panel is None:
            return
        show = bool(self.highlight_model.groups) or not self.preferences.hide_empty_highlight_panel
        height = self.highlight_panel.height() if show else 0
        if self.viewportMargins().bottom() != height:
            self.setViewportMargins(0, 0, 0, height)
        if height:
            geometry = self.viewport().geometry()
            self.highlight_panel.setGeometry(geometry.left(), geometry.bottom() + 1, geometry.width(), height)
            self.highlight_panel.show()
        else:
            self.highlight_panel.hide()

    def set_range(self, left, span):
        if not self.capture:
            return
        self.span = max(min(2, self.capture.count), min(float(self.capture.count), span))
        self.left = max(0, min(float(self.capture.count) - self.span, left))
        self.update_scrollbars()
        self.viewport().update()
        self.viewChanged.emit()

    def fit(self):
        if self.capture:
            self.set_range(0, self.capture.count)

    def zoom(self, factor, anchor=None):
        if anchor is None:
            anchor = self.cursor_a if self.left <= self.cursor_a < self.left + self.span else self.left + self.span / 2
        ratio = (anchor - self.left) / self.span
        total = self.capture.count if self.capture else self.span
        new_span = max(min(2, total), min(total, self.span * factor))
        self.set_range(anchor - new_span * ratio, new_span)

    def zoom_toolbar(self, factor):
        if self.capture and self.period_mode and all(marker is not None for marker in self.period_markers):
            midpoint = sum(self.period_markers) / 2
            new_span = max(min(2, self.capture.count), min(self.capture.count, self.span * factor))
            self.set_range(midpoint - new_span / 2, new_span)
        elif self.capture and self.cursor_b is not None:
            midpoint = (self.cursor_a + self.cursor_b) / 2
            new_span = max(min(2, self.capture.count), min(self.capture.count, self.span * factor))
            self.set_range(midpoint - new_span / 2, new_span)
        else:
            self.zoom(factor)

    def zoom_cursors(self):
        if self.cursor_b is not None:
            start, end = sorted((self.cursor_a, self.cursor_b))
            self.set_range(max(0, start - 1), max(2, end - start + 2))

    def set_cursor(self, sample, secondary=False, reveal=True):
        if not self.capture:
            return
        sample = max(0, min(self.capture.count - 1, int(sample)))
        if secondary:
            self.cursor_b = sample
        else:
            self.cursor_a = sample
        if reveal and not self.left <= sample < self.left + self.span:
            self.set_range(sample - self.span / 2, self.span)
        self.ensure_value_width()
        self.cursorChanged.emit()
        self.viewport().update()

    def next_transition(self, direction):
        sig = self.current_signal
        if sig:
            sample = sig.transition(self.cursor_a, direction)
            if sample is None:
                self.set_cursor(0 if direction < 0 else self.capture.count - 1)
            else:
                self.set_cursor(sample)

    def sample_x(self, sample):
        return self.plot_left + (sample - self.left) / self.span * self.plot_width

    def x_sample(self, x):
        return self.left + (x - self.plot_left) / self.plot_width * self.span

    def nearest_sample(self, x):
        return max(0, min(self.capture.count - 1, math.floor(self.x_sample(x) + 0.5)))

    def paintEvent(self, event):
        self.renderer.paint(event)

    def _select_at(self, y, extend=False, toggle=False, preserve=False):
        row = int((y - self.HEADER + self.verticalScrollBar().value()) // self.ROW)
        if y >= self.HEADER and 0 <= row < len(self.visible_signals):
            self._selected = row
            if not (preserve and self.rows[row].node.id in self.selection.ids):
                self.selection.select(self.rows, row, extend, toggle)
            self.selectionChanged.emit()
            self.viewport().update()

    def mousePressEvent(self, event):
        self.setFocus()
        pos = event.position()
        if event.button() == Qt.MouseButton.LeftButton:
            self.interaction_region = "waveform" if pos.x() >= self.plot_left else "names"
        if (event.button() == Qt.MouseButton.LeftButton and self.period_mode
                and pos.x() >= self.plot_left and self.capture):
            sample = self.nearest_sample(pos.x())
            marker = 2 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            self.period_markers[marker - 1] = sample
            self.period_marker_target = marker
            self.drag = ("period", marker)
            self.periodMarkerPlaced.emit(marker, sample)
            self.viewport().update()
            event.accept()
            return
        column = self.column_at(pos.x())
        if pos.x() >= self.plot_left and self.marker_at(pos.x()) and pos.y() >= self.HEADER:
            column = None
        if column and event.button() == Qt.MouseButton.LeftButton:
            self.drag = ("divider", column)
        elif event.button() == Qt.MouseButton.MiddleButton:
            self.drag = ("pan", pos.x(), self.left)
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.LeftButton:
            in_names = pos.x() < self.plot_left
            extended = in_names and bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            toggled = in_names and bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
            if in_names:
                self._select_at(pos.y(), extended, toggled, preserve=not extended and not toggled)
            if pos.x() >= self.plot_left:
                secondary = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                if not secondary:
                    self._select_at(pos.y(), toggle=bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier))
                marker = self.marker_at(pos.x())
                if not secondary:
                    self.cursor_b = None
                # Empty waveform space starts a zoom gesture; marker lines remain draggable.
                if secondary or marker is not None:
                    self.drag = ("cursor", secondary)
                else:
                    self.drag = ("zoom_pending", pos.x(), self.x_sample(pos.x()))
                self.set_cursor(self.nearest_sample(pos.x()), secondary, False)
            elif self.current_node and self.HEADER <= pos.y() < self.HEADER + len(self.rows) * self.ROW - self.verticalScrollBar().value():
                item = self.rows[self.selected]
                indent = 10 + item.depth * self.INDENT
                if extended or toggled:
                    return
                if item.node.expandable and indent - 4 <= pos.x() <= indent + 14:
                    self.toggle_node(item.node)
                else:
                    self.drag = ("row_pending", tuple(self.selected_nodes), pos)

    def mouseMoveEvent(self, event):
        pos = event.position()
        if self.drag:
            if self.drag[0] == "divider":
                self.set_column_width(self.drag[1], pos.x() if self.drag[1] == "name" else pos.x() - self.name_width)
            elif self.drag[0] == "pan":
                self.set_range(self.drag[2] - (pos.x() - self.drag[1]) / self.plot_width * self.span, self.span)
            elif self.drag[0] == "period":
                sample = self.nearest_sample(pos.x())
                marker = self.drag[1]
                if self.period_markers[marker - 1] != sample:
                    self.period_markers[marker - 1] = sample
                    self.periodMarkerPlaced.emit(marker, sample)
                    self.viewport().update()
            elif self.drag[0] in ("row_pending", "row"):
                if self.drag[0] == "row_pending" and (pos - self.drag[2]).manhattanLength() < QApplication.startDragDistance():
                    return
                self.drag = ("row", self.drag[1])
                self.drag_position = pos
                self._update_drop_target(pos)
                self.auto_scroll.start()
                self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            elif self.drag[0] in ("zoom_pending", "zoom"):
                x = max(self.plot_left, min(self.viewport().width(), pos.x()))
                if self.drag[0] == "zoom_pending" and abs(x - self.drag[1]) < QApplication.startDragDistance():
                    return
                self.drag = ("zoom", self.drag[1], self.drag[2])
                self.zoom_band = (self.drag[2], self.x_sample(x))
                self.viewport().update()
            else:
                self.set_cursor(self.nearest_sample(pos.x()), self.drag[1], False)
            return
        self.hover_cursor(pos)
        row = int((pos.y() - self.HEADER + self.verticalScrollBar().value()) // self.ROW)
        if self.capture and pos.y() >= self.HEADER and 0 <= row < len(self.visible_signals):
            node = self.rows[row].node
            sig = node.signal
            if not sig:
                self.viewport().setToolTip(node.label)
                return
            sample = max(0, min(self.capture.count - 1, math.floor(self.x_sample(pos.x())))) if pos.x() >= self.plot_left else self.cursor_a
            value = sig.format_value(sig.value_at(sample), self.display_radix(sig))
            self.viewport().setToolTip(f"{sig.name}\nШирина: {sig.width} бит · Отсчёт {sample}\n{value} ({self.display_radix(sig)})")

    def mouseReleaseEvent(self, event):
        if self.drag and self.drag[0] == "row_pending":
            self.selection.select(self.rows, self.selected)
            self.selectionChanged.emit()
        if self.drag and self.drag[0] == "row" and self.drop_target:
            nodes = self.drag[1]
            if self.tree.move_many(nodes, *self.drop_target):
                self.refresh_rows()
                self.selection.ids = {n.id for n in nodes}
            else:
                self.message.emit("Этот перенос невозможен: бит должен оставаться внутри своей шины.")
        if self.drag and self.drag[0] == "zoom" and self.zoom_band:
            start, end = sorted(self.zoom_band)
            if end - start >= 2 and end - start < self.span:
                self.set_range(start, end - start)
        self.zoom_band = None
        self.auto_scroll.stop()
        self.drop_target = None
        self.drag = None
        self.hover_cursor(event.position())
        self.viewport().update()

    def mouseDoubleClickEvent(self, event):
        column = self.column_at(event.position().x())
        if column and event.button() == Qt.MouseButton.LeftButton:
            self.drag = None
            self.auto_fit_column(column)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def _update_drop_target(self, pos):
        if not self.rows or pos.x() >= self.plot_left:
            self.drop_target = None
            return
        coordinate = pos.y() - self.HEADER + self.verticalScrollBar().value()
        row = max(0, min(len(self.rows) - 1, int(coordinate // self.ROW)))
        node = self.rows[row].node
        offset = coordinate - row * self.ROW
        position = "inside" if node.kind == "group" and self.ROW * .25 <= offset <= self.ROW * .75 else ("before" if offset < self.ROW / 2 else "after")
        self.drop_target = (node, position)
        self.viewport().update()

    def _drag_scroll(self):
        if not self.drag or self.drag[0] != "row" or self.drag_position is None:
            self.auto_scroll.stop()
            return
        y = self.drag_position.y()
        step = -12 if y < self.HEADER + 22 else 12 if y > self.viewport().height() - 22 else 0
        if step:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() + step)
            self._update_drop_target(self.drag_position)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(0.8 ** (delta / 120), self.x_sample(max(self.plot_left, event.position().x())))
        elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier or event.angleDelta().x():
            self.set_range(self.left - delta / 120 * self.span * 0.12, self.span)
        else:
            bar = self.verticalScrollBar()
            pixels = event.pixelDelta().y() if not event.pixelDelta().isNull() else delta / 120 * self.ROW * 2
            bar.setValue(bar.value() - round(pixels))
        event.accept()

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_A and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if self.interaction_region == "waveform":
                if self.capture and not self.period_mode:
                    self.cursor_a = 0
                    # B marks the exclusive boundary just after the final sample.
                    self.cursor_b = self.capture.count
                    self.cursorChanged.emit()
                    self.viewport().update()
            elif self.rows:
                self.selection.ids = {row.node.id for row in self.rows if row.node.signal or row.node.kind == "group"}
                self.selection.anchor = self.rows[0].node.id
                self.selectionChanged.emit()
                self.viewport().update()
            event.accept()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            direction = -1 if key == Qt.Key.Key_Left else 1
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.set_cursor(self.cursor_a + direction)
            elif event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.set_range(self.left + direction * self.span * 0.1, self.span)
            else:
                self.next_transition(direction)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and self.visible_signals:
            self._selected = max(0, min(len(self.visible_signals) - 1, self.selected + (-1 if key == Qt.Key.Key_Up else 1)))
            self.selection.select(self.rows, self.selected, bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            top = self.selected * self.ROW
            bar = self.verticalScrollBar()
            if top < bar.value():
                bar.setValue(top)
            elif top + self.ROW > bar.value() + bar.pageStep():
                bar.setValue(top + self.ROW - bar.pageStep())
            self.selectionChanged.emit()
            self.viewport().update()
        elif key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self.zoom(0.5)
        elif key == Qt.Key.Key_Minus:
            self.zoom(2)
        elif key == Qt.Key.Key_Home:
            self.set_cursor(0)
        elif key == Qt.Key.Key_End and self.capture:
            self.set_cursor(self.capture.count - 1)
        elif key == Qt.Key.Key_F:
            self.fit()
        elif key == Qt.Key.Key_Escape:
            self.auto_scroll.stop()
            self.drag = None
            self.drop_target = None
            self.zoom_band = None
            if not self.period_mode:
                self.cursor_b = None
            self.cursorChanged.emit()
            self.viewport().update()
        else:
            super().keyPressEvent(event)

    def contextMenuEvent(self, event):
        # A right click selects a new row, or preserves a multi-selection when
        # the clicked row already belongs to it, regardless of the clicked column.
        self._select_at(event.pos().y(), preserve=True)
        self.context_column = ("name" if event.pos().x() < self.name_width else
                               "value" if event.pos().x() < self.plot_left else None)
        menu = self.menus.build(self)
        menu.exec(event.globalPos())
        menu.deleteLater()

    def request_period_marker(self, marker):
        self.period_marker_target = marker if marker in (1, 2) else None
        self.viewport().update()

    def begin_period_setup(self):
        if not self.capture or self.capture.count < 2:
            return False
        first = max(0, min(self.capture.count - 2, math.floor(self.left + self.span / 3)))
        second = max(first + 1, min(self.capture.count - 1, math.floor(self.left + 2 * self.span / 3)))
        self.period_markers = [first, second]
        self.period_mode = True
        self.period_marker_target = 1
        self.viewport().update()
        return True

    def end_period_setup(self):
        self.period_mode = False
        self.period_marker_target = None
        self.period_markers = [None, None]
        self.drag = None
        self.viewport().update()
