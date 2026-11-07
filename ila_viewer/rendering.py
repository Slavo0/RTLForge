# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Drawing-only renderer, separate from input handling and tree operations."""
import math
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF

from .timebase import sample_axis_ruler
from .spi import ADDR, CMD, DATA, DUMMY, MISO, MOSI, READ, RESET, UNKNOWN, WRITE, bubble_label

# Theme roles for decoded protocol fields.
FIELD_ROLES = {CMD: "marker_a", ADDR: "marker_b", DUMMY: "muted", READ: "bit", WRITE: "bus",
               MOSI: "bus", MISO: "bit", DATA: "bus", RESET: "muted"}


class WaveformRenderer:
    @staticmethod
    def _time_edges(v):
        origin = int(v.capture.times[max(0, min(v.capture.count - 1, int(v.left)))])
        offsets = np.linspace(v.capture.time_offset_at(v.left, origin),
                              v.capture.time_offset_at(v.left + v.span, origin), v.plot_width + 1)
        return v.capture.samples_at_offsets(offsets, origin)

    def __init__(self, view):
        self.view = view

    def pixel_edges(self):
        """Sample positions of the plot's pixel boundaries, following the real time axis."""
        v = self.view
        if v.capture.times is not None:
            return self._time_edges(v)
        return np.linspace(v.left, v.left + v.span, v.plot_width + 1)

    def draw_track(self, painter, track, y):
        """A decoder row: one filled, labelled box per field, a shaded band when zoomed out."""
        v = self.view
        first, stop = track.window(v.left, v.left + v.span)
        if stop - first > max(8, v.plot_width // 3):
            self.draw_busy(painter, track.busy(self.pixel_edges()), y + 12, y + v.ROW - 12)
            return
        self.draw_boxes(painter, track.result, track.fields[first:stop], y + 10, y + v.ROW - 10)

    def draw_busy(self, painter, spans, top, bottom):
        v = self.view
        color = QColor(v.themes.current.bus)
        color.setAlpha(100)
        for a, b in spans:
            x1, x2 = v.sample_x(a), v.sample_x(b)
            painter.fillRect(QRectF(x1, top, max(1.0, x2 - x1), bottom - top), color)

    @staticmethod
    def ink(fill):
        """Dark or light text, whichever reads better on ``fill``."""
        return QColor("#14161a") if fill.lightnessF() > 0.5 else QColor("#ffffff")

    def draw_boxes(self, painter, result, fields, top, bottom):
        """Decoded fields: rounded filled boxes with ``0x45`` centered."""
        v = self.view
        theme = v.themes.current
        left, right = v.left, v.left + v.span
        metrics = painter.fontMetrics()
        height = bottom - top
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for record in fields:
            kind, flags = int(record["kind"]), int(record["flags"])
            fill = QColor(theme.unknown if flags & UNKNOWN else getattr(theme, FIELD_ROLES.get(kind, "bus")))
            x1 = v.sample_x(max(left, float(record["start"])))
            x2 = v.sample_x(min(right, float(record["end"])))
            if x2 - x1 < 1:
                x2 = x1 + 1
            # A one-pixel gap keeps neighbouring bytes apart, like separate packets.
            rect = QRectF(x1 + 0.5, top, max(1.0, x2 - x1 - 1.5), height)
            outline = QColor(fill)
            body = QColor(fill)
            body.setAlpha(70 if kind == DUMMY else 215)
            painter.setPen(QPen(outline, 1))
            painter.setBrush(body)
            radius = min(3.0, rect.width() / 2, height / 2)
            painter.drawRoundedRect(rect, radius, radius)
            room = int(rect.width() - 6)
            if room > 8:
                label = bubble_label(result, record)
                if metrics.horizontalAdvance(label) > room:
                    label = bubble_label(result, record, short=True)
                if metrics.horizontalAdvance(label) > room and label.startswith("0x"):
                    label = label[2:]
                label = metrics.elidedText(label, Qt.TextElideMode.ElideRight, room)
                painter.setPen(QColor(theme.text) if kind == DUMMY else self.ink(fill))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
        painter.restore()

    def draw_overlays(self, painter, annotations, sig, y):
        """Decoded bytes above the waveform and sampling arrows on the clock."""
        v = self.view
        left, right = v.left, v.left + v.span
        for annotation in annotations:
            if annotation.kind == "fields":
                top, bottom = y + 3, y + 20
                first, stop = annotation.window(left, right)
                if stop - first > max(8, v.plot_width // 3):
                    self.draw_busy(painter, annotation.busy(self.pixel_edges()), top, bottom)
                else:
                    self.draw_boxes(painter, annotation.result, annotation.visible(first, stop), top, bottom)
            elif annotation.kind == "edges":
                positions = annotation.visible(left, right, max(1, v.plot_width // 6))
                if positions is None or not len(positions):
                    continue
                xs = [v.sample_x(float(sample)) for sample in positions]
                if len(xs) > 1 and min(b - a for a, b in zip(xs, xs[1:])) < 10:
                    continue  # arrows would merge into a strip; zoom in to see them
                hi, lo = self.band(sig, y)
                middle = (hi + lo) / 2
                color = QColor(v.themes.current.text)
                color.setAlpha(200)
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(color)
                direction = -1 if annotation.rising else 1
                for x in xs:
                    tip = middle + direction * 4
                    base = middle - direction * 4
                    painter.drawPolygon(QPolygonF([QPointF(x, tip), QPointF(x - 4, base), QPointF(x + 4, base)]))
                painter.restore()

    def band(self, sig, y):
        """Waveform top/bottom of a row; rows with decoded bytes leave room above for them."""
        v = self.view
        if any(a.kind == "fields" for a in v.annotations.for_signal(sig)):
            return y + 23, y + v.ROW - 5
        return y + 12, y + v.ROW - 12

    def line(self, painter, x1, y1, x2, y2):
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    def draw_segment(self, painter, sig, code, x1, x2, y, transition=False, band=None):
        v = self.view
        if x2 <= x1:
            return
        value = sig.values[int(code)]
        unknown = isinstance(value, str)
        color = QColor(v.themes.current.unknown) if unknown else v.signal_color(sig)
        painter.setPen(QPen(color, 1.2))
        hi, lo = band or self.band(sig, y)
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
            painter.drawText(QRectF(x1 + 5, hi - 12, x2 - x1 - 10, lo - hi + 24), Qt.AlignmentFlag.AlignCenter, label)

    def draw_signal(self, painter, sig, y):
        v = self.view
        hi, lo = self.band(sig, y)
        first = sig.run_index(v.left)
        last = min(len(sig.starts), int(np.searchsorted(sig.starts, v.left + v.span, side="left")) + 1)
        if last - first <= v.plot_width * 2:
            for index in range(first, last):
                start = max(v.left, int(sig.starts[index]))
                end = min(v.left + v.span,
                          int(sig.starts[index + 1]) if index + 1 < len(sig.starts) else v.capture.count)
                self.draw_segment(painter, sig, sig.codes[index], v.sample_x(start), v.sample_x(end), y,
                                   index > 0 and sig.starts[index] >= v.left, (hi, lo))
        else:
            # Preserve subpixel activity: shade any pixel containing transitions.
            # Work is bounded by viewport width, regardless of capture length.
            if v.capture.times is not None:
                edges = self._time_edges(v)
            else:
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
                    painter.fillRect(QRectF(x1, hi, x2 - x1, lo - hi),
                                     QColor(color.red(), color.green(), color.blue(), 100))
                    painter.setPen(color)
                    self.line(painter, x1, hi, x2, hi)
                    self.line(painter, x1, lo, x2, lo)
                else:
                    self.draw_segment(painter, sig, code, x1, x2, y, band=(hi, lo))
                run_start = run_end

    def draw_bookmarks(self, painter, w, h, color, background):
        """Named bookmarks: dashed lines labelled at the top of the time axis."""
        v = self.view
        metrics = painter.fontMetrics()
        for bookmark in v.bookmarks.visible(v.left, v.left + v.span):
            x = min(w - 1, v.sample_x(bookmark.sample))
            painter.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
            self.line(painter, x, 18, x, h)
            label = metrics.elidedText(bookmark.name, Qt.TextElideMode.ElideRight, 96)
            width = metrics.horizontalAdvance(label) + 8
            label_x = min(x + 1, w - width - 1)
            painter.fillRect(QRectF(label_x, 0, width, 18), color)
            painter.setPen(background)
            painter.drawText(QRectF(label_x, 0, width, 18), Qt.AlignmentFlag.AlignCenter, label)

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
                             "Откройте Vivado ILA CSV, VCD или WLF\nCtrl+O или перетащите файл в окно")
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
        # Tick spacing uses 1/2/5 decades in time (Vivado-style labels) or in samples.
        axis = v.time_axis()
        spacing = 140
        ticks = (axis.ruler(v.left, v.left + v.span, v.plot_width, spacing) if axis else
                 sample_axis_ruler(v.left, v.left + v.span, v.plot_width, spacing))
        painter.setPen(grid)
        for sample, _ in ticks:
            x = v.sample_x(sample)
            self.line(painter, x, v.HEADER, x, h)
        if v.highlight_model and v.highlight_model.master_enabled:
            edges = None  # pixel boundaries on the real time axis, computed once per frame
            for group in v.highlight_model.groups:
                if not group.visible:
                    continue
                color = QColor(group.color)
                color.setAlpha(round(255 * group.opacity / 100))
                if edges is None:
                    edges = self.pixel_edges()
                for start, end in group.intervals.visible(v.left, v.left + v.span, v.plot_width, edges):
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
                annotations = v.annotations.for_signal(sig)
                if annotations:
                    self.draw_overlays(painter, annotations, sig, y)
            elif node.kind == "decoded":
                self.draw_track(painter, node.track, y)
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
            target, _ = v.drop_target
            position = v.drop_line  # where the line is drawn, even for reversed bit rows
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
        for sample, label in ticks:
            x = v.sample_x(sample)
            painter.setPen(muted)
            painter.drawText(QRectF(x + 4, 0, spacing - 8, v.HEADER), Qt.AlignmentFlag.AlignVCenter, label)
        if not v.period_mode and v.cursor_b is not None:
            xa, xb = sorted((v.sample_x(v.cursor_a), v.sample_x(v.cursor_b)))
            painter.fillRect(QRectF(xa, v.HEADER, xb - xa, h - v.HEADER), theme.a_b_band())
        if v.zoom_band:
            xa, xb = sorted(v.sample_x(s) for s in v.zoom_band)
            color = QColor(theme.bus)
            painter.fillRect(QRectF(xa, v.HEADER, xb - xa, h - v.HEADER),
                             QColor(color.red(), color.green(), color.blue(), 50))
            painter.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
            self.line(painter, xa, v.HEADER, xa, h)
            self.line(painter, xb, v.HEADER, xb, h)
        if not v.period_mode:
            self.draw_bookmarks(painter, w, h, QColor(theme.text), background)
        if not v.period_mode:
            self.draw_cursors(painter, w, h, theme, background, split)
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

    LABEL = 18

    def cursor_badges(self, w, split):
        """(text, x, colors) badges for markers A/B, kept apart when the lines are close.

        Coincident markers share one ``A/B`` badge in both colors; markers closer than a
        badge put the earlier label left of its line and the later one right of it.
        """
        v = self.view
        theme = v.themes.current
        size = self.LABEL
        shown = [(label, sample, QColor(color)) for label, sample, color in
                 (("A", v.cursor_a, theme.marker_a), ("B", v.cursor_b, theme.marker_b))
                 if sample is not None and v.left <= sample <= v.left + v.span]
        placed = [(label, min(w - 1, v.sample_x(sample)), color) for label, sample, color in shown]
        if len(placed) == 2 and abs(placed[0][1] - placed[1][1]) < size + 1:
            (_, xa, ca), (_, xb, cb) = placed
            if v.cursor_a == v.cursor_b or abs(xa - xb) < 1:
                width = 2 * size + 4
                return [("A/B", max(split, min(xa + 1, w - width)), width, (ca, cb))]
            (first, x1, c1), (second, x2, c2) = sorted(placed, key=lambda item: item[1])
            return [(first, max(split, x1 - size), size, (c1,)), (second, min(x2 + 1, w - size - 1), size, (c2,))]
        return [(label, min(x + 1, w - size - 1), size, (color,)) for label, x, color in placed]

    def draw_cursors(self, painter, w, h, theme, background, split):
        v = self.view
        lines = [(sample, QColor(color)) for sample, color in
                 ((v.cursor_a, theme.marker_a), (v.cursor_b, theme.marker_b))
                 if sample is not None and v.left <= sample <= v.left + v.span]
        for index, (sample, color) in enumerate(lines):
            x = min(w - 1, v.sample_x(sample))
            # B is dashed over a coincident A, so both colors stay visible on one line.
            dashed = index == 1 and abs(x - min(w - 1, v.sample_x(lines[0][0]))) < 1
            painter.setPen(QPen(color, 1.4, Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine))
            self.line(painter, x, 0, x, h)
        top = v.HEADER - self.LABEL
        for text, x, width, colors in self.cursor_badges(w, split):
            part = width / len(colors)
            for index, color in enumerate(colors):
                painter.fillRect(QRectF(x + index * part, top, part, self.LABEL), color)
            painter.setPen(background)
            painter.drawText(QRectF(x, top, width, self.LABEL), Qt.AlignmentFlag.AlignCenter, text)

