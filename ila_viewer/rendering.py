# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Drawing-only renderer, separate from input handling and tree operations."""
import math
import numpy as np
from PySide6.QtCore import QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF

from .data import search_sorted
from .timebase import sample_axis_ruler
from .viewport import pixel_runs, viewport_transform
from .spi import ADDR, CMD, DATA, DUMMY, MISO, MOSI, READ, RESET, UNKNOWN, WRITE, bubble_label

# Theme roles for decoded protocol fields.
FIELD_ROLES = {CMD: "marker_a", ADDR: "marker_b", DUMMY: "muted", READ: "bit", WRITE: "bus",
               MOSI: "bus", MISO: "bit", DATA: "bus", RESET: "muted"}


class WaveformRenderer:
    @staticmethod
    def _time_edges(v):
        """Pixel boundaries of a view-like object on its time axis (kept for callers outside paint)."""
        return viewport_transform(v.capture, v.left, v.span, v.plot_left, v.plot_width).edges

    def __init__(self, view):
        self.view = view

    def pixel_edges(self, t=None):
        """Sample positions of the plot's pixel boundaries, following the real time axis."""
        return (t or self.view.transform()).edges

    def draw_track(self, painter, track, y, t):
        """A decoder row: one filled, labelled box per field, a shaded band when zoomed out."""
        v = self.view
        first, stop = track.window(t.left, t.left + t.span)
        if stop - first > max(8, t.plot_width // 3):
            self.draw_busy(painter, track.busy(t.edges), y + 12, y + v.ROW - 12, t)
            return
        self.draw_boxes(painter, track.result, track.fields[first:stop], y + 10, y + v.ROW - 10, t)

    def draw_busy(self, painter, spans, top, bottom, t):
        v = self.view
        color = QColor(v.themes.current.bus)
        color.setAlpha(100)
        for a, b in spans:
            x1, x2 = t.x(a), t.x(b)
            painter.fillRect(QRectF(x1, top, max(1.0, x2 - x1), bottom - top), color)

    @staticmethod
    def ink(fill):
        """Dark or light text, whichever reads better on ``fill``."""
        return QColor("#14161a") if fill.lightnessF() > 0.5 else QColor("#ffffff")

    def draw_boxes(self, painter, result, fields, top, bottom, t):
        """Decoded fields: rounded filled boxes with ``0x45`` centered."""
        v = self.view
        theme = v.themes.current
        left, right = t.left, t.left + t.span
        metrics = painter.fontMetrics()
        height = bottom - top
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        for record in fields:
            kind, flags = int(record["kind"]), int(record["flags"])
            fill = QColor(theme.unknown if flags & UNKNOWN else getattr(theme, FIELD_ROLES.get(kind, "bus")))
            x1 = t.x(max(left, float(record["start"])))
            x2 = t.x(min(right, float(record["end"])))
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

    def draw_overlays(self, painter, annotations, sig, y, t):
        """Decoded bytes above the waveform and sampling arrows on the clock."""
        v = self.view
        left, right = t.left, t.left + t.span
        for annotation in annotations:
            if annotation.kind == "fields":
                top, bottom = y + 3, y + 20
                first, stop = annotation.window(left, right)
                if stop - first > max(8, t.plot_width // 3):
                    self.draw_busy(painter, annotation.busy(t.edges), top, bottom, t)
                else:
                    self.draw_boxes(painter, annotation.result, annotation.visible(first, stop), top, bottom, t)
            elif annotation.kind == "edges":
                positions = annotation.visible(left, right, max(1, t.plot_width // 6))
                if positions is None or not len(positions):
                    continue
                xs = t.xs(np.asarray(positions, dtype=np.float64)).tolist()
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

    def row_painter(self, painter, sig, y):
        """Colors, pens and brushes of one signal row, resolved once for all its segments."""
        return RowPainter(self.view, painter, sig, self.band(sig, y))

    def draw_segment(self, painter, sig, code, x1, x2, y, transition=False, band=None):
        """One run of a row (callers outside ``draw_signal``); resolves the row style itself."""
        row = RowPainter(self.view, painter, sig, band or self.band(sig, y))
        row.segment(code, x1, x2, transition)
        row.finish()

    def draw_signal(self, painter, sig, y, t):
        v = self.view
        row = self.row_painter(painter, sig, y)
        left, right = t.left, t.left + t.span
        first = sig.run_index(left)
        last = min(len(sig.starts), search_sorted(sig.starts, right, "left") + 1)
        if last - first <= t.plot_width * 2:
            # Read the visible runs once and map every boundary to x in one call.
            bounds = np.asarray(sig.starts[first:last + 1], dtype=np.int64)
            if last + 1 > len(sig.starts):
                bounds = np.append(bounds, np.int64(v.capture.count))
            x1 = t.xs(np.maximum(bounds[:-1], left))
            x2 = t.xs(np.minimum(bounds[1:], right))
            codes = np.asarray(sig.codes[first:last])
            # A run starting inside the window (not the capture's first) begins with a transition.
            transitions = (bounds[:-1] >= left) & (np.arange(first, last) > 0)
            if not (row.levels(codes, x1, x2, transitions) or row.shapes(codes, x1, x2)):
                segment = row.segment
                for code, a, b, transition in zip(codes.tolist(), x1.tolist(), x2.tolist(), transitions.tolist()):
                    segment(code, a, b, transition)
        else:
            # Preserve subpixel activity: shade any pixel containing transitions.
            # Work is bounded by viewport width, regardless of capture length.
            indices = np.maximum(0, search_sorted(sig.starts, t.edges, "right") - 1)
            codes = np.asarray(sig.codes[indices[:-1]])
            starts, ends, busy, run_codes = pixel_runs(indices, codes)
            plot_left = t.plot_left
            for start, stop, shaded, code in zip(starts.tolist(), ends.tolist(), busy.tolist(), run_codes.tolist()):
                if shaded:
                    row.busy(plot_left + start, plot_left + stop)
                else:
                    row.segment(code, plot_left + start, plot_left + stop)
        row.finish()

    def draw_bookmarks(self, painter, w, h, color, background, t):
        """Named bookmarks: dashed lines labelled at the top of the time axis."""
        v = self.view
        metrics = painter.fontMetrics()
        for bookmark in v.bookmarks.visible(t.left, t.left + t.span):
            x = min(w - 1, t.x(bookmark.sample))
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
        t = v.transform()  # one sample <-> x mapping for the whole frame
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
        ticks = (axis.ruler(v.left, v.left + v.span, t.plot_width, spacing) if axis else
                 sample_axis_ruler(v.left, v.left + v.span, t.plot_width, spacing))
        painter.setPen(grid)
        for sample, _ in ticks:
            x = t.x(sample)
            self.line(painter, x, v.HEADER, x, h)
        if v.highlight_model and v.highlight_model.master_enabled:
            for group in v.highlight_model.groups:
                if not group.visible:
                    continue
                color = QColor(group.color)
                color.setAlpha(round(255 * group.opacity / 100))
                # Pixel boundaries on the real time axis come from the frame transform, once.
                bands = np.asarray(list(group.intervals.visible(t.left, t.left + t.span, t.plot_width, t.edges)),
                                   dtype=np.float64).reshape(-1, 2)
                for x1, x2 in zip(t.xs(bands[:, 0]).tolist(), t.xs(bands[:, 1]).tolist()):
                    painter.fillRect(QRectF(x1, v.HEADER, x2 - x1, h - v.HEADER), color)
        for row in range(first, last):
            item = v.rows[row]
            node, depth = item.node, item.depth
            sig = node.signal
            y = v.HEADER + row * v.ROW - scroll
            painter.save()
            painter.setClipRect(QRectF(split, v.HEADER, t.plot_width, max(0, h - v.HEADER)))
            if sig:
                self.draw_signal(painter, sig, y, t)
                annotations = v.annotations.for_signal(sig)
                if annotations:
                    self.draw_overlays(painter, annotations, sig, y, t)
            elif node.kind == "decoded":
                self.draw_track(painter, node.track, y, t)
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
        painter.setClipRect(QRectF(split, 0, t.plot_width, h))
        for sample, label in ticks:
            x = t.x(sample)
            painter.setPen(muted)
            painter.drawText(QRectF(x + 4, 0, spacing - 8, v.HEADER), Qt.AlignmentFlag.AlignVCenter, label)
        if not v.period_mode and v.cursor_b is not None:
            xa, xb = sorted((t.x(v.cursor_a), t.x(v.cursor_b)))
            painter.fillRect(QRectF(xa, v.HEADER, xb - xa, h - v.HEADER), theme.a_b_band())
        if v.zoom_band:
            xa, xb = sorted(t.x(s) for s in v.zoom_band)
            color = QColor(theme.bus)
            painter.fillRect(QRectF(xa, v.HEADER, xb - xa, h - v.HEADER),
                             QColor(color.red(), color.green(), color.blue(), 50))
            painter.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
            self.line(painter, xa, v.HEADER, xa, h)
            self.line(painter, xb, v.HEADER, xb, h)
        if not v.period_mode:
            self.draw_bookmarks(painter, w, h, QColor(theme.text), background, t)
        if not v.period_mode:
            self.draw_cursors(painter, w, h, theme, background, split, t)
        for label, sample, color in (("1", v.period_markers[0], QColor(theme.marker_a)),
                                     ("2", v.period_markers[1], QColor(theme.marker_b))):
            if sample is not None and v.left <= sample <= v.left + v.span:
                x = t.x(sample)
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

    def cursor_badges(self, w, split, t=None):
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
        x_of = t.x if t is not None else v.sample_x
        placed = [(label, min(w - 1, x_of(sample)), color) for label, sample, color in shown]
        if len(placed) == 2 and abs(placed[0][1] - placed[1][1]) < size + 1:
            (_, xa, ca), (_, xb, cb) = placed
            if v.cursor_a == v.cursor_b or abs(xa - xb) < 1:
                width = 2 * size + 4
                return [("A/B", max(split, min(xa + 1, w - width)), width, (ca, cb))]
            (first, x1, c1), (second, x2, c2) = sorted(placed, key=lambda item: item[1])
            return [(first, max(split, x1 - size), size, (c1,)), (second, min(x2 + 1, w - size - 1), size, (c2,))]
        return [(label, min(x + 1, w - size - 1), size, (color,)) for label, x, color in placed]

    def draw_cursors(self, painter, w, h, theme, background, split, t):
        v = self.view
        lines = [(sample, QColor(color)) for sample, color in
                 ((v.cursor_a, theme.marker_a), (v.cursor_b, theme.marker_b))
                 if sample is not None and v.left <= sample <= v.left + v.span]
        for index, (sample, color) in enumerate(lines):
            x = min(w - 1, t.x(sample))
            # B is dashed over a coincident A, so both colors stay visible on one line.
            dashed = index == 1 and abs(x - min(w - 1, t.x(lines[0][0]))) < 1
            painter.setPen(QPen(color, 1.4, Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine))
            self.line(painter, x, 0, x, h)
        top = v.HEADER - self.LABEL
        for text, x, width, colors in self.cursor_badges(w, split, t):
            part = width / len(colors)
            for index, color in enumerate(colors):
                painter.fillRect(QRectF(x + index * part, top, part, self.LABEL), color)
            painter.setPen(background)
            painter.drawText(QRectF(x, top, width, self.LABEL), Qt.AlignmentFlag.AlignCenter, text)



class RowPainter:
    """Draws the runs of one signal row with colors, pens and labels resolved once.

    Straight lines in one pen are collected and drawn with one ``drawLines`` call; the
    batch is flushed before any other drawing, so the painting order of overlapping
    translucent fills and lines is the same as drawing every line on its own.
    """

    def __init__(self, view, painter, sig, band):
        self.painter = painter
        self.sig = sig
        self.values = sig.values
        self.is_bus = sig.is_bus
        self.hi, self.lo = band
        preferences = view.preferences
        self.fill_high = preferences.fill_high
        self.fill_bus = preferences.fill_bus
        self.radix = view.display_radix(sig)
        color = view.signal_color(sig)
        unknown = QColor(view.themes.current.unknown)
        self.color = color
        self.pen = QPen(color, 1.2)
        self.unknown_pen = QPen(unknown, 1.2)
        self.high_fill = QColor(color.red(), color.green(), color.blue(), 65)
        self.bus_brush = QColor(color.red(), color.green(), color.blue(), 65)
        self.unknown_brush = QColor(unknown.red(), unknown.green(), unknown.blue(), 65)
        self.busy_fill = QColor(color.red(), color.green(), color.blue(), 100)
        self.busy_pen = QPen(color)
        self.metrics = painter.fontMetrics()
        self.labels = {}
        self.current_pen = None
        self.brush = None  # unknown until the first polygon sets it
        self.batch_pen = None
        self.batch = []

    def use_pen(self, pen):
        if pen is not self.current_pen:
            self.painter.setPen(pen)
            self.current_pen = pen

    def stroke(self, pen, x1, y1, x2, y2):
        if pen is not self.batch_pen:
            self.flush()
            self.batch_pen = pen
        self.batch.append(QLineF(x1, y1, x2, y2))

    def flush(self):
        if self.batch:
            self.use_pen(self.batch_pen)
            self.painter.drawLines(self.batch)
            self.batch = []

    def busy(self, x1, x2):
        """A run of pixels that each contain a transition: a shaded band."""
        hi, lo = self.hi, self.lo
        self.flush()
        self.painter.fillRect(QRectF(x1, hi, x2 - x1, lo - hi), self.busy_fill)
        self.stroke(self.busy_pen, x1, hi, x2, hi)
        self.stroke(self.busy_pen, x1, lo, x2, lo)

    def levels(self, codes, x1, x2, transitions):
        """Draw a 1-bit row's runs as level and transition lines built with numpy.

        Equal to calling ``segment`` for every run in order: lines go into the same-pen
        batch, and a run with an unknown value is drawn by ``segment`` at its place in
        the sequence. Returns False (drawing nothing) when the row needs per-run drawing:
        a bus, or translucent high-level fills that must interleave with the lines.
        """
        if self.is_bus or self.fill_high:
            return False
        values = self.values
        unknown = np.fromiter((isinstance(value, str) for value in values), dtype=bool, count=len(values))
        high = np.fromiter((not isinstance(value, str) and bool(value) for value in values),
                           dtype=bool, count=len(values))  # a 1-bit row has a handful of values
        codes = np.asarray(codes, dtype=np.int64)
        visible = x2 > x1
        levels = np.where(high[codes], self.hi, self.lo).astype(np.float64)
        odd = np.flatnonzero(unknown[codes] & visible)
        stop = len(codes)
        for chunk_start, chunk_stop in zip(np.concatenate(([0], odd + 1)).tolist(), np.append(odd, stop).tolist()):
            part = slice(chunk_start, chunk_stop)
            shown = np.flatnonzero(visible[part] & ~unknown[codes[part]]) + chunk_start
            if len(shown):
                a, b, y = x1[shown].tolist(), x2[shown].tolist(), levels[shown].tolist()
                self.lines(self.pen, map(QLineF, a, y, b, y))
                steps = shown[transitions[shown]]
                if len(steps):
                    xs = x1[steps].tolist()
                    count = len(xs)
                    self.lines(self.pen, map(QLineF, xs, [self.hi] * count, xs, [self.lo] * count))
            if chunk_stop < stop:
                self.segment(int(codes[chunk_stop]), float(x1[chunk_stop]), float(x2[chunk_stop]),
                             bool(transitions[chunk_stop]))
        return True

    def shapes(self, codes, x1, x2):
        """Draw a bus row's runs as notched shapes with vertices computed by numpy.

        Equal to calling ``segment`` for every run in order. Returns False for a 1-bit
        row, whose known runs are lines rather than shapes.
        """
        if not self.is_bus:
            return False
        self.flush()
        painter, values, labels = self.painter, self.values, self.labels
        hi, lo = self.hi, self.lo
        middle = (hi + lo) / 2
        shown = np.flatnonzero(x2 > x1)
        codes = np.asarray(codes, dtype=np.int64)[shown]
        # Unknown-ness only for the codes on screen; a counter bus has millions of values.
        present, inverse = np.unique(codes, return_inverse=True)
        odd = np.fromiter((isinstance(values[code], str) for code in present.tolist()),
                          dtype=bool, count=len(present))[inverse.reshape(-1)]
        a, b = x1[shown], x2[shown]
        notch = np.minimum(4, (b - a) / 2)
        brushes = ((self.bus_brush, self.unknown_brush) if self.fill_bus
                   else (Qt.BrushStyle.NoBrush, Qt.BrushStyle.NoBrush))
        pens = (self.pen, self.unknown_pen)
        mixed = bool(odd.any())
        if not mixed:
            self.use_pen(pens[0])
            if brushes[0] is not self.brush:
                painter.setBrush(brushes[0])
                self.brush = brushes[0]
        draw_polygon = painter.drawPolygon
        for code, unknown, wide, left, inner_left, inner_right, right in zip(
                codes.tolist(), odd.tolist(), (b - a > 20).tolist(), a.tolist(), (a + notch).tolist(),
                (b - notch).tolist(), b.tolist()):
            if mixed:
                pen = pens[unknown]
                if pen is not self.current_pen:
                    painter.setPen(pen)
                    self.current_pen = pen
                brush = brushes[unknown]
                if brush is not self.brush:
                    painter.setBrush(brush)
                    self.brush = brush
            draw_polygon(QPolygonF([QPointF(left, middle), QPointF(inner_left, hi),
                                    QPointF(inner_right, hi), QPointF(right, middle),
                                    QPointF(inner_right, lo), QPointF(inner_left, lo)]))
            if wide:
                label = labels.get(code)
                if label is None:
                    label = labels[code] = self.sig.format_value(values[code], self.radix)
                label = self.metrics.elidedText(label, Qt.TextElideMode.ElideRight, int(right - left - 10))
                painter.drawText(QRectF(left + 5, hi - 12, right - left - 10, lo - hi + 24),
                                 Qt.AlignmentFlag.AlignCenter, label)
        return True

    def lines(self, pen, lines):
        if pen is not self.batch_pen:
            self.flush()
            self.batch_pen = pen
        self.batch.extend(lines)

    def segment(self, code, x1, x2, transition=False):
        if x2 <= x1:
            return
        value = self.values[code]
        hi, lo = self.hi, self.lo
        unknown = isinstance(value, str)
        if not self.is_bus and not unknown:
            level = hi if value else lo
            if value and self.fill_high:
                self.flush()
                self.painter.fillRect(QRectF(x1, hi, x2 - x1, lo - hi + 1), self.high_fill)
            self.stroke(self.pen, x1, level, x2, level)
            if transition:
                self.stroke(self.pen, x1, hi, x1, lo)
            return
        self.flush()
        painter = self.painter
        self.use_pen(self.unknown_pen if unknown else self.pen)
        notch = min(4, (x2 - x1) / 2)
        middle = (hi + lo) / 2
        polygon = QPolygonF([QPointF(x1, middle), QPointF(x1 + notch, hi),
                             QPointF(x2 - notch, hi), QPointF(x2, middle),
                             QPointF(x2 - notch, lo), QPointF(x1 + notch, lo)])
        brush = (self.unknown_brush if unknown else self.bus_brush) if self.fill_bus else Qt.BrushStyle.NoBrush
        if brush is not self.brush:
            painter.setBrush(brush)
            self.brush = brush
        painter.drawPolygon(polygon)
        if x2 - x1 > 20:
            label = self.labels.get(code)
            if label is None:
                label = self.labels[code] = self.sig.format_value(value, self.radix)
            label = self.metrics.elidedText(label, Qt.TextElideMode.ElideRight, int(x2 - x1 - 10))
            painter.drawText(QRectF(x1 + 5, hi - 12, x2 - x1 - 10, lo - hi + 24), Qt.AlignmentFlag.AlignCenter, label)

    def finish(self):
        """Draw the pending lines and leave the painter with no brush, as each segment did."""
        self.flush()
        if self.brush is not None and self.brush is not Qt.BrushStyle.NoBrush:
            self.painter.setBrush(Qt.BrushStyle.NoBrush)
            self.brush = Qt.BrushStyle.NoBrush
