# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""SPI / QSPI decoder UI: settings dialog, transaction list, commands and context menu.

The window only installs ``SpiController`` into a menu and forwards capture changes;
decoding and export run through the window's cancellable background task, and the
clipboard is written on the GUI thread after a successful export.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDockWidget,
                               QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QSizePolicy, QSpinBox, QStackedWidget, QTableView,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .actions import ContextActionProvider
from .spi import (CONTENTS, DATA_KINDS, LINE_NAMES, MISO, PARTIAL, TRUNCATED, UNKNOWN,
                  UNKNOWN_COMMAND, BusWidths, CommandSpec, DecodedTrack, FixedFormat,
                  SpiDecoder, SpiDecoderConfig, SpiExportRequest, SpiExportService, SpiPins,
                  SpiSettings, default_commands, guess_pins, line_options, overlays, rebind)
from .tree import DecodedNode
from .widgets import SegmentedChoice, muted, plain_container

FIXED_PRESETS = ("1-1-1", "1-1-2", "1-2-2", "1-1-4", "1-4-4", "2-2-2", "4-4-4", "0-4-4", "0-1-1", "1-0-1", "1-1-0", "1-0-0")
DIRECTION_TEXT = {"read": "чтение", "write": "запись", "none": "нет"}
SEPARATORS = {"space": ("Пробел", " "), "none": ("Без разделителя", ""), "comma": ("Запятая", ","),
              "comma_space": ("Запятая и пробел", ", "), "tab": ("Табуляция", "\t"), "newline": ("Новая строка", "\n")}


def _combo(items, current=None):
    box = QComboBox()
    for data, text in items:
        box.addItem(text, data)
    if current is not None:
        index = box.findData(current)
        box.setCurrentIndex(max(0, index))
    return box


class SpiDecoderDialog(QDialog):
    """Nonmodal: every line and every frame-format parameter is chosen explicitly."""

    decodeRequested = Signal(object)
    COLUMNS = ("Код", "Команда", "Extended", "Dual", "Quad", "Адрес, Б", "Dummy Ext",
               "Dummy Dual", "Dummy Quad", "Данные", "DTR", "Dummy из настройки")

    def __init__(self, signals, config=None, settings=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Декодер SPI / QSPI")
        self.setModal(False)
        self.options = line_options(signals)
        pins = config.pins if config else guess_pins(signals)
        settings = config.settings if config else (settings or SpiSettings())
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # 1. Wires. IO2/IO3 matter only for quad formats, so they open on demand.
        lines = QGroupBox("Линии")
        form = QFormLayout(lines)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)
        self.cs_box = self._line_box(pins.cs if pins else None, "— нет: весь захват один кадр")
        self.cs_level = SegmentedChoice([(True, "активен 0", "CS# — кадр, пока линия в нуле"),
                                         (False, "активен 1", "Кадр, пока линия в единице")],
                                        pins.cs_active_low if pins else True)
        row = QHBoxLayout()
        row.addWidget(self.cs_box, 1)
        row.addWidget(self.cs_level)
        form.addRow("CS", row)
        self.sck_box = self._line_box(pins.sck if pins else None, None)
        form.addRow("SCK", self.sck_box)
        self.io_boxes = []
        hints = ("MOSI · IO0", "MISO · IO1", "IO2 · WP#", "IO3 · HOLD#")
        self.quad_rows = []
        for index, hint in enumerate(hints):
            box = self._line_box(pins.io[index] if pins else None, "— не подключена")
            self.io_boxes.append(box)
            label = QLabel(hint)
            form.addRow(label, box)
            if index >= 2:
                self.quad_rows.append((label, box))
        self.quad_toggle = QPushButton("+ IO2, IO3 для Quad SPI")
        self.quad_toggle.setCheckable(True)
        self.quad_toggle.setAutoDefault(False)
        self.quad_toggle.toggled.connect(self._show_quad)
        form.addRow("", self.quad_toggle)

        # 2. Sampling.
        sampling = QGroupBox("Выборка")
        sample_form = QFormLayout(sampling)
        sample_form.setHorizontalSpacing(12)
        sample_form.setVerticalSpacing(6)
        self.edge_box = SegmentedChoice([("rising", "↑ передний", "SPI режимы 0 и 3"),
                                         ("falling", "↓ задний", "SPI режимы 1 и 2")], settings.sample_edge)
        self.timing_box = SegmentedChoice([(True, "до фронта", "Значение линии перед фронтом, как его видит регистр"),
                                           (False, "на фронте", "Значение линии в момент фронта")],
                                          settings.before_edge)
        sample_form.addRow("Фронт SCK", self.edge_box)
        sample_form.addRow("Чтение линий", self.timing_box)

        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(lines, 3)
        top.addWidget(sampling, 2, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)

        # 3. What a frame contains. Pages follow the segment order.
        content = QGroupBox("Содержимое кадра")
        content_layout = QVBoxLayout(content)
        content_layout.setSpacing(8)
        self.mode_box = SegmentedChoice([
            ("raw", "Только байты", "Каждый кадр CS — поток байтов, без команды и адреса"),
            ("commands", "Команды флеш-памяти", "Формат кадра по коду команды из таблицы"),
            ("fixed", "Один формат для всех", "Команда, адрес, dummy и данные заданы вручную")], settings.mode)
        mode_row = QHBoxLayout()
        mode_row.addWidget(self.mode_box)
        mode_row.addStretch(1)
        content_layout.addLayout(mode_row)
        self.pages = QStackedWidget()
        self.pages.addWidget(self._raw_page(settings))
        self.pages.addWidget(self._commands_page(settings))
        self.pages.addWidget(self._fixed_page(settings.fixed))
        self.mode_box.currentIndexChanged.connect(self._mode_changed)
        content_layout.addWidget(self.pages, 1)
        layout.addWidget(content, 1)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setProperty("role", "error")
        buttons = QHBoxLayout()
        buttons.addWidget(self.error, 1)
        self.decode_button = QPushButton("Декодировать")
        self.decode_button.setProperty("primary", True)
        self.decode_button.setDefault(True)
        self.decode_button.clicked.connect(self.submit)
        close = QPushButton("Закрыть")
        close.setAutoDefault(False)
        close.clicked.connect(self.reject)
        buttons.addWidget(close)
        buttons.addWidget(self.decode_button)
        layout.addLayout(buttons)

        has_quad = any(box.currentData() is not None for _, box in self.quad_rows)
        self.quad_toggle.setChecked(has_quad)
        self._show_quad(has_quad)
        self._mode_changed()
        self.resize(760, 0)

    def _show_quad(self, shown):
        for label, box in self.quad_rows:
            label.setVisible(shown)
            box.setVisible(shown)
        self.quad_toggle.setText("− скрыть IO2, IO3" if shown else "+ IO2, IO3 для Quad SPI")

    def _mode_changed(self, *_):
        self.pages.setCurrentIndex(self.mode_box.currentIndex())
        # Only the current page takes space; the table page is tall.
        for index in range(self.pages.count()):
            policy = QSizePolicy.Policy.Preferred if index == self.pages.currentIndex() else QSizePolicy.Policy.Ignored
            self.pages.widget(index).setSizePolicy(policy, policy)
        self.pages.updateGeometry()
        # Fit the height to the current page once the layout has settled; width stays.
        QTimer.singleShot(0, lambda: self.resize(self.width(), self.sizeHint().height()))

    def _line_box(self, current, empty_text):
        box = QComboBox()
        if empty_text is not None:
            box.addItem(empty_text, None)
        for option in self.options:
            box.addItem(option.text, option)
            box.setItemData(box.count() - 1, option.signal, Qt.ItemDataRole.ToolTipRole)
        if current is not None:
            index = next((i for i in range(box.count()) if box.itemData(i) == current), -1)
            box.setCurrentIndex(max(0, index))
        return box

    def _commands_page(self, settings):
        page = QWidget()
        page.setObjectName("plain")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)
        self.protocol_box = SegmentedChoice([
            ("extended", "Extended 1-x-x", "Команда на 1 линии, адрес и данные — по формату команды"),
            ("dual", "Dual 2-2-2", "Все фазы на 2 линиях"),
            ("quad", "Quad 4-4-4", "Все фазы на 4 линиях")], settings.protocol)
        self.address_box = SegmentedChoice([(3, "3 байта"), (4, "4 байта", "Команды с 3-байтовым адресом тоже читают 4 байта")],
                                           settings.address_bytes)
        self.dummy_box = QSpinBox()
        self.dummy_box.setRange(-1, 255)
        self.dummy_box.setSpecialValueText("по таблице")
        self.dummy_box.setValue(-1 if settings.dummy_cycles is None else settings.dummy_cycles)
        self.dummy_box.setToolTip("Dummy-такты команд FAST READ. Должно совпадать с настройкой микросхемы.")
        self.dtr_box = QCheckBox("DTR: все фазы на обоих фронтах")
        self.dtr_box.setChecked(settings.dtr)
        form.addRow("Протокол", self.protocol_box)
        form.addRow("Адрес", self.address_box)
        dummy_row = QHBoxLayout()
        dummy_row.addWidget(self.dummy_box)
        dummy_row.addSpacing(16)
        dummy_row.addWidget(self.dtr_box)
        dummy_row.addStretch(1)
        form.addRow("Dummy FAST READ", dummy_row)
        layout.addLayout(form)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setToolTip("Формат: команда-адрес-данные в линиях. Пусто в Dual/Quad — "
                              "те же фазы на 2/4 линиях. Данные: чтение, запись или нет.")
        self.fill_table(settings.commands)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(260)
        self.table_toggle = QPushButton()
        self.table_toggle.setCheckable(True)
        self.table_toggle.setAutoDefault(False)
        self.table_toggle.toggled.connect(self._show_table)
        self.table_tools = plain_container()
        tools = QHBoxLayout(self.table_tools)
        tools.setContentsMargins(0, 0, 0, 0)
        add = QPushButton("Добавить")
        add.clicked.connect(self.add_row)
        remove = QPushButton("Удалить выбранные")
        remove.clicked.connect(self.remove_rows)
        reset = QPushButton("По умолчанию")
        reset.clicked.connect(lambda: self.fill_table(default_commands()))
        for button in (add, remove, reset):
            button.setAutoDefault(False)
            tools.addWidget(button)
        tools.addStretch(1)
        toggle_row = QHBoxLayout()
        toggle_row.addWidget(self.table_toggle)
        toggle_row.addWidget(self.table_tools, 1)
        layout.addLayout(toggle_row)
        layout.addWidget(self.table, 1)
        self._show_table(False)
        return page

    def _show_table(self, shown):
        self.table.setVisible(shown)
        self.table_tools.setVisible(shown)
        self.table_toggle.setText(("▾ " if shown else "▸ ") + f"Таблица команд ({self.table.rowCount()})")
        if self.isVisible():
            self._mode_changed()

    def fill_table(self, commands):
        self.table.setRowCount(0)
        for spec in commands:
            self.add_row(spec)

    def add_row(self, spec=None):
        if not isinstance(spec, CommandSpec):
            spec = CommandSpec(0, "НОВАЯ КОМАНДА", ("1-0-0", "", ""))
        row = self.table.rowCount()
        self.table.insertRow(row)
        cells = (f"{spec.opcode:02X}", spec.name, *spec.formats, str(spec.address_bytes),
                 *map(str, spec.dummy), DIRECTION_TEXT[spec.direction], "да" if spec.dtr else "нет",
                 "да" if spec.configurable_dummy else "нет")
        for column, text in enumerate(cells):
            self.table.setItem(row, column, QTableWidgetItem(text))
        if spec.opcode == 0 and spec.name == "НОВАЯ КОМАНДА":
            self.table.scrollToBottom()
            self.table.setCurrentCell(row, 0)

    def remove_rows(self):
        for row in sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)

    def table_commands(self):
        commands = []
        yes = {"да", "yes", "1", "true", "+"}
        directions = {"чтение": "read", "read": "read", "запись": "write", "write": "write",
                      "нет": "none", "none": "none", "": "none"}
        for row in range(self.table.rowCount()):
            text = [(self.table.item(row, c).text().strip() if self.table.item(row, c) else "")
                    for c in range(len(self.COLUMNS))]
            where = f"Строка {row + 1}"
            try:
                opcode = int(text[0].removesuffix("h").removesuffix("H"), 16)
            except ValueError:
                raise ValueError(f"{where}: код команды — HEX 00…FF") from None
            if not 0 <= opcode <= 0xFF:
                raise ValueError(f"{where}: код команды — HEX 00…FF")
            formats = []
            for value in text[2:5]:
                if value:
                    BusWidths.parse(value)
                formats.append(value)
            if not any(formats):
                raise ValueError(f"{where}: укажите формат хотя бы для одного протокола")
            try:
                address = int(text[5] or 0)
                dummy = tuple(int(value or 0) for value in text[6:9])
            except ValueError:
                raise ValueError(f"{where}: адрес и dummy — целые числа") from None
            if not 0 <= address <= 4 or not all(0 <= value <= 255 for value in dummy):
                raise ValueError(f"{where}: адрес 0…4 байта, dummy 0…255 тактов")
            direction = directions.get(text[9].casefold())
            if direction is None:
                raise ValueError(f"{where}: данные — «чтение», «запись» или «нет»")
            commands.append(CommandSpec(opcode, text[1] or f"CMD {opcode:02X}", tuple(formats), address, dummy,
                                        direction, text[10].casefold() in yes, text[11].casefold() in yes))
        return tuple(commands)

    def _fixed_page(self, fixed):
        page = QWidget()
        page.setObjectName("plain")
        form = QFormLayout(page)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)
        self.fixed_format = QComboBox()
        self.fixed_format.setEditable(True)
        self.fixed_format.addItems(FIXED_PRESETS)
        self.fixed_format.setCurrentText(fixed.widths.text)
        self.fixed_format.setToolTip("команда-адрес-данные в линиях; 0 — фазы нет "
                                     "(например 0-4-4 для чтения без кода команды)")
        self.fixed_address = QSpinBox()
        self.fixed_address.setRange(0, 4)
        self.fixed_address.setSuffix(" Б")
        self.fixed_address.setValue(fixed.address_bytes)
        self.fixed_dummy = QSpinBox()
        self.fixed_dummy.setRange(0, 255)
        self.fixed_dummy.setSuffix(" такт.")
        self.fixed_dummy.setValue(fixed.dummy)
        self.fixed_direction = SegmentedChoice([("read", "чтение", "Данные от устройства; одна линия — IO1"),
                                                ("write", "запись", "Данные в устройство; одна линия — IO0")],
                                               fixed.direction)
        self.fixed_dtr = SegmentedChoice([("off", "нет"), ("address_data", "адрес и данные", "Команда SDR"),
                                          ("all", "все фазы")], fixed.dtr)
        phases = QHBoxLayout()
        phases.setSpacing(6)
        phases.addWidget(self.fixed_format)
        phases.addSpacing(16)
        phases.addWidget(muted("адрес"))
        phases.addWidget(self.fixed_address)
        phases.addSpacing(16)
        phases.addWidget(muted("dummy"))
        phases.addWidget(self.fixed_dummy)
        phases.addStretch(1)
        form.addRow("Линии фаз", phases)
        form.addRow("Данные", self.fixed_direction)
        form.addRow("DTR", self.fixed_dtr)
        return page

    def _raw_page(self, settings):
        page = QWidget()
        page.setObjectName("plain")
        form = QFormLayout(page)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)
        self.raw_lines = SegmentedChoice([(1, "1 линия: MOSI и MISO", "Два потока байтов одновременно: IO0 и IO1"),
                                          (2, "2 линии", "IO1 IO0"), (4, "4 линии", "IO3…IO0")], settings.raw_lines)
        self.bit_order = SegmentedChoice([(True, "MSB first"), (False, "LSB first")], settings.msb_first)
        form.addRow("Данные", self.raw_lines)
        form.addRow("Порядок битов", self.bit_order)
        return page

    def config(self):
        sck = self.sck_box.currentData()
        if sck is None:
            raise ValueError("Выберите линию SCK")
        io = tuple(box.currentData() for box in self.io_boxes)
        cs = self.cs_box.currentData()
        used = [line for line in (sck, cs, *io) if line is not None]
        if len(set(used)) != len(used):
            raise ValueError("Одна линия выбрана для нескольких ролей")
        pins = SpiPins(sck, cs, bool(self.cs_level.currentData()), io)
        mode = self.mode_box.currentData()
        fixed = FixedFormat(BusWidths.parse(self.fixed_format.currentText()), self.fixed_address.value(),
                            self.fixed_dummy.value(), self.fixed_direction.currentData(), self.fixed_dtr.currentData())
        settings = SpiSettings(
            mode=mode, sample_edge=self.edge_box.currentData(), before_edge=bool(self.timing_box.currentData()),
            msb_first=bool(self.bit_order.currentData()) if mode == "raw" else True,
            protocol=self.protocol_box.currentData(), address_bytes=self.address_box.currentData(),
            dummy_cycles=None if self.dummy_box.value() < 0 else self.dummy_box.value(),
            dtr=self.dtr_box.isChecked(), commands=self.table_commands(), fixed=fixed,
            raw_lines=self.raw_lines.currentData())
        self._check_lines(settings, io)
        return SpiDecoderConfig(pins, settings)

    @staticmethod
    def _check_lines(settings, io):
        """Fail early when the chosen format needs a line that is not assigned."""
        if settings.mode == "commands":
            return  # commands differ per frame; missing lines decode as X
        widths = (settings.raw_lines,) if settings.mode == "raw" else (
            settings.fixed.widths.command, settings.fixed.widths.address, settings.fixed.widths.data)
        needed = {0}
        for width in widths:
            needed |= set(range(width)) if width > 1 else set()
        if settings.mode == "raw" and settings.raw_lines == 1 or settings.mode == "fixed" and settings.fixed.widths.data == 1 \
                and settings.fixed.direction == "read":
            needed.add(1)
        absent = [LINE_NAMES[i] for i in sorted(needed) if io[i] is None]
        if absent:
            raise ValueError("Для выбранного формата назначьте линии: " + ", ".join(absent))

    def submit(self):
        try:
            config = self.config()
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        self.error.clear()
        self.decodeRequested.emit(config)
        self.accept()


class SpiTransactionModel(QAbstractTableModel):
    """Virtual table over disk-backed frames: rows are read on demand."""

    COLUMNS = ("#", "Начало", "Длительность", "Команда", "Формат", "Адрес", "Dummy", "Байт", "Данные", "Флаги")
    PREVIEW = 16

    def __init__(self, axis=lambda: None, parent=None):
        super().__init__(parent)
        self.result = None
        self.rows = np.empty(0, dtype=np.int64)
        self.axis = axis

    def set_result(self, result, rows=None):
        self.beginResetModel()
        self.result = result
        self.rows = (np.arange(result.frame_count, dtype=np.int64) if rows is None else rows) if result else \
            np.empty(0, dtype=np.int64)
        self.endResetModel()

    def frame_index(self, row):
        return int(self.rows[row])

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.COLUMNS[section]
        return None

    def preview(self, index):
        result = self.result
        parts = []
        for lane, kinds in ((0, DATA_KINDS - {MISO}), (1, {MISO})):
            if lane >= result.lane_count:
                continue
            fields = result.frame_data(index, kinds, (lane,))
            if not len(fields) and lane:
                continue
            shown = fields[:self.PREVIEW]
            text = " ".join("XX" if f["flags"] & UNKNOWN else (f"{int(f['value']):X}/{int(f['bits'])}b"
                            if f["flags"] & PARTIAL else f"{int(f['value']):02X}") for f in shown)
            if len(fields) > self.PREVIEW:
                text += " …"
            parts.append(("MISO: " if lane else "") + text)
        return " | ".join(part for part in parts if part.strip(": "))

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or self.result is None:
            return None
        frame_index = self.frame_index(index.row())
        frame = self.result.frames[frame_index]
        column = index.column()
        if role == Qt.ItemDataRole.TextAlignmentRole and column in (0, 6, 7):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole and column == 3:
            return self.result.command_name(int(frame["opcode"]))
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        start, end = int(frame["start"]), int(frame["end"])
        axis = self.axis()
        if column == 0:
            return f"{frame_index + 1:,}"
        if column == 1:
            return f"{axis.readout(start)} ({start:,})" if axis else f"{start:,}"
        if column == 2:
            return axis.delta(start, end) if axis else f"{end - start:,} отсч."
        if column == 3:
            opcode = int(frame["opcode"])
            return f"{opcode:02X} {self.result.command_name(opcode)}" if opcode >= 0 else "—"
        if column == 4:
            return self.result.frame_format(frame_index)
        if column == 5:
            address = int(frame["address"])
            return f"{address:0{2 * max(1, int(frame['address_bytes']))}X}" if address >= 0 else ""
        if column == 6:
            return str(int(frame["dummy"])) if frame["dummy"] else ""
        if column == 7:
            return f"{int(frame['data_bytes']):,}"
        if column == 8:
            return self.preview(frame_index)
        flags = int(frame["flags"])
        names = [text for bit, text in ((UNKNOWN_COMMAND, "нет в таблице"), (UNKNOWN, "X/Z"),
                                        (PARTIAL, "неполный"), (TRUNCATED, "обрезан")) if flags & bit]
        return ", ".join(names)


@dataclass(frozen=True)
class ExportOptions:
    content: str = "data"
    radix: str = "HEX"
    separator: str = "space"
    prefix: bool = False
    layout: str = "stream"
    header: bool = True

    def request(self, result, **where):
        return SpiExportRequest(result, content=self.content, radix=self.radix,
                                separator=SEPARATORS[self.separator][1], prefix=self.prefix,
                                layout=self.layout, header=self.header, **where)


class SpiPanel(QDockWidget):
    """Transaction list with filters, navigation and byte export; dockable and nonmodal."""

    def __init__(self, controller, parent=None):
        super().__init__("Транзакции SPI / QSPI", parent)
        self.setObjectName("spiTransactions")
        self.controller = controller
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(6, 4, 6, 4)
        top = QHBoxLayout()
        self.filter_box = QComboBox()
        self.filter_box.setMinimumWidth(260)
        self.filter_box.currentIndexChanged.connect(self.apply_filter)
        self.data_only = QCheckBox("Только с данными")
        self.data_only.toggled.connect(self.apply_filter)
        self.summary = QLabel()
        self.settings_button = QPushButton("Настройки декодера…")
        self.settings_button.clicked.connect(controller.open_dialog)
        top.addWidget(QLabel("Показать:"))
        top.addWidget(self.filter_box)
        top.addWidget(self.data_only)
        top.addWidget(self.summary, 1)
        top.addWidget(self.settings_button)
        layout.addLayout(top)
        self.model = SpiTransactionModel(lambda: controller.view.time_axis(), self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setStretchLastSection(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        for column, width in ((0, 60), (1, 170), (2, 110), (3, 330), (4, 80), (5, 90), (6, 60), (7, 70), (9, 120)):
            header.resizeSection(column, width)
        self.table.doubleClicked.connect(lambda index: controller.select_frames([self.model.frame_index(index.row())]))
        self.table.setToolTip("Двойной щелчок — выделить транзакцию маркерами A/B и приблизить")
        layout.addWidget(self.table, 1)
        bottom = QHBoxLayout()
        self.content_box = _combo(list(CONTENTS.items()))
        self.radix_box = _combo([("HEX", "HEX"), ("DEC", "DEC"), ("BIN", "BIN")])
        self.separator_box = _combo([(key, text) for key, (text, _) in SEPARATORS.items()])
        self.prefix_box = QCheckBox("0x")
        self.layout_box = _combo([("stream", "Поток байтов"), ("frames", "Строка на транзакцию")])
        self.header_box = QCheckBox("№, код, адрес")
        self.header_box.setChecked(True)
        self.header_box.setToolTip("В режиме «Строка на транзакцию»: #номер код @адрес: перед байтами")
        for widget in (self.content_box, self.radix_box, self.separator_box, self.prefix_box,
                       self.layout_box, self.header_box):
            bottom.addWidget(widget)
        bottom.addStretch(1)
        layout.addLayout(bottom)
        buttons = QHBoxLayout()
        actions = (("Копировать выбранные", lambda: controller.copy_frames(self.selected_frames())),
                   ("Копировать A…B", controller.copy_range),
                   ("Выделить A/B", lambda: controller.select_frames(self.selected_frames())),
                   ("Выделить данные", lambda: controller.select_frames(self.selected_frames(), data_only=True)),
                   ("Сохранить .bin…", lambda: controller.save_binary(self.selected_frames() or None)))
        self.buttons = {}
        for text, callback in actions:
            button = QPushButton(text)
            button.clicked.connect(callback)
            buttons.addWidget(button)
            self.buttons[text] = button
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.setWidget(body)

    def options(self):
        return ExportOptions(self.content_box.currentData(), self.radix_box.currentData(),
                             self.separator_box.currentData(), self.prefix_box.isChecked(),
                             self.layout_box.currentData(), self.header_box.isChecked())

    def selected_frames(self):
        rows = sorted({index.row() for index in self.table.selectionModel().selectedRows()})
        return [self.model.frame_index(row) for row in rows]

    def set_result(self, result):
        self.filter_box.blockSignals(True)
        self.filter_box.clear()
        self.filter_box.addItem("Все транзакции", "all")
        if result is not None and result.frame_count:
            opcodes, counts = np.unique(np.asarray(result.frames["opcode"]), return_counts=True)
            for opcode, count in zip(opcodes, counts):
                if opcode >= 0:
                    self.filter_box.addItem(f"{int(opcode):02X} {result.command_name(int(opcode))} ({count:,})",
                                            int(opcode))
            self.filter_box.addItem("С ошибками / нет в таблице", "problems")
        self.filter_box.blockSignals(False)
        self.result = result
        self.apply_filter()

    def apply_filter(self):
        result = getattr(self, "result", None)
        if result is None:
            self.model.set_result(None)
            self.summary.setText("Нет результата декодирования")
            return
        frames = result.frames
        mask = np.ones(result.frame_count, dtype=bool)
        choice = self.filter_box.currentData()
        if isinstance(choice, int):
            mask &= np.asarray(frames["opcode"]) == choice
        elif choice == "problems":
            mask &= np.asarray(frames["flags"]) != 0
        if self.data_only.isChecked():
            mask &= np.asarray(frames["data_bytes"]) > 0
        rows = np.flatnonzero(mask)
        self.model.set_result(result, rows)
        total = int(np.asarray(frames["data_bytes"]).sum()) if result.frame_count else 0
        self.summary.setText(f"{len(rows):,} из {result.frame_count:,} · байт данных: {total:,} · "
                             f"{result.config.describe()}")


class SpiContextActions(ContextActionProvider):
    """Decoder commands for a right click on a decoded row or on the waveform."""

    def __init__(self, controller):
        self.controller = controller

    def contribute(self, menu, view):
        controller = self.controller
        node = view.current_node
        on_row = node is not None and node.kind == "decoded"
        if controller.result is None or view.capture is None or not (on_row or view.context_sample is not None):
            return
        menu.addSeparator()
        spi = menu.addMenu("SPI / QSPI")
        result = controller.result
        frame = result.frame_at(view.context_sample) if view.context_sample is not None else None
        if frame is not None:
            spi.addAction(f"Выделить транзакцию #{frame + 1}", lambda: controller.select_frames([frame]))
            spi.addAction("Выделить данные транзакции", lambda: controller.select_frames([frame], data_only=True))
            spi.addAction(f"Копировать данные транзакции #{frame + 1}", lambda: controller.copy_frames([frame]))
            spi.addSeparator()
        spi.addAction("Копировать байты A…B" if view.cursor_b is not None else "Копировать байты всего захвата",
                      controller.copy_range)
        overlay = spi.addAction("Байты на линиях MOSI/MISO и стрелки SCK")
        overlay.setCheckable(True)
        overlay.setChecked(view.annotations.enabled)
        overlay.toggled.connect(controller.set_overlays_enabled)
        spi.addAction("Список транзакций", controller.show_panel)
        spi.addAction("Настройки декодера…", controller.open_dialog)
        if on_row:
            spi.addSeparator()
            spi.addAction("Убрать строку декодера", lambda: controller.remove_node(node))


class SpiController(QObject):
    """Owns decoder state for the current capture; the window installs it into a menu.

    ``runner`` provides ``start_work(work, complete, message, unit)``, ``run_when_idle``
    and ``worker`` (the main window). The decoder config is capture state: refresh keeps
    it when its lines still exist, opening another file drops it.
    """

    message = Signal(str)

    def __init__(self, view, runner, clipboard=None, parent=None):
        super().__init__(parent)
        self.view = view
        self.runner = runner
        self._clipboard = clipboard  # None: use the runner's (window's) injected clipboard
        self.config = None
        self.settings = SpiSettings()  # last protocol choices, offered for the next capture
        self.result = None
        self.nodes = []
        self.panel = None
        self.dialog = None
        self.window = None
        self.exporter = SpiExportService()
        self.actions = []
        view.menus.providers.append(SpiContextActions(self))

    @property
    def clipboard(self):
        return self._clipboard if self._clipboard is not None else self.runner.clipboard

    def install(self, menu, window):
        self.window = window

        def action(text, callback, shortcut=None):
            item = QAction(text, window)
            if shortcut:
                item.setShortcut(shortcut)
                item.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            item.triggered.connect(callback)
            menu.addAction(item)
            self.actions.append(item)
            return item

        self.decoder_action = action("SPI / QSPI: декодер…", self.open_dialog)
        self.panel_action = action("SPI / QSPI: транзакции", self.show_panel)
        self.copy_action = action("SPI / QSPI: копировать байты A…B", self.copy_range, "Ctrl+Shift+C")
        self.overlay_action = action("SPI / QSPI: байты на линиях MOSI/MISO", self.set_overlays_enabled)
        self.overlay_action.setCheckable(True)
        self.overlay_action.setChecked(self.view.annotations.enabled)

    def set_overlays_enabled(self, enabled):
        """Show decoded bytes on the wires themselves; decoder rows stay."""
        self.view.annotations.set_enabled(enabled)
        action = getattr(self, "overlay_action", None)
        if action is not None and action.isChecked() != bool(enabled):
            action.setChecked(bool(enabled))

    # Decoding -----------------------------------------------------------------
    def signals(self):
        window = self.window
        if window is not None and hasattr(window, "condition_signals"):
            return window.condition_signals()
        return list(self.view.capture.signals) if self.view.capture else []

    def open_dialog(self):
        if self.view.capture is None:
            self.message.emit("Сначала откройте захват.")
            return None
        if self.dialog is not None:
            self.dialog.raise_()
            self.dialog.activateWindow()
            return self.dialog
        dialog = SpiDecoderDialog(self.signals(), self.config, self.settings, self.window or self.view)
        dialog.decodeRequested.connect(self.decode)
        dialog.finished.connect(lambda _result: self._dialog_closed(dialog))
        self.dialog = dialog
        dialog.show()
        dialog.raise_()
        return dialog

    def _dialog_closed(self, dialog):
        if self.dialog is dialog:
            self.dialog = None
        dialog.deleteLater()

    def decode(self, config):
        capture = self.view.capture
        if capture is None:
            return False
        if self.runner.worker:
            self.message.emit("Дождитесь окончания текущей операции.")
            return False
        self.config = config
        self.settings = config.settings
        self.runner.start_work(
            lambda task: SpiDecoder().decode(capture, config, None, task.progress.emit, task.isInterruptionRequested),
            lambda result: self.attach(result, capture), "Декодирование SPI / QSPI…", "кадров CS")
        return True

    def attach(self, result, capture):
        if capture is not self.view.capture:
            result.close()
            return
        parent, index = self._detach_nodes()
        self._close_result()
        self.result = result
        tree = self.view.tree
        parent = parent if parent is not None and (parent is tree.root or tree.root.contains(parent)) else tree.root
        index = min(index, len(parent.children))
        self.nodes = []
        for lane in range(result.lane_count):
            title = result.lane_title(lane)
            node = DecodedNode(title, DecodedTrack(result, lane, title))
            parent.add(node, index + lane)
            self.nodes.append(node)
        self.view.annotations.set(self, overlays(result, capture, [node.track for node in self.nodes]))
        self.view.refresh_rows(self.nodes[0])
        self.show_panel()
        self.panel.set_result(result)
        bytes_total = int(np.asarray(result.frames["data_bytes"]).sum()) if result.frame_count else 0
        problems = int(np.count_nonzero(np.asarray(result.frames["flags"]))) if result.frame_count else 0
        text = f"SPI: {result.frame_count:,} транзакций, {bytes_total:,} байт данных"
        self.message.emit(text + (f", с замечаниями: {problems:,}" if problems else ""))

    def _detach_nodes(self):
        """Remove decoder rows from the tree; return where the first one was."""
        position = (None, 0)
        for node in self.nodes:
            parent = node.parent
            if parent is not None and node in parent.children:
                if position[0] is None:
                    position = (parent, parent.children.index(node))
                parent.children.remove(node)
                node.parent = None
        self.nodes = []
        return position

    def _close_result(self):
        self.view.annotations.clear(self)
        if self.result is not None:
            self.result.close()
            self.result = None
        if self.panel is not None:
            self.panel.set_result(None)

    def remove_node(self, node):
        if node in self.nodes and node.parent is not None:
            node.parent.children.remove(node)
            node.parent = None
            self.view.refresh_rows()

    def dispose(self):
        """Release decoder files before the capture storage is removed."""
        self._detach_nodes()
        self._close_result()
        if self.dialog is not None:
            self.dialog.reject()

    def capture_changed(self, capture, refresh):
        """Called after the view received a new capture; the old tree is already gone."""
        self.nodes = []
        self._close_result()
        if self.dialog is not None:
            self.dialog.reject()
        config = rebind(self.config, capture) if refresh else None
        self.config = config
        if config is not None:
            # Highlights restore first; the decoder waits for an idle worker.
            QTimer.singleShot(0, lambda: self.runner.run_when_idle(
                lambda: self.decode(config) if self.view.capture is capture else None))

    # Panel and navigation -------------------------------------------------------
    def show_panel(self):
        if self.panel is None:
            self.panel = SpiPanel(self, self.window)
            if self.window is not None:
                self.window.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.panel)
            self.panel.set_result(self.result)
        self.panel.show()
        self.panel.raise_()
        return self.panel

    def frames_span(self, frames, data_only=False):
        result = self.result
        spans = []
        for index in frames:
            if data_only:
                fields = result.frame_data(index, DATA_KINDS, tuple(range(result.lane_count)))
                if len(fields):
                    spans.append((int(fields["start"].min()), int(fields["end"].max())))
            else:
                frame = result.frames[index]
                spans.append((int(frame["start"]), int(frame["end"])))
        if not spans:
            return None
        return min(a for a, _ in spans), max(b for _, b in spans)

    def select_frames(self, frames, data_only=False):
        """A at the first boundary, B at the exclusive end, then zoom to the range."""
        if self.result is None or not frames:
            self.message.emit("Выберите транзакции в списке SPI.")
            return None
        span = self.frames_span(frames, data_only)
        if span is None:
            self.message.emit("В выбранных транзакциях нет данных.")
            return None
        start, end = span
        view = self.view
        view.set_cursor(start, reveal=False)
        view.cursor_b = max(0, min(view.capture.count, end))
        width = max(2, end - start)
        view.set_range(start - width * 0.08, width * 1.16)
        view.cursorChanged.emit()
        view.viewport().update()
        return span

    # Export -------------------------------------------------------------------
    def options(self):
        return self.panel.options() if self.panel is not None else ExportOptions()

    def _export(self, request, what):
        if self.result is None:
            self.message.emit("Сначала декодируйте SPI: Анализ → SPI / QSPI: декодер…")
            return False
        if self.runner.worker:
            self.message.emit("Дождитесь окончания текущей операции.")
            return False

        def complete(result):
            if result.count == 0:
                self.message.emit("Таких байтов нет.")
                return
            self.clipboard.write(result.text)
            self.message.emit(f"Скопировано {result.count:,} значений SPI ({what}).")
        self.runner.start_work(lambda task: self.exporter.build(request, task.progress.emit, task.isInterruptionRequested),
                               complete, "Подготовка байтов SPI для буфера обмена…", "значений")
        return True

    def copy_frames(self, frames):
        if self.result is not None and not frames:
            self.message.emit("Выберите транзакции в списке SPI.")
            return False
        return self._export(self.options().request(self.result, frames=tuple(frames)) if self.result else None,
                            f"транзакций: {len(frames)}")

    def range_bounds(self):
        view = self.view
        if view.cursor_b is None:
            return 0, view.capture.count
        return tuple(sorted((view.cursor_a, view.cursor_b)))

    def copy_range(self):
        if self.result is None or self.view.capture is None:
            return self._export(None, "")
        start, end = self.range_bounds()
        return self._export(self.options().request(self.result, start=start, end=end),
                            "A…B" if self.view.cursor_b is not None else "весь захват")

    def save_binary(self, frames=None, path=None):
        if self.result is None:
            self.message.emit("Сначала декодируйте SPI.")
            return False
        if self.runner.worker:
            self.message.emit("Дождитесь окончания текущей операции.")
            return False
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self.window or self.view, "Сохранить данные SPI", "spi_data.bin",
                                                  "Двоичный файл (*.bin);;Все файлы (*)")
            if not path:
                return False
        options = self.options()
        if frames:
            request = options.request(self.result, frames=tuple(frames))
        else:
            start, end = self.range_bounds()
            request = options.request(self.result, start=start, end=end)
        target = Path(path)
        self.runner.start_work(lambda task: self.exporter.write_binary(request, target, task.progress.emit,
                                                                       task.isInterruptionRequested),
                               lambda count: self.message.emit(f"Сохранено {count:,} байт: {target}"),
                               "Сохранение данных SPI…", "байт")
        return True
