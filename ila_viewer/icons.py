# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Vector icons stay sharp at any display scale; no image dependencies."""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPen, QPolygonF


class RowIconPainter:
    def paint(self, painter, kind, x, y, color):
        painter.save()
        painter.setPen(QPen(color, 1.4))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if kind == "bus":
            painter.drawRect(QRectF(x + 2, y + 3, 11, 9))
            painter.drawLine(QPointF(x + 5, y), QPointF(x + 5, y + 15))
            painter.drawLine(QPointF(x + 10, y), QPointF(x + 10, y + 15))
        elif kind in ("signal", "bit"):
            painter.drawPolyline(QPolygonF([QPointF(x, y + 12), QPointF(x + 5, y + 12),
                                           QPointF(x + 5, y + 3), QPointF(x + 11, y + 3),
                                           QPointF(x + 11, y + 12), QPointF(x + 15, y + 12)]))
        elif kind == "group":
            painter.drawPolygon(QPolygonF([QPointF(x, y + 2), QPointF(x + 6, y + 2),
                                          QPointF(x + 8, y + 5), QPointF(x + 15, y + 5),
                                          QPointF(x + 15, y + 14), QPointF(x, y + 14)]))
        elif kind == "divider":
            for offset in (5, 10):
                painter.drawLine(QPointF(x, y + offset), QPointF(x + 15, y + offset))
        else:
            painter.drawText(QRectF(x, y - 2, 16, 20), Qt.AlignmentFlag.AlignCenter, "P")
        painter.restore()

    def arrow(self, painter, x, y, expanded, color):
        painter.save()
        painter.setPen(QPen(color, 1.5))
        points = [(x, y - 3), (x + 4, y + 1), (x + 8, y - 3)] if expanded else [(x + 2, y - 5), (x + 6, y - 1), (x + 2, y + 3)]
        painter.drawPolyline(QPolygonF([QPointF(*p) for p in points]))
        painter.restore()
