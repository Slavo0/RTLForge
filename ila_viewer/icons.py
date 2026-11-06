# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Vector row icons: sharp at any display scale, no image dependencies.

Each icon fits a 16 × 16 box and is drawn from the row color: a translucent fill
gives the shape weight, a 1.5 px outline with round joins keeps it crisp. The
shapes repeat what the row draws on the waveform (a step for a bit, a value cell
for a bus, byte bubbles for a decoder), so the name column reads like the plot.
"""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF


def _pen(color, width=1.5, style=Qt.PenStyle.SolidLine):
    pen = QPen(color, width, style)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _tint(color, alpha):
    tint = QColor(color)
    tint.setAlpha(alpha)
    return tint


class RowIconPainter:
    FILL = 70

    def paint(self, painter, kind, x, y, color):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(x, y)
        color = QColor(color)
        draw = getattr(self, f"_{kind}", self._parameter)
        draw(painter, color)
        painter.restore()

    def _bit(self, painter, color):
        # One pulse; the high part is filled like a high level with "fill high" on.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_tint(color, self.FILL))
        painter.drawRect(QRectF(5, 3.5, 6, 9))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(_pen(color))
        painter.drawPolyline(QPolygonF([QPointF(0.75, 12.5), QPointF(5, 12.5), QPointF(5, 3.5),
                                        QPointF(11, 3.5), QPointF(11, 12.5), QPointF(15.25, 12.5)]))

    _signal = _bit

    def _bus(self, painter, color):
        # Two bus values with a change between them, as drawn on the plot.
        top, bottom, middle = 3.5, 12.5, 8.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_tint(color, self.FILL))
        for cell in ([(0.75, middle), (3, top), (6, top), (8, middle), (6, bottom), (3, bottom)],
                     [(8, middle), (10, top), (13, top), (15.25, middle), (13, bottom), (10, bottom)]):
            painter.drawPolygon(QPolygonF([QPointF(*point) for point in cell]))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(_pen(color))
        painter.drawPolyline(QPolygonF([QPointF(0.75, middle), QPointF(3, top), QPointF(6, top), QPointF(10, bottom),
                                        QPointF(13, bottom), QPointF(15.25, middle)]))
        painter.drawPolyline(QPolygonF([QPointF(0.75, middle), QPointF(3, bottom), QPointF(6, bottom), QPointF(10, top),
                                        QPointF(13, top), QPointF(15.25, middle)]))

    def _group(self, painter, color):
        path = QPainterPath()
        path.moveTo(1, 4)
        path.quadTo(1, 2.5, 2.5, 2.5)
        path.lineTo(5.5, 2.5)
        path.lineTo(7.25, 4.25)
        path.lineTo(13.5, 4.25)
        path.quadTo(15, 4.25, 15, 5.75)
        path.lineTo(15, 12)
        path.quadTo(15, 13.5, 13.5, 13.5)
        path.lineTo(2.5, 13.5)
        path.quadTo(1, 13.5, 1, 12)
        path.closeSubpath()
        painter.setPen(_pen(color, 1.4))
        painter.setBrush(_tint(color, self.FILL))
        painter.drawPath(path)
        painter.drawLine(QPointF(1.5, 6.5), QPointF(14.5, 6.5))

    def _decoded(self, painter, color):
        # Two decoded bytes as filled bubbles, like the fields on the decoder row.
        painter.setPen(_pen(color, 1.3))
        painter.setBrush(_tint(color, 110))
        for left in (0.75, 8.75):
            painter.drawRoundedRect(QRectF(left, 4, 6.5, 8), 2.5, 2.5)

    def _divider(self, painter, color):
        painter.setPen(_pen(color, 1.5))
        painter.drawLine(QPointF(1, 8), QPointF(15, 8))
        painter.setPen(_pen(_tint(color, 140), 1.2))
        painter.drawLine(QPointF(4, 4.5), QPointF(12, 4.5))
        painter.drawLine(QPointF(4, 11.5), QPointF(12, 11.5))

    def _parameter(self, painter, color):
        # A per-row number (Sample in Window): a numbered tag.
        painter.setPen(_pen(color, 1.3))
        painter.setBrush(_tint(color, self.FILL))
        painter.drawRoundedRect(QRectF(1, 3, 14, 10), 3, 3)
        painter.setPen(_pen(color, 1.2))
        for left in (6, 9.5):
            painter.drawLine(QPointF(left, 5.5), QPointF(left - 1, 10.5))
        painter.drawLine(QPointF(3.75, 7), QPointF(12, 7))
        painter.drawLine(QPointF(3.5, 9.25), QPointF(11.75, 9.25))

    def arrow(self, painter, x, y, expanded, color):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(_pen(color, 1.6))
        points = [(x + 0.5, y - 2), (x + 4, y + 1.5), (x + 7.5, y - 2)] if expanded else \
            [(x + 2, y - 3.5), (x + 5.5, y), (x + 2, y + 3.5)]
        painter.drawPolyline(QPolygonF([QPointF(*p) for p in points]))
        painter.restore()
