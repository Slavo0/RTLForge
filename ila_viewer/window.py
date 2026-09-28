# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Standalone offline viewer for Vivado ILA CSV exports."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QDialog,
    QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QToolBar,
    QVBoxLayout, QWidget,
)

from .workers import BackgroundTask
from .themes import ThemeManager
from .sources import VivadoCsvSource
from .bits import BitExpansionService
from .formats import FORMATS
from .view import WaveformView
from .preferences import PreferencesStore
from .settings_dialog import AppearanceDialog
from .copying import CopyValuesService, SystemClipboard
from .conditions import ConditionCompiler
from .copy_dialog import CopyConditionDialog
from .period_dialog import CopyPeriodDialog


class MainWindow(QMainWindow):
    def __init__(self, source=None, themes=None, source_navigator=None, menus=None, preferences_store=None, clipboard=None):
        super().__init__()
        self.setWindowTitle("ILA Waveform Reader")
        self.setAcceptDrops(True)
        self.capture = None
        self.worker = None
        self.closing = False
        self.preferences_store = preferences_store if preferences_store is not None else PreferencesStore.for_application()
        self.preferences = self.preferences_store.load()
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(300)
        self.save_timer.timeout.connect(self.save_preferences)
        self.source = source or VivadoCsvSource()
        self.themes = themes or ThemeManager()
        self.themes.select(self.preferences.theme if self.preferences.theme in self.themes.themes else next(iter(self.themes.themes)))
        self.bit_expansion = BitExpansionService()
        self.copy_service = CopyValuesService()
        self.copy_period = None
        self.copy_phase = 0
        self.copy_period_dialog = None
        self.pending_copy_radix = None
        self.clipboard = clipboard if clipboard is not None else SystemClipboard()
        self.view = WaveformView(themes=self.themes, menus=menus, source_navigator=source_navigator, preferences=self.preferences)
        self.themes.changed.connect(self.apply_theme)
        self.apply_theme()
        self.view.findRequested.connect(self.focus_search)
        self.view.expansionRequested.connect(self.expand_bus)
        self.view.copyRequested.connect(self.copy_values)
        self.view.cursorChanged.connect(self.update_status)
        self.view.selectionChanged.connect(self.update_selection)
        self.view.message.connect(lambda s: self.statusBar().showMessage(s, 5000))
        self.view.viewChanged.connect(self.update_status)

        toolbar = QToolBar("Навигация", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        self.open_action = self.add_action(toolbar, "Открыть CSV", self.choose_file, "Ctrl+O")
        toolbar.addSeparator()
        self.add_action(toolbar, "+", lambda: self.view.zoom(0.5), tooltip="Приблизить: + или Ctrl+колесо")
        self.add_action(toolbar, "−", lambda: self.view.zoom(2), tooltip="Отдалить: − или Ctrl+колесо")
        self.add_action(toolbar, "Весь захват", self.view.fit, tooltip="F — уместить весь захват")
        self.add_action(toolbar, "A ↔ B", self.view.zoom_cursors, tooltip="Уместить диапазон между курсорами")
        toolbar.addSeparator()
        self.add_action(toolbar, "← Изменение", lambda: self.view.next_transition(-1), tooltip="← — предыдущее изменение выбранного сигнала")
        self.add_action(toolbar, "Изменение →", lambda: self.view.next_transition(1), tooltip="→ — следующее изменение выбранного сигнала")
        self.add_action(toolbar, "К отсчёту…", self.goto_sample, "Ctrl+G")
        self.add_action(toolbar, "Триггер", self.goto_trigger)
        self.add_action(toolbar, "Настройки вида…", self.show_appearance)

        tools_menu = self.menuBar().addMenu("Файл")
        tools_menu.addAction(self.open_action)
        self.add_action(tools_menu, "Сохранить вид как PNG…", self.save_image)
        self.add_action(tools_menu, "Выход", self.close, "Ctrl+Q")
        settings = self.menuBar().addMenu("Вид")
        self.add_action(settings, "Настройки вида…", self.show_appearance)
        self.add_action(settings, "Период отсчёта…", self.set_period)
        self.add_action(settings, "Убрать курсор B", self.clear_b)
        help_menu = self.menuBar().addMenu("Справка")
        self.add_action(help_menu, "Управление", self.show_help, "F1")

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 0, 8, 5)
        layout.setSpacing(7)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Фильтр сигналов по имени…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self.view.filter_signals)
        search_row = QHBoxLayout()
        search_row.addWidget(self.filter_edit)
        search_row.addWidget(QLabel("Значение:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("HEX: 0AC, 100; или 0xAC / 0b1010")
        self.search_edit.setToolTip("Точное значение целой шины. Без префикса — в выбранном формате. Enter: следующее совпадение.")
        self.search_edit.returnPressed.connect(lambda: self.find_value(1))
        search_row.addWidget(self.search_edit, 1)
        self.previous_button = QPushButton("Найти ←")
        self.next_button = QPushButton("Найти →")
        self.previous_button.clicked.connect(lambda: self.find_value(-1))
        self.next_button.clicked.connect(lambda: self.find_value(1))
        search_row.addWidget(self.previous_button)
        search_row.addWidget(self.next_button)
        layout.addLayout(search_row)
        layout.addWidget(self.view, 1)
        self.readout = QLabel("Откройте waveform.csv")
        self.readout.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.readout.setWordWrap(True)
        layout.addWidget(self.readout)
        self.setCentralWidget(central)
        self.view.columnsChanged.connect(self.columns_changed)
        self.sync_filter_width()
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(210)
        self.progress.hide()
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.clicked.connect(self.cancel_work)
        self.cancel_button.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.cancel_button)
        self.statusBar().showMessage("Ctrl+колесо: масштаб · Shift+колесо: время · ←/→: изменения · Shift+клик: курсор B")
        self.add_action(self, "Поиск", self.focus_search, "Ctrl+F")
        self.add_action(self, "Следующее совпадение", lambda: self.find_value(1), "F3")
        self.add_action(self, "Предыдущее совпадение", lambda: self.find_value(-1), "Shift+F3")
        available = QApplication.primaryScreen().availableGeometry()
        self.resize(min(self.preferences.window_width, int(available.width() * 0.95)),
                    min(self.preferences.window_height, int(available.height() * 0.95)))
        self.move(available.center() - self.rect().center())
        if self.preferences.maximized:
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
        if self.preferences_store.error:
            self.statusBar().showMessage("Не удалось прочитать настройки; используются значения по умолчанию.", 8000)

    def add_action(self, container, text, callback, shortcut=None, tooltip=None):
        action = QAction(text, self)
        action.triggered.connect(callback)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        if tooltip:
            action.setToolTip(tooltip)
        container.addAction(action)
        return action

    def focus_search(self):
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def copy_values(self, radix="DEFAULT"):
        if self.worker:
            self.statusBar().showMessage("Дождитесь завершения операции или нажмите «Отмена».", 4000)
            return
        try:
            request = self.copy_service.snapshot(self.view, radix)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 7000)
            return
        if self.copy_period is None:
            if self.copy_period_dialog is not None:
                self.copy_period_dialog.raise_()
                self.copy_period_dialog.activateWindow()
                return
            self.pending_copy_radix = radix
            self.view.period_markers = [None, None]
            self.copy_period_dialog = CopyPeriodDialog(self.view, self)
            self.copy_period_dialog.finished.connect(self.finish_period_setup)
            self.copy_period_dialog.show()
            self.copy_period_dialog.raise_()
            self.statusBar().showMessage("Укажите на диаграмме две точки одного полного периода.", 6000)
            return
        radix_by_name = {}
        stack = list(self.view.tree.root.children)
        while stack:
            node = stack.pop()
            if node.signal:
                radix_by_name[node.signal.source.name] = self.view.display_radix(node.signal)
            stack.extend(node.children)
        compiler = ConditionCompiler(self.capture.signals, radix_by_name)
        dialog = CopyConditionDialog(self.capture.signals, compiler, self, full_names=self.view.full_names)
        if not dialog.exec():
            return
        try:
            request = self.copy_service.snapshot(self.view, radix, compiler.compile(dialog.condition_text()),
                                                 self.copy_period, self.copy_phase)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 7000)
            return
        def complete(result):
            if result.count == 0:
                self.statusBar().showMessage("Таких данных нет.", 6000)
                return
            self.clipboard.write(result.text)
            self.statusBar().showMessage(f"Скопировано {result.count:,} значений в буфер обмена.", 6000)
        self.start_work(lambda task: self.copy_service.build(request, task.progress.emit, task.isInterruptionRequested),
                        complete, "Подготовка значений для буфера обмена…", "значений")

    def finish_period_setup(self, result):
        dialog, self.copy_period_dialog = self.copy_period_dialog, None
        self.view.request_period_marker(None)
        self.view.period_markers = [None, None]
        self.view.viewport().update()
        if not dialog or result != QDialog.DialogCode.Accepted:
            self.pending_copy_radix = None
            self.statusBar().showMessage("Настройка периода отменена.", 5000)
            return
        self.copy_period = dialog.accepted_period
        self.copy_phase = dialog.phase
        radix, self.pending_copy_radix = self.pending_copy_radix, None
        self.statusBar().showMessage(f"Период копирования: {self.copy_period:,} отсчётов.", 5000)
        self.copy_values(radix or "DEFAULT")

    def apply_theme(self):
        self.setStyleSheet(self.themes.current.stylesheet())
        self.preferences.theme = self.themes.current.key
        self.save_timer.start()

    def sync_filter_width(self):
        self.filter_edit.setFixedWidth(min(self.view.name_width + 1, max(100, self.width() - 350)))

    def columns_changed(self):
        self.sync_filter_width()
        self.preferences.name_width = self.view.name_width
        self.preferences.value_width = self.view.value_width
        self.save_timer.start()

    def show_appearance(self):
        dialog = AppearanceDialog(self.preferences, self.themes, self)
        if dialog.exec():
            self.apply_preferences(dialog.result_preferences())

    def apply_preferences(self, preferences):
        self.preferences = preferences
        self.view.preferences = preferences
        self.view.full_names = preferences.full_names
        self.themes.select(preferences.theme)
        self.view.viewport().update()
        self.update_selection()
        self.save_preferences()

    def save_preferences(self):
        self.save_timer.stop()
        self.preferences.name_width = self.view.name_width
        self.preferences.value_width = self.view.value_width
        self.preferences.full_names = self.view.full_names
        self.preferences.theme = self.themes.current.key
        self.preferences.maximized = self.isMaximized()
        geometry = self.normalGeometry() if self.isMaximized() else self.geometry()
        self.preferences.window_width = geometry.width()
        self.preferences.window_height = geometry.height()
        try:
            self.preferences_store.save(self.preferences)
        except OSError as exc:
            self.statusBar().showMessage(f"Не удалось сохранить настройки: {exc}", 8000)

    def expand_bus(self, node):
        if self.worker:
            self.statusBar().showMessage("Дождитесь завершения операции или нажмите «Отмена».", 4000)
            return
        if not self.capture:
            return
        def complete(result):
            self.capture.derived.append(result)
            self.view.tree.add_bits(node, result.signals)
            self.view.refresh_rows(node)
            self.statusBar().showMessage(f"Раскрыта шина: {node.label}", 4000)
        self.start_work(lambda task: self.bit_expansion.build(node.signal.source, self.capture.storage.name,
                                                              task.progress.emit, task.isInterruptionRequested),
                        complete, f"Подготовка битов {node.label}…", "битов")

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Открыть Vivado ILA CSV", str(Path.cwd()), "CSV (*.csv);;Все файлы (*)")
        if path:
            self.open_file(path)

    def open_file(self, path):
        if self.worker:
            self.statusBar().showMessage("Дождитесь завершения операции или нажмите «Отмена».", 4000)
            return
        self.start_work(lambda task: self.source.load(path, task.progress.emit, task.isInterruptionRequested),
                        self.loaded, f"Загрузка {Path(path).name}…")

    def start_work(self, work, complete, message, progress_unit="отсчётов"):
        if self.worker:
            return
        task = BackgroundTask(work, self)
        self.worker = task
        self.progress_unit = progress_unit
        self.open_action.setEnabled(False)
        self.previous_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.cancel_button.show()
        self.statusBar().showMessage(message)
        task.progress.connect(self.load_progress)
        task.finished.connect(lambda: self.finish_work(task, complete))
        task.start()

    def load_progress(self, percent, rows):
        self.progress.setRange(0, 100)
        self.progress.setValue(percent)
        self.statusBar().showMessage(f"Подготовка: {rows:,} {self.progress_unit}")

    def finish_work(self, task, complete):
        self.worker = None
        self.open_action.setEnabled(True)
        self.previous_button.setEnabled(True)
        self.next_button.setEnabled(True)
        self.progress.hide()
        self.cancel_button.hide()
        if self.closing:
            if hasattr(task.result, "close"):
                task.result.close()
            task.deleteLater()
            self.close()
            return
        if task.error:
            self.statusBar().showMessage("Операция не выполнена.", 5000)
            QMessageBox.warning(self, "Ошибка", task.error)
        elif task.cancelled:
            self.statusBar().showMessage("Операция отменена.", 5000)
        else:
            complete(task.result)
        task.deleteLater()

    def cancel_work(self):
        if self.worker:
            self.worker.requestInterruption()
            self.statusBar().showMessage("Отмена операции…")

    def loaded(self, capture):
        old = self.capture
        self.capture = capture
        self.filter_edit.clear()
        self.view.set_capture(capture)
        if old:
            old.close()
        self.setWindowTitle(f"{capture.path.name} — ILA Waveform Reader")
        changes = sum(len(sig.starts) - 1 for sig in capture.signals)
        self.statusBar().showMessage(f"{capture.count:,} отсчётов · {len(capture.signals)} сигналов · {changes:,} изменений")
        self.view.setFocus()

    def update_selection(self):
        sig = self.view.current_signal
        effective = FORMATS.effective(self.view.display_radix(), sig.radix) if sig else "HEX"
        hints = {"HEX": "HEX: 0AC; или 0xAC / 0b1010", "BINARY": "Binary: 1010; или 0xA",
                 "OCTAL": "Octal: 254; или 0o254", "ASCII": 'ASCII: AB; пробелы в кавычках; \\x00 — байт 00',
                 "UNSIGNED": "Unsigned Decimal: 172", "SIGNED": "Signed Decimal: -1",
                 "SIGNED_MAGNITUDE": "Signed Magnitude: -5", "REAL": "Real: 1.5"}
        self.search_edit.setPlaceholderText(hints.get(effective, "Значение"))
        self.update_status()

    def update_status(self):
        if not self.capture or not hasattr(self, "readout"):
            return
        v = self.view
        parts = [f"A: {v.cursor_a:,}"]
        if v.period_ns:
            parts.append(f"tA: {v.cursor_a * v.period_ns:g} ns")
        if v.cursor_b is not None:
            parts.extend([f"B: {v.cursor_b:,}", f"Δ: {abs(v.cursor_b - v.cursor_a):,} отсч."])
            if v.period_ns:
                parts.append(f"Δt: {abs(v.cursor_b - v.cursor_a) * v.period_ns:g} ns")
        for name, data in self.capture.metadata.items():
            parts.append(f"{name}: {int(data[v.cursor_a])}")
        parts.append(f"Вид: {v.left:.0f}…{min(self.capture.count - 1, v.left + v.span):.0f}")
        self.readout.setText("   |   ".join(parts))

    def find_value(self, direction):
        sig = self.view.current_signal
        if not sig or self.worker:
            return
        try:
            value = sig.query_value(self.search_edit.text(), self.view.display_radix())
        except ValueError as exc:
            self.statusBar().showMessage(f"Поиск: {exc}", 7000)
            self.search_edit.setFocus()
            return
        sample = self.view.cursor_a
        self.start_work(lambda task: sig.search(value, sample, direction, task.isInterruptionRequested),
                        self.found, f"Поиск в {sig.short_name}…")

    def found(self, result):
        if result is None:
            self.statusBar().showMessage("Значение не найдено в выбранном сигнале.", 6000)
            return
        sample, wrapped = result
        self.view.set_cursor(sample)
        self.view.setFocus()
        self.statusBar().showMessage(f"Найдено: отсчёт {sample}" + (" · поиск продолжен с другого края захвата" if wrapped else ""), 6000)

    def goto_sample(self):
        if not self.capture:
            return
        text, ok = QInputDialog.getText(self, "Перейти к отсчёту", f"Индекс строки: 0…{self.capture.count - 1}", text=str(self.view.cursor_a))
        if ok:
            try:
                sample = int(text)
                if not 0 <= sample < self.capture.count:
                    raise ValueError()
                self.view.set_cursor(sample)
                self.view.setFocus()
            except ValueError:
                self.statusBar().showMessage("Введите целый индекс внутри захвата.", 5000)

    def goto_trigger(self):
        if not self.capture or self.worker:
            return
        sig = next((s for s in self.capture.signals if s.name.upper() == "TRIGGER"), None)
        if not sig:
            self.statusBar().showMessage("В файле нет столбца TRIGGER.", 5000)
            return
        self.start_work(lambda task: sig.search(1, -1, 1, task.isInterruptionRequested),
                        lambda result: self.found(result) if result else self.statusBar().showMessage("В захвате нет TRIGGER=1.", 5000),
                        "Поиск триггера…")

    def set_period(self):
        period, ok = QInputDialog.getDouble(self, "Период отсчёта", "Период ILA clock, ns (0 — только отсчёты):",
                                           self.view.period_ns, 0, 1e12, 6)
        if ok:
            self.view.period_ns = period
            self.view.viewport().update()
            self.update_status()

    def toggle_names(self, checked):
        self.view.full_names = checked
        self.view.viewport().update()
        self.save_timer.start()

    def clear_b(self):
        self.view.cursor_b = None
        self.view.viewport().update()
        self.update_status()

    def save_image(self):
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить текущий вид", "waveform.png", "PNG (*.png)")
        if path:
            if not Path(path).suffix:
                path += ".png"
            if not self.view.viewport().grab().save(path):
                QMessageBox.warning(self, "Ошибка", "Не удалось сохранить PNG.")

    def show_help(self):
        QMessageBox.information(self, "Управление", """Щелчок по сигналу — выбрать сигнал и установить курсор A.
Shift + щелчок — курсор B; обычный щелчок по диаграмме — убрать B.
Shift + щелчок в Name / Value — выделить диапазон строк без изменения B.
Radix, Signal Color и перетаскивание применяются ко всему выделению.
Протянуть ЛКМ по диаграмме — приблизить диапазон (от 2 отсчётов).
Перетаскивание за линию маркера — переместить маркер; для B удерживать Shift.
Copy Values → Radix — условие и копирование выбранных сигналов/битов от A до B.
Условие поддерживает &&, ||, ! / ~, сравнения и выбор сигнала из списка.
При первом копировании за запуск выберите две точки периода; B — исключающая граница.
Настройки вида → Копирование — разделитель и режим повторов.
Name и Value имеют независимые границы; двойной щелчок — подобрать ширину.
Общий Radix, тема и заливка единиц находятся в «Настройки вида» и сохраняются.
Стрелка слева от шины — раскрыть / свернуть биты.
Перетаскивание имени — изменить порядок строк.
Верх / низ строки: вставить до / после; центр группы: перенести внутрь.
Правая кнопка — Radix, Signal Color, Reverse Bit Order, Find Value,
New Divider, New Group. Цвета с пометкой «подходит» контрастны теме.
← / → — предыдущее / следующее изменение выбранного сигнала.
Ctrl + ← / → — перемещение A на один отсчёт.
↑ / ↓ — выбрать соседний сигнал.
Колесо — вертикальная прокрутка сигналов.
Shift + колесо, Shift + ← / → — прокрутка времени.
Ctrl + колесо — масштаб относительно мыши; + / − — относительно A.
Средняя кнопка мыши + движение — прокрутка времени.
F — весь захват; Home / End — начало / конец.
Ctrl+F — поиск; Enter / F3 — вперёд; Shift+F3 — назад.
Ctrl+G — перейти к индексу отсчёта; Ctrl+O — открыть CSV.
Перетаскивание границы столбцов — ширина области имён.
Правый щелчок — формат значения выбранного сигнала.

Поиск: точное значение целой шины; результат — начало интервала.
Без префикса используется выбранный HEX/UNSIGNED/SIGNED/BINARY.
Префиксы 0x и 0b задают формат явно. Поиск циклический.
Плотные изменения на общем виде показаны заполненной полосой.
Приблизьте участок, чтобы увидеть отдельные переходы.

Ось X — порядок строк CSV, начиная с 0. Sample in Buffer/Window
показываются отдельно. Время вычисляется только из введённого
периода: при захвате с пропусками это не реальное время.""")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and any(u.isLocalFile() for u in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            if url.isLocalFile():
                self.open_file(url.toLocalFile())
                break

    def closeEvent(self, event):
        if self.worker:
            self.closing = True
            self.cancel_work()
            event.ignore()
            return
        self.save_preferences()
        if self.capture:
            self.view.capture = None
            self.view.auto_scroll.stop()
            self.capture.close()
            self.capture = None
        event.accept()


