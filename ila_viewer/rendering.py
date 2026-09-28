# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Drawing-only renderer, separate from input handling and tree operations."""
import math
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF


class WaveformRenderer:
    def __init__(self, view):
        self.view = view

    def line(self, painter, x1, y1, x2, y2):
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    def draw_segment(self, painter, sig, code, x1, x2, y, transition=False):
        v = self.view
        if x2 <= x1:
            return
        value = sig.values[int(code)]
        unknown = isinstance(value, str)
        color = QColor(v.themes.current.unknown) if unknown else v.signal_color(sig)
        painter.setPen(QPen(color, 1.2))
        hi, lo = y + 12, y + v.ROW - 12
        if not sig.is_bus and not unknown:
            level = hi if value else lo
            if value and v.preferences.fill_high:
                painter.fillRect(QRectF(x1, hi, x2 - x1, lo - hi + 1),
                                 QColor(color.red(), color.green(), color.blue(), 65))
            self.line(painter, x1, level, x2, level)
            if transition:
                self.line(painter, x1, hi, x1, lo)
            return
        notch = min(4, (x2 - x1) / 2)
        polygon = QPolygonF([QPointF(x1, (hi + lo) / 2), QPointF(x1 + notch, hi),
                             QPointF(x2 - notch, hi), QPointF(x2, (hi + lo) / 2),
                             QPointF(x2 - notch, lo), QPointF(x1 + notch, lo)])
        filled = v.preferences.fill_bus
        painter.setBrush(QColor(color.red(), color.green(), color.blue(), 65) if filled else Qt.BrushStyle.NoBrush)
        painter.drawPolygon(polygon)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if x2 - x1 > 20:
            label = sig.format_value(value, v.display_radix(sig))
            label = painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, int(x2 - x1 - 10))
            painter.drawText(QRectF(x1 + 5, y, x2 - x1 - 10, v.ROW), Qt.AlignmentFlag.AlignCenter, label)

    def draw_signal(self, painter, sig, y):
        v = self.view
        first = sig.run_index(v.left)
        last = min(len(sig.starts), int(np.searchsorted(sig.starts, v.left + v.span, side="left")) + 1)
        if last - first <= v.plot_width * 2:
            for index in range(first, last):
                start = max(v.left, int(sig.starts[index]))
                end = min(v.left + v.span,
                          int(sig.starts[index + 1]) if index + 1 < len(sig.starts) else v.capture.count)
                self.draw_segment(painter, sig, sig.codes[index], v.sample_x(start), v.sample_x(end), y,
                                   index > 0 and sig.starts[index] >= v.left)
        else:
            # Preserve subpixel activity: shade any pixel containing transitions.
            # Work is bounded by viewport width, regardless of capture length.
            edges = np.linspace(v.left, v.left + v.span, v.plot_width + 1)
            indices = np.maximum(0, np.searchsorted(sig.starts, edges, side="right") - 1)
            run_start = 0
            while run_start < v.plot_width:
                busy = indices[run_start + 1] != indices[run_start]
                code = sig.codes[indices[run_start]]
                run_end = run_start + 1
                while run_end < v.plot_width:
                    next_busy = indices[run_end + 1] != indices[run_end]
                    if next_busy != busy or (not busy and sig.codes[indices[run_end]] != code):
                        break
                    run_end += 1
                x1, x2 = v.plot_left + run_start, v.plot_left + run_end
                if busy:
                    color = v.signal_color(sig)
                    painter.fillRect(QRectF(x1, y + 12, x2 - x1, v.ROW - 24),
                                     QColor(color.red(), color.green(), color.blue(), 100))
                    painter.setPen(color)
                    self.line(painter, x1, y + 12, x2, y + 12)
                    self.line(painter, x1, y + v.ROW - 12, x2, y + v.ROW - 12)
                else:
                    self.draw_segment(painter, sig, code, x1, x2, y)
                run_start = run_end

    def paint(self, event):
        v = self.view
        theme = v.themes.current
        background, grid, text, muted = (QColor(getattr(theme, key)) for key in ("background", "grid", "text", "muted"))
        painter = QPainter(v.viewport())
        painter.fillRect(v.viewport().rect(), background)
        painter.setFont(v.font())
        if not v.capture:
            painter.setPen(muted)
            painter.drawText(v.viewport().rect(), Qt.AlignmentFlag.AlignCenter,
                             "Откройте CSV из Vivado ILA\nCtrl+O или перетащите файл в окно")
            return
        w, h, split = v.viewport().width(), v.viewport().height(), v.plot_left
        scroll = v.verticalScrollBar().value()
        first = scroll // v.ROW
        last = min(len(v.visible_signals), first + (h - v.HEADER) // v.ROW + 2)
        painter.save()
        painter.setClipRect(QRectF(0, v.HEADER, w, max(0, h - v.HEADER)))
        for row in range(first, last):
            y = v.HEADER + row * v.ROW - scroll
            if v.rows[row].node.id in v.selection.ids:
                painter.fillRect(QRectF(0, y, w, v.ROW), QColor(theme.selected))
            elif row % 2:
                painter.fillRect(QRectF(0, y, w, v.ROW), QColor(theme.alternate))
            painter.setPen(grid)
            self.line(painter, 0, y + v.ROW, w, y + v.ROW)
        # Tick spacing uses 1/2/5 decades and never goes below one sample.
        ideal = v.span / max(1, v.plot_width / 100)
        decade = 10 ** math.floor(math.log10(max(1, ideal)))
        step = next((v * decade for v in (1, 2, 5, 10) if v * decade >= ideal), 10 * decade)
        ticks = list(range(max(0, math.ceil(v.left / step) * step), math.ceil(v.left + v.span), step))
        painter.setPen(grid)
        for tick in ticks:
            x = v.sample_x(tick)
            self.line(painter, x, v.HEADER, x, h)
        if v.highlight_model and v.highlight_model.master_enabled:
            for group in v.highlight_model.groups:
                if not group.visible:
                    continue
                color = QColor(group.color)
                color.setAlpha(round(255 * group.opacity / 100))
                for start, end in group.intervals.visible(v.left, v.left + v.span, v.plot_width):
                    x1, x2 = v.sample_x(start), v.sample_x(end)
                    painter.fillRect(QRectF(x1, v.HEADER, x2 - x1, h - v.HEADER), color)
        for row in range(first, last):
            item = v.rows[row]
            node, depth = item.node, item.depth
            sig = node.signal
            y = v.HEADER + row * v.ROW - scroll
            painter.save()
            painter.setClipRect(QRectF(split, v.HEADER, v.plot_width, max(0, h - v.HEADER)))
            if sig:
                self.draw_signal(painter, sig, y)
            elif node.kind == "divider":
                painter.setPen(QPen(QColor(v.tree.inherited_color(node) or theme.muted), 1, Qt.PenStyle.DashLine))
                self.line(painter, split, y + v.ROW / 2, w, y + v.ROW / 2)
            painter.restore()
            painter.save()
            painter.setClipRect(QRectF(0, v.HEADER, v.name_width, max(0, h - v.HEADER)))
            painter.setPen(text)
            name = v.row_name(node)
            indent = 10 + depth * v.INDENT
            if depth:
                painter.setPen(grid)
                for ancestor in range(depth):
                    x = 14 + ancestor * v.INDENT
                    self.line(painter, x, y, x, y + v.ROW)
                painter.setPen(text)
            if node.expandable:
                v.icons.arrow(painter, indent, y + v.ROW / 2, node.expanded, muted)
            icon_color = v.signal_color(sig) if sig else QColor(v.tree.inherited_color(node) or theme.muted)
            v.icons.paint(painter, node.kind, indent + 18, y + (v.ROW - 16) / 2, icon_color)
            name_rect = QRectF(indent + 40, y, max(0, v.name_width - indent - 48), v.ROW)
            name = painter.fontMetrics().elidedText(name, Qt.TextElideMode.ElideMiddle, int(name_rect.width()))
            painter.drawText(name_rect, Qt.AlignmentFlag.AlignVCenter, name)
            painter.restore()
            painter.save()
            painter.setClipRect(QRectF(v.name_width, v.HEADER, v.value_width, max(0, h - v.HEADER)))
            painter.setPen(icon_color)
            value = painter.fontMetrics().elidedText(v.row_value(node), Qt.TextElideMode.ElideRight, v.value_width - 20)
            painter.drawText(QRectF(v.name_width + 10, y, v.value_width - 20, v.ROW),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, value)
            painter.restore()
        if v.drop_target:
            target, position = v.drop_target
            drop_row = next((i for i, item in enumerate(v.rows) if item.node is target), None)
            if drop_row is not None:
                y = v.HEADER + drop_row * v.ROW - scroll
                painter.setPen(QPen(QColor(theme.marker_a), 2))
                if position == "inside":
                    painter.drawRect(QRectF(2, y + 2, split - 4, v.ROW - 4))
                else:
                    y += v.ROW if position == "after" else 0
                    self.line(painter, 0, y, split, y)
        painter.restore()
        painter.fillRect(QRectF(0, 0, w, v.HEADER), QColor(theme.panel))
        painter.setPen(muted)
        painter.drawText(QRectF(12, 0, v.name_width - 24, v.HEADER), Qt.AlignmentFlag.AlignVCenter, "Name")
        painter.drawText(QRectF(v.name_width + 10, 0, v.value_width - 20, v.HEADER),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "Value")
        painter.save()
        painter.setClipRect(QRectF(split, 0, v.plot_width, h))
        for tick in ticks:
            x = v.sample_x(tick)
            label = f"{tick * v.period_ns:g} ns" if v.period_ns else str(tick)
            painter.setPen(muted)
            painter.drawText(QRectF(x + 4, 0, 110, v.HEADER), Qt.AlignmentFlag.AlignVCenter, label)
        if not v.period_mode and v.cursor_b is not None:
            xa, xb = sorted((v.sample_x(v.cursor_a), v.sample_x(v.cursor_b)))
            painter.fillRect(QRectF(xa, v.HEADER, xb - xa, h - v.HEADER), QColor(255, 204, 116, 12))
        if v.zoom_band:
            xa, xb = sorted(v.sample_x(s) for s in v.zoom_band)
            color = QColor(theme.bus)
            painter.fillRect(QRectF(xa, v.HEADER, xb - xa, h - v.HEADER),
                             QColor(color.red(), color.green(), color.blue(), 50))
            painter.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
            self.line(painter, xa, v.HEADER, xa, h)
            self.line(painter, xb, v.HEADER, xb, h)
        for label, sample, color in (() if v.period_mode else (("A", v.cursor_a, QColor(theme.marker_a)), ("B", v.cursor_b, QColor(theme.marker_b)))):
            if sample is not None and v.left <= sample <= v.left + v.span:
                x = min(w - 1, v.sample_x(sample))
                painter.setPen(QPen(color, 1.4))
                self.line(painter, x, 0, x, h)
                label_x = min(x + 1, w - 19)
                painter.fillRect(QRectF(label_x, v.HEADER - 18, 18, 18), color)
                painter.setPen(background)
                painter.drawText(QRectF(label_x, v.HEADER - 18, 18, 18), Qt.AlignmentFlag.AlignCenter, label)
        for label, sample, color in (("1", v.period_markers[0], QColor(theme.marker_a)),
                                     ("2", v.period_markers[1], QColor(theme.marker_b))):
            if sample is not None and v.left <= sample <= v.left + v.span:
                x = v.sample_x(sample)
                painter.setPen(QPen(color, 1.2, Qt.PenStyle.DashLine))
                self.line(painter, x, 0, x, h)
                painter.fillRect(QRectF(x + 1, 0, 18, 18), color)
                painter.setPen(background)
                painter.drawText(QRectF(x + 1, 0, 18, 18), Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()
        painter.setPen(grid)
        self.line(painter, v.name_width, 0, v.name_width, h)
        self.line(painter, split, 0, split, h)

