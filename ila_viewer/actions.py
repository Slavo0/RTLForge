# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Composable context-menu providers. Add a provider without changing the window."""
from abc import ABC, abstractmethod

from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                              QFormLayout, QInputDialog, QMenu, QSpinBox)

from .formats import FORMATS, RealSettings
from .themes import SignalPalette


class ContextActionProvider(ABC):
    @abstractmethod
    def contribute(self, menu, view): ...


class RadixActions(ContextActionProvider):
    def contribute(self, menu, view):
        nodes = view.selected_signal_nodes
        if not nodes:
            return
        node = nodes[0]
        radix = menu.addMenu("Radix")
        choices = [("DEFAULT", "Default")] + [(f.key, f.label) for f in FORMATS.formats.values()]
        for key, label in choices:
            action = radix.addAction(label)
            action.setCheckable(True)
            action.setChecked(all(n.options.radix == key for n in nodes))
            if key == "REAL":
                enabled = all(n.signal.width in (32, 64) or (n.options.real and n.options.real.mode == "fixed") for n in nodes)
                action.setEnabled(bool(enabled))
                action.setToolTip("IEEE 754: 32/64 бита. Для другой ширины задайте Fixed Point в Real Settings.")
            action.triggered.connect(lambda checked=False, k=key: view.set_radix(k))
        radix.addSeparator()
        radix.addAction("Real Settings…", lambda: self.real_settings(view, node))

    def real_settings(self, view, node):
        nodes = view.selected_signal_nodes
        settings = node.options.real or RealSettings("float" if node.signal.width in (32, 64) else "fixed")
        dialog = QDialog(view)
        dialog.setWindowTitle("Real Settings")
        form = QFormLayout(dialog)
        mode = QComboBox()
        mode.addItem("Fixed Point", "fixed")
        if all(n.signal.width in (32, 64) for n in nodes):
            mode.addItem(f"IEEE 754 ({node.signal.width} bit)", "float")
        mode.setCurrentIndex(max(0, mode.findData(settings.mode)))
        fraction = QSpinBox()
        fraction.setRange(0, min(n.signal.width for n in nodes))
        fraction.setValue(settings.fractional_bits)
        signed = QCheckBox("Signed (two's complement)")
        signed.setChecked(settings.signed)
        def update():
            fraction.setEnabled(mode.currentData() == "fixed")
            signed.setEnabled(mode.currentData() == "fixed")
        mode.currentIndexChanged.connect(update)
        update()
        form.addRow("Представление", mode)
        form.addRow("Дробных бит", fraction)
        form.addRow(signed)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec():
            for target in nodes:
                target.options.real = RealSettings(mode.currentData(), fraction.value(), signed.isChecked())
            view.set_radix("REAL")


class ColorActions(ContextActionProvider):
    def contribute(self, menu, view):
        node = view.current_node
        if not node:
            return
        colors = menu.addMenu("Signal Color")
        action = colors.addAction("По теме / наследовать от группы или шины")
        action.triggered.connect(lambda: view.set_color(None))
        colors.addSeparator()
        palette = SignalPalette()
        selected = {node.id for node in view.selected_nodes}
        comparison_colors = []
        for node in view.tree.rows(""):
            if node.node.signal and node.node.id not in selected:
                comparison_colors.append(view.signal_color(node.node.signal).name())
        recommended = palette.recommended(view.themes.current, comparison_colors)
        for color in sorted(palette.colors, key=lambda c: c not in recommended):
            swatch = QPixmap(18, 18)
            swatch.fill(QColor(color))
            action = colors.addAction(QIcon(swatch), color + ("  — подходит" if color in recommended else ""))
            action.setCheckable(True)
            action.setChecked(all(n.options.color == color for n in view.selected_nodes))
            action.triggered.connect(lambda checked=False, c=color: view.set_color(c))


class SignalActions(ContextActionProvider):
    def contribute(self, menu, view):
        node = view.current_node
        if not node or not node.signal:
            return
        menu.addSeparator()
        reverse = menu.addAction("Reverse Bit Order")
        reverse.setCheckable(True)
        reverse.setEnabled(node.kind == "bus")
        reverse.setChecked(node.options.reverse)
        reverse.triggered.connect(view.reverse_bits)
        menu.addAction("Find Value…", view.findRequested.emit)
        source = menu.addAction("Go to Source Code")
        source.setEnabled(view.source_navigator.available(node.signal.source))
        source.setToolTip("Точка расширения: привязка к HDL будет добавлена позже")
        source.triggered.connect(lambda: view.source_navigator.go_to(node.signal.source))


class StructureActions(ContextActionProvider):
    def contribute(self, menu, view):
        menu.addSeparator()
        menu.addAction("New Divider…", lambda: self.create(view, "divider"))
        menu.addAction("New Group…", lambda: self.create(view, "group"))
        node = view.current_node
        if node and node.kind in ("group", "divider"):
            menu.addAction("Переименовать…", lambda: self.rename(view, node))
            menu.addAction("Удалить группу и её сигналы" if node.kind == "group" else "Удалить разделитель",
                           lambda: (view.tree.remove_container(node), view.refresh_rows()))

    def create(self, view, kind):
        text, ok = QInputDialog.getText(view, "New Group" if kind == "group" else "New Divider", "Имя:")
        if ok and text.strip():
            node = view.tree.create_group_many(text.strip(), view.selected_nodes) if kind == "group" else view.tree.create_divider(text.strip(), view.current_node)
            view.refresh_rows(node)

    def rename(self, view, node):
        text, ok = QInputDialog.getText(view, "Переименовать", "Имя:", text=node.label)
        if ok and text.strip():
            node.label = text.strip()
            view.refresh_rows(node)


class CopyActions(ContextActionProvider):
    def contribute(self, menu, view):
        action = menu.addAction("Copy Values…", lambda: view.copyRequested.emit("DEFAULT"))
        action.setEnabled(view.capture is not None and bool(view.selected_signal_nodes))


class CellCopyActions(ContextActionProvider):
    """Copy exactly what the user sees in the clicked Name or Value cell."""

    def contribute(self, menu, view):
        node = view.current_node
        if not node:
            return
        if view.context_column == "name":
            menu.addAction("Copy Name", lambda: view.cellCopyRequested.emit(view.row_name(node)))
        elif view.context_column == "value":
            menu.addAction("Copy Value", lambda: view.cellCopyRequested.emit(view.row_value(node)))


class HighlightActions(ContextActionProvider):
    def contribute(self, menu, view):
        action = menu.addAction("Подсветить участки по условию…", view.highlightRequested.emit)
        action.setEnabled(view.capture is not None)
        action.setToolTip("Создать подсветку A…B или всего захвата, если B не установлен")


class ContextMenuController:
    def __init__(self, providers=None):
        self.providers = list(providers) if providers is not None else [RadixActions(), ColorActions(), SignalActions(), CopyActions(), CellCopyActions(), HighlightActions(), StructureActions()]

    def build(self, view):
        menu = QMenu(view)
        for provider in self.providers:
            provider.contribute(menu, view)
        menu.addSeparator()
        menu.addAction("Приблизить между A и B", view.zoom_cursors)
        return menu
