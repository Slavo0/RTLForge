# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Standalone offline viewer for ILA CSV exports and simulator dumps (VCD, WLF)."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
    QDialog, QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSizePolicy, QToolBar,
    QVBoxLayout, QWidget,
)

from .workers import BackgroundTask
from .themes import ThemeManager
from .sources import AutoCaptureSource, WlfCaptureSource, QuestaWlfConverter, WlfExportService
from .bits import BitExpansionService
from .formats import FORMATS
from .view import WaveformView
from .preferences import PreferencesStore
from .settings_dialog import AppearanceDialog
from .copying import CopyValuesService, SystemClipboard
from .conditions import ConditionCompiler
from .copy_dialog import CopyOptionsDialog
from .period_dialog import CopyPeriodBanner
from .highlights import HighlightGroup, HighlightModel, HighlightService
from .advanced import AdvancedHighlightService
from .highlights_ui import HighlightConditionDialog, HighlightPanel
from .refresh import CaptureRefreshState, legacy_period
from .sampling import PeriodicSampling
from .timebase import parse_clock
from .bookmarks_ui import BookmarkController
from .measurements import EdgeMeasurementService, MeasurementFormatter

APP_NAME = "Waveform Reader"


class MainWindow(QMainWindow):
    def __init__(self, source=None, themes=None, source_navigator=None, menus=None, preferences_store=None, clipboard=None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
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
        self.source = source or AutoCaptureSource(wlf=WlfCaptureSource(
            converter=QuestaWlfConverter(self.preferences.wlf2vcd_path or None)))
        self.themes = themes or ThemeManager()
        self.themes.select(self.preferences.theme if self.preferences.theme in self.themes.themes else next(iter(self.themes.themes)))
        self.bit_expansion = BitExpansionService()
        self.copy_service = CopyValuesService()
        self.highlight_service = HighlightService()
        self.advanced_highlight_service = AdvancedHighlightService()
        self.edge_measurements = EdgeMeasurementService()
        self.measurement_formatter = MeasurementFormatter()
        self.highlights = HighlightModel(self)
        self.copy_period = None
        self.copy_phase = 0
        self.clock_sampling = None  # EdgeSampling chosen from a clock signal, if any
        self.copy_period_banner = None
        self.pending_period_action = None
        self.pending_copy_expression = ""
        self.active_editor = None
        self.refresh_state = None
        self.refresh_bit_styles = None
        self.clipboard = clipboard if clipboard is not None else SystemClipboard()
        self.view = WaveformView(themes=self.themes, menus=menus, source_navigator=source_navigator, preferences=self.preferences)
        self.bookmark_controller = BookmarkController(self.view, parent=self)
        self.bookmark_controller.message.connect(lambda s: self.statusBar().showMessage(s, 5000))
        self.highlight_panel = HighlightPanel(self.highlights, self.view)
        self.view.attach_highlight_panel(self.highlights, self.highlight_panel)
        self.highlight_panel.createRequested.connect(self.create_highlight)
        self.highlight_panel.advancedRequested.connect(lambda: self.create_highlight(mode="advanced"))
        self.highlight_panel.editRequested.connect(self.create_highlight)
        self.themes.changed.connect(self.apply_theme)
        self.apply_theme()
        self.view.findRequested.connect(self.focus_search)
        self.view.expansionRequested.connect(self.expand_bus)
        self.view.copyRequested.connect(self.copy_values)
        self.view.cellCopyRequested.connect(self.copy_cell)
        self.view.highlightRequested.connect(self.create_highlight)
        self.view.advancedHighlightRequested.connect(lambda: self.create_highlight(mode="advanced"))
        self.view.cursorChanged.connect(self.update_status)
        self.view.selectionChanged.connect(self.update_selection)
        self.view.message.connect(lambda s: self.statusBar().showMessage(s, 5000))
        self.view.viewChanged.connect(self.update_status)

        toolbar = QToolBar("Навигация", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)
        self.open_action = self.add_action(toolbar, "Открыть захват", self.choose_file, "Ctrl+O")
        toolbar.addSeparator()
        self.zoom_in_action = self.add_action(toolbar, "+", lambda: self.view.zoom_toolbar(0.5), tooltip="Приблизить относительно середины A↔B")
        self.zoom_out_action = self.add_action(toolbar, "−", lambda: self.view.zoom_toolbar(2), tooltip="Отдалить относительно середины A↔B")
        self.fit_action = self.add_action(toolbar, "Весь захват", self.view.fit, tooltip="F — уместить весь захват")
        self.zoom_cursors_action = self.add_action(toolbar, "A ↔ B", self.view.zoom_cursors, tooltip="Уместить диапазон между курсорами")
        toolbar.addSeparator()
        self.previous_change_action = self.add_action(toolbar, "← Изменение", lambda: self.view.next_transition(-1), tooltip="← — предыдущее изменение выбранного сигнала")
        self.next_change_action = self.add_action(toolbar, "Изменение →", lambda: self.view.next_transition(1), tooltip="→ — следующее изменение выбранного сигнала")
        self.goto_action = self.add_action(toolbar, "К отсчёту…", self.goto_sample, "Ctrl+G")
        # Only Vivado ILA captures carry a TRIGGER column; simulator dumps do not.
        self.trigger_action = self.add_action(toolbar, "Триггер", self.goto_trigger,
                                              tooltip="Перейти к первому TRIGGER = 1")
        self.trigger_action.setVisible(False)
        toolbar_spacer = QWidget()
        toolbar_spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(toolbar_spacer)
        self.refresh_action = self.add_action(toolbar, "Обновить файл", self.refresh_csv, "F5",
                                              tooltip="F5 — повторно открыть текущий захват с диска")
        self.refresh_action.setEnabled(False)
        self.build_menus()

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
        self.search_edit = QLineEdit()
        self.search_edit.setFixedWidth(self.search_edit.fontMetrics().horizontalAdvance("0" * 13) + 12)
        self.search_edit.setPlaceholderText("HEX: 0AC, 100; или 0xAC / 0b1010")
        self.search_edit.setToolTip("Точное значение целой шины. Без префикса — в выбранном формате. Enter: следующее совпадение.")
        self.search_edit.returnPressed.connect(lambda: self.find_value(1))
        search_row.addWidget(self.search_edit)
        # The find buttons sit right next to the value they search for.
        self.previous_button = QPushButton("Найти ←")
        self.next_button = QPushButton("Найти →")
        self.previous_button.setToolTip("Shift+F3 — предыдущее совпадение")
        self.next_button.setToolTip("Enter / F3 — следующее совпадение")
        self.previous_button.clicked.connect(lambda: self.find_value(-1))
        self.next_button.clicked.connect(lambda: self.find_value(1))
        search_row.addWidget(self.previous_button)
        search_row.addWidget(self.next_button)
        self.loop_search = QCheckBox("↻")
        self.loop_search.setToolTip("Кольцевой поиск: после конца продолжать с начала сигнала")
        self.loop_search.setChecked(True)
        search_row.addWidget(self.loop_search)
        search_row.addStretch(1)
        layout.addLayout(search_row)
        self.period_container = QVBoxLayout()
        self.period_container.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.period_container)
        layout.addWidget(self.view, 1)
        self.readout = QLabel("Откройте захват: Vivado ILA CSV, VCD или WLF")
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

    def build_menus(self):
        """Menus group commands by task; toolbar actions are shared, not duplicated."""
        bar = self.menuBar()
        file_menu = bar.addMenu("Файл")
        file_menu.addAction(self.open_action)
        self.recent_menu = file_menu.addMenu("Недавние файлы")
        self.recent_menu.aboutToShow.connect(self.populate_recent_menu)
        file_menu.addAction(self.refresh_action)
        file_menu.addSeparator()
        self.add_action(file_menu, "Сохранить WLF как VCD…", self.export_wlf)
        self.add_action(file_menu, "Сохранить вид как PNG…", self.save_image)
        file_menu.addSeparator()
        self.add_action(file_menu, "Выход", self.close, "Ctrl+Q")

        view_menu = bar.addMenu("Вид")
        for action in (self.zoom_in_action, self.zoom_out_action, self.fit_action, self.zoom_cursors_action):
            view_menu.addAction(action)
        view_menu.addSeparator()
        self.full_names_action = self.add_action(view_menu, "Полные имена сигналов", self.toggle_names)
        self.full_names_action.setCheckable(True)
        self.full_names_action.setChecked(self.preferences.full_names)
        self.add_action(view_menu, "Ширина Name по содержимому", lambda: self.view.auto_fit_column("name"))
        theme_menu = view_menu.addMenu("Тема")
        self.theme_actions = QActionGroup(self)
        for theme in self.themes.themes.values():
            action = self.add_action(theme_menu, theme.label, lambda _checked=False, key=theme.key: self.themes.select(key))
            action.setCheckable(True)
            action.setData(theme.key)
            self.theme_actions.addAction(action)
        self.sync_theme_actions()

        navigation = bar.addMenu("Навигация")
        for action in (self.previous_change_action, self.next_change_action, self.goto_action, self.trigger_action):
            navigation.addAction(action)
        navigation.addSeparator()
        self.add_action(navigation, "Поиск значения", self.focus_search, "Ctrl+F")
        self.add_action(navigation, "Следующее совпадение", lambda: self.find_value(1), "F3")
        self.add_action(navigation, "Предыдущее совпадение", lambda: self.find_value(-1), "Shift+F3")
        navigation.addSeparator()
        self.add_action(navigation, "Убрать курсор B", self.clear_b)
        navigation.addSeparator()
        self.bookmark_controller.install(navigation, self)


        analysis = bar.addMenu("Анализ")
        self.add_action(analysis, "Копировать значения…", lambda: self.copy_values("DEFAULT"))
        self.add_action(analysis, "Подсветить участки по условию…", lambda: self.create_highlight())
        self.add_action(analysis, "Advanced Highlight…", lambda: self.create_highlight(mode="advanced"))
        analysis.addSeparator()
        self.add_action(analysis, "Такт выборки: сигнал clk или маркеры…", self.recalibrate_period,
                        tooltip="Маркеры 1/2 задают шаг, с которым проверяются условия копирования и подсветки")
        self.clock_period_action = self.add_action(analysis, "Шкала времени CSV: такт ILA…", self.set_period)

        settings = bar.addMenu("Настройки")
        self.add_action(settings, "Параметры…", self.show_appearance, "Ctrl+,")

        help_menu = bar.addMenu("Справка")
        self.add_action(help_menu, "Управление", self.show_help, "F1")
        self.add_action(help_menu, "О программе", self.show_about)

    def sync_theme_actions(self):
        if not hasattr(self, "theme_actions"):
            return
        for action in self.theme_actions.actions():
            action.setChecked(action.data() == self.themes.current.key)

    def populate_recent_menu(self):
        self.recent_menu.clear()
        recent = self.preferences.recent_files
        for index, path in enumerate(recent, 1):
            action = self.recent_menu.addAction(f"&{index % 10}  {Path(path).name}")
            action.setToolTip(path)
            action.setStatusTip(path)
            action.triggered.connect(lambda _checked=False, p=path: self.open_recent(p))
        if recent:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("Очистить список", self.clear_recent_files)
        else:
            self.recent_menu.addAction("Нет недавних файлов").setEnabled(False)

    def open_recent(self, path):
        if not Path(path).is_file():
            self.preferences.recent_files = [p for p in self.preferences.recent_files if p != path]
            self.save_timer.start()
            self.statusBar().showMessage(f"Файл не найден: {path}", 6000)
            return
        self.open_file(path)

    def remember_recent(self, path):
        path = str(Path(path).resolve())
        recent = [path] + [p for p in self.preferences.recent_files if p != path]
        self.preferences.recent_files = recent[:PreferencesStore.RECENT_LIMIT]
        self.save_timer.start()

    def clear_recent_files(self):
        self.preferences.recent_files = []
        self.save_timer.start()

    def recalibrate_period(self):
        if not self.capture:
            self.statusBar().showMessage("Сначала откройте захват.", 5000)
            return
        if self.worker or self.copy_period_banner is not None or self.active_editor is not None:
            self.statusBar().showMessage("Дождитесь окончания текущей операции.", 5000)
            return
        self.start_period_setup(continuation=lambda: None)

    def show_about(self):
        QMessageBox.about(self, f"О программе {APP_NAME}",
                          f"<b>{APP_NAME}</b><br>Офлайн-просмотр временных диаграмм: "
                          "Vivado ILA CSV, VCD и Questa/ModelSim WLF (через wlf2vcd).<br><br>"
                          "Apache License 2.0")

    def focus_search(self):
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def copy_values(self, radix="DEFAULT"):
        if self.active_editor is not None:
            self.active_editor.raise_()
            self.active_editor.activateWindow()
            return
        if self.copy_period_banner is not None:
            self.statusBar().showMessage("Сначала подтвердите или отмените настройку периода.", 5000)
            return
        if self.worker:
            self.statusBar().showMessage("Дождитесь завершения операции или нажмите «Отмена».", 4000)
            return
        try:
            self.copy_service.snapshot(self.view, radix)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 7000)
            return
        if self.current_sampling() is None:
            if self.copy_period_banner is not None:
                return
            self.start_period_setup(radix)
            return
        compiler = self.condition_compiler()
        selected = self.copy_service.selected_targets(self.view)
        selected_ids = {node.id for node, _ in selected}
        available = [(node, None) for node in self.condition_nodes() if node.id not in selected_ids]
        dialog = CopyOptionsDialog(self.condition_signals(), compiler,
                                   selected, self.copy_service,
                                   self, full_names=self.view.full_names,
                                   expression_text=self.pending_copy_expression,
                                   separator=self.preferences.copy_separator,
                                   available_targets=available)
        self.show_editor(dialog, lambda accepted: self.finish_copy_dialog(dialog, compiler, radix, accepted))

    def show_editor(self, dialog, callback):
        self.active_editor = dialog
        source_capture = self.capture
        dialog.setModal(False)
        def finished(result):
            self.active_editor = None
            if not self.closing and self.capture is source_capture:
                callback(result == QDialog.DialogCode.Accepted)
            dialog.deleteLater()
        dialog.finished.connect(finished)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def finish_copy_dialog(self, dialog, compiler, radix, accepted):
        if not accepted:
            if dialog.reselect_period:
                self.pending_copy_expression = dialog.condition_text()
                self.start_period_setup(radix)
            return
        self.pending_copy_expression = ""
        try:
            request = self.copy_service.snapshot(
                self.view, radix, compiler.compile(dialog.condition_text()),
                self.current_sampling(), 0, dialog.channel_options(),
                dialog.separator_box.currentData(),
                "unique" if dialog.unique_box.isChecked() else "all",
                dialog.sort_box.currentData(), dialog.selected_targets())
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

    def copy_cell(self, text):
        self.clipboard.write(text)
        self.statusBar().showMessage("Скопировано в буфер обмена.", 3000)

    def condition_nodes(self):
        """Signal rows offered by editors: whole buses, never their split VCD bits."""
        nodes = []
        def visit(node):
            if node.signal and node.kind != "bit":
                nodes.append(node)
            for child in node.children:
                visit(child)
        visit(self.view.tree.root)
        return nodes

    def condition_signals(self):
        return [node.signal.source for node in self.condition_nodes()]

    def condition_compiler(self):
        radix_by_name = {}
        stack = list(self.view.tree.root.children)
        while stack:
            node = stack.pop()
            if node.signal:
                radix_by_name[node.signal.source.name] = self.view.display_radix(node.signal)
            stack.extend(node.children)
        return ConditionCompiler(self.capture.signals, radix_by_name)

    def create_highlight(self, group=None, draft=None, mode="simple"):
        if isinstance(group, bool):
            group = None
        if self.active_editor is not None:
            self.active_editor.raise_()
            self.active_editor.activateWindow()
            return
        if self.worker or self.copy_period_banner is not None:
            self.statusBar().showMessage("Дождитесь окончания текущей операции.", 5000)
            return
        if not self.capture:
            self.statusBar().showMessage("Откройте захват для подсветки.", 6000)
            return
        if self.current_sampling() is None:
            self.start_period_setup(continuation=lambda: self.create_highlight(group, draft, mode))
            return
        compiler = self.condition_compiler()
        if group is None and draft is None:
            index = len(self.highlights.groups)
            draft = {"name": f"Подсветка {index + 1}",
                     "style": index % len(self.preferences.highlight_palette), "mode": mode}
        dialog = HighlightConditionDialog(self.condition_signals(), compiler,
                                          self.preferences.highlight_palette,
                                          self.preferences.highlight_opacities,
                                          self, group, draft,
                                          full_names=self.view.full_names, mode=mode)
        self.show_editor(dialog, lambda accepted: self.finish_highlight_dialog(dialog, compiler, group, accepted))

    def finish_highlight_dialog(self, dialog, compiler, group, accepted):
        if not accepted:
            if dialog.reselect_period:
                new_draft = dialog.draft()
                self.start_period_setup(continuation=lambda: self.create_highlight(group, new_draft, dialog.mode))
            return
        try:
            condition = (compiler.compile_pattern(dialog.condition_text()) if dialog.mode == "advanced"
                         else compiler.compile(dialog.condition_text()))
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 7000)
            return
        marker_a = group.marker_a if group else (self.view.cursor_a if self.view.cursor_b is not None else 0)
        marker_b = group.marker_b if group else (self.view.cursor_b if self.view.cursor_b is not None else self.capture.count)
        period, phase = legacy_period(self.current_sampling())
        name, expression, color, opacity, highlight_mode = (dialog.name_edit.text().strip(), dialog.condition_text(),
                                                            dialog.color, dialog.opacity, dialog.mode)

        def complete(intervals):
            new_group = HighlightGroup(name, expression, color, intervals, marker_a, marker_b,
                                       period, phase, opacity=opacity, mode=highlight_mode)
            if group and group in self.highlights.groups:
                self.highlights.replace(group, new_group)
            elif group:
                intervals.close()
                return
            else:
                self.highlights.add(new_group)
            self.statusBar().showMessage(f"Подсветка «{name}»: {intervals.count:,} участков.", 6000)

        service = self.advanced_highlight_service if highlight_mode == "advanced" else self.highlight_service
        self.start_work(lambda task: service.build(
            self.capture, condition, marker_a, marker_b, period, phase,
            task.progress.emit, task.isInterruptionRequested), complete,
            "Поиск участков для подсветки…", "проверенных периодов")

    def start_period_setup(self, radix="DEFAULT", continuation=None):
        if not self.view.begin_period_setup():
            self.statusBar().showMessage("Для задания периода нужны хотя бы два отсчёта.", 6000)
            return
        self.pending_period_action = continuation or (lambda: self.copy_values(radix))
        banner = CopyPeriodBanner(self.view, self, signals=self.condition_signals(),
                                  current=self.clock_sampling, edge=self.preferences.clock_edge,
                                  before_edge=self.preferences.clock_before_edge,
                                  sample_period_ns=self.view.period_ns)
        self.copy_period_banner = banner
        banner.confirmed.connect(self.finish_period_setup)
        banner.clockConfirmed.connect(self.finish_clock_setup)
        banner.cancelled.connect(self.cancel_period_setup)
        self.period_container.addWidget(banner)
        self.view.setFocus()
        self.statusBar().showMessage("Выбор такта: сигнал clk в списке или маркеры (ЛКМ — точка 1, Shift+ЛКМ — точка 2); затем подтвердите.", 8000)

    def clear_period_banner(self):
        banner, self.copy_period_banner = self.copy_period_banner, None
        if banner:
            banner.detach()
            self.period_container.removeWidget(banner)
            banner.deleteLater()
        self.view.end_period_setup()

    def cancel_period_setup(self):
        self.clear_period_banner()
        self.pending_period_action = None
        self.statusBar().showMessage("Настройка периода отменена.", 5000)

    def current_sampling(self):
        """The grid copy and highlight sample on: clock edges, else markers 1/2, else None."""
        if self.clock_sampling is not None:
            return self.clock_sampling
        if self.copy_period is not None:
            return PeriodicSampling(self.copy_period, self.copy_phase)
        return None

    def apply_banner_time_base(self):
        banner = self.copy_period_banner
        try:
            period = banner.sample_period_ns() if banner else None
        except ValueError:
            period = None
        if period:
            self.set_sample_period(period)

    def finish_period_setup(self, period, phase):
        self.apply_banner_time_base()
        self.copy_period = period
        self.copy_phase = phase
        self.clock_sampling = None
        action, self.pending_period_action = self.pending_period_action, None
        self.clear_period_banner()
        self.statusBar().showMessage(f"Период выборки: {self.copy_period:,} отсчётов.", 5000)
        if action:
            action()

    def finish_clock_setup(self, sampling):
        self.apply_banner_time_base()
        self.clock_sampling = sampling
        self.preferences.clock_edge = sampling.edge
        self.preferences.clock_before_edge = sampling.before_edge
        self.save_timer.start()
        action, self.pending_period_action = self.pending_period_action, None
        self.clear_period_banner()
        self.statusBar().showMessage(f"Выборка по такту: {sampling.describe()}.", 6000)
        if action:
            action()

    def apply_theme(self):
        self.setStyleSheet(self.themes.current.stylesheet())
        self.preferences.theme = self.themes.current.key
        self.sync_theme_actions()
        self.save_timer.start()

    def sync_filter_width(self):
        # Leave room for the value search, both find buttons and the loop toggle.
        self.filter_edit.setFixedWidth(min(self.view.name_width + 1, max(100, self.width() - 420)))

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
        if isinstance(self.source, AutoCaptureSource):
            converter = self.source.readers[".wlf"].converter
            if isinstance(converter, QuestaWlfConverter):
                converter.executable = preferences.wlf2vcd_path or None
        self.view.preferences = preferences
        self.view.full_names = preferences.full_names
        self.view.period_ns = preferences.sample_period_ns
        self.full_names_action.setChecked(preferences.full_names)
        self.view.refresh_rows()
        self.themes.select(preferences.theme)
        self.view.layout_highlight_panel()
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
            if self.refresh_bit_styles:
                for child in node.children:
                    self.refresh_bit_styles.apply_style(child)
            self.view.refresh_rows(node)
            self.statusBar().showMessage(f"Раскрыта шина: {node.label}", 4000)
        self.start_work(lambda task: self.bit_expansion.build(node.signal.source, self.capture.storage.name,
                                                              task.progress.emit, task.isInterruptionRequested),
                        complete, f"Подготовка битов {node.label}…", "битов")

    def choose_file(self):
        initial = self.preferences.last_csv_directory or str(Path.cwd())
        path, _ = QFileDialog.getOpenFileName(self, "Открыть захват", initial,
                                              "Захваты (*.csv *.vcd *.wlf);;Vivado CSV (*.csv);;VCD (*.vcd);;Questa WLF (*.wlf);;Все файлы (*)")
        if path:
            self.preferences.last_csv_directory = str(Path(path).resolve().parent)
            self.save_timer.start()
            self.open_file(path)

    def export_wlf(self):
        if self.worker:
            self.statusBar().showMessage("Дождитесь окончания текущей операции.", 5000)
            return
        initial = str(self.capture.path) if self.capture and self.capture.path.suffix.lower() == ".wlf" else self.preferences.last_csv_directory
        source, _ = QFileDialog.getOpenFileName(self, "Выбрать WLF", initial, "Questa WLF (*.wlf)")
        if not source:
            return
        destination, _ = QFileDialog.getSaveFileName(self, "Сохранить VCD", str(Path(source).with_suffix(".vcd")),
                                                      "VCD (*.vcd)")
        if not destination:
            return
        if Path(destination).suffix.lower() != ".vcd":
            destination += ".vcd"
        converter = QuestaWlfConverter(self.preferences.wlf2vcd_path or None)
        service = WlfExportService(converter)
        self.start_work(lambda task: service.export(source, destination, task.isInterruptionRequested),
                        lambda result: self.open_file(result),
                        "Конвертация WLF в VCD…", "отсчётов")

    def refresh_csv(self):
        if self.capture:
            self.open_file(self.capture.path, refresh=True)

    def open_file(self, path, refresh=False):
        if self.worker:
            self.statusBar().showMessage("Дождитесь завершения операции или нажмите «Отмена».", 4000)
            return
        self.refresh_state = (CaptureRefreshState.capture(self.view, self.highlights, self.condition_compiler())
                              if refresh and self.capture else None)
        self.start_work(lambda task: self.source.load(path, task.progress.emit, task.isInterruptionRequested),
                        self.loaded, f"Загрузка {Path(path).name}…")

    def start_work(self, work, complete, message, progress_unit="отсчётов"):
        if self.worker:
            return
        task = BackgroundTask(work, self)
        self.worker = task
        self.progress_unit = progress_unit
        self.open_action.setEnabled(False)
        self.refresh_action.setEnabled(False)
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
        self.refresh_action.setEnabled(self.capture is not None)
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
        if self.active_editor is not None:
            self.active_editor.reject()
        if self.copy_period_banner is not None:
            self.cancel_period_setup()
        state, self.refresh_state = self.refresh_state, None
        if state is None:
            self.copy_period = None
            self.copy_phase = 0
            self.clock_sampling = None
            self.refresh_bit_styles = None
        else:
            self.refresh_bit_styles = state
            # The clock follows its signal by name; without it, ask for the clock again.
            self.clock_sampling = self.clock_sampling.rebind(capture) if self.clock_sampling else None
        self.pending_copy_expression = ""
        old = self.capture
        self.highlights.reset()
        if state is None:
            self.view.bookmarks.clear()
        else:
            self.view.bookmarks.clamp(capture.count)
        self.capture = capture
        self.preferences.last_csv_directory = str(capture.path.resolve().parent)
        self.save_timer.start()
        self.refresh_action.setEnabled(True)
        self.filter_edit.clear()
        self.view.set_capture(capture)
        if state:
            state.apply_styles(self.view)
            self.highlights.master_enabled = state.master_enabled
            self.highlights.changed.emit()
        if old:
            old.close()
        self.setWindowTitle(f"{capture.path.name} — {APP_NAME}")
        self.remember_recent(capture.path)
        self.trigger_action.setVisible(self.trigger_signal() is not None)
        # Simulator dumps carry real timestamps; the ILA clock period only applies to CSV rows.
        self.clock_period_action.setEnabled(capture.times is None)
        # Bits of a split VCD bus are already counted by the rebuilt bus.
        split_bits = {id(bit) for bits in capture.split_buses.values() for bit in bits}
        shown = [sig for sig in capture.signals if id(sig) not in split_bits]
        changes = sum(len(sig.starts) - 1 for sig in shown)
        self.statusBar().showMessage(f"{capture.count:,} отсчётов · {len(shown)} сигналов · {changes:,} изменений")
        self.view.setFocus()
        if state and state.highlights:
            compiler = self.condition_compiler()
            def restored(groups):
                for group in groups:
                    self.highlights.add(group)
                self.statusBar().showMessage(f"Захват обновлён · восстановлено подсветок: {len(groups)}", 6000)
            self.start_work(lambda task: state.rebuild_highlights(
                capture, compiler, self.highlight_service, self.advanced_highlight_service,
                task.progress.emit, task.isInterruptionRequested),
                restored, "Восстановление подсветок после обновления файла…", "подсветок")

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
        axis = v.time_axis()
        parts = [f"A: {v.cursor_a:,}"]
        if axis is not None:
            parts.append(f"tA: {axis.readout(v.cursor_a)}")
        if v.cursor_b is not None:
            parts.extend([f"B: {v.cursor_b:,}", f"Δ: {abs(v.cursor_b - v.cursor_a):,} отсч."])
            if axis is not None:
                parts.append(f"Δt: {axis.delta(v.cursor_a, v.cursor_b)}")
            sig = v.current_signal
            measurement = self.edge_measurements.measure(sig, v.cursor_a, v.cursor_b)
            parts.extend(self.measurement_formatter.parts(measurement, self.capture, v.period_ns,
                                                          sig.short_name if sig else ""))
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
        wrap = self.loop_search.isChecked()
        self.start_work(lambda task: sig.search(value, sample, direction, task.isInterruptionRequested,
                                                wrap),
                        self.found, f"Поиск в {sig.short_name}…")

    def found(self, result):
        if result is None:
            self.statusBar().showMessage("Значение не найдено в выбранном сигнале.", 6000)
            return
        sample, wrapped = result
        self.view.set_cursor(sample)
        self.search_edit.setFocus()
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

    def trigger_signal(self):
        if not self.capture:
            return None
        return next((s for s in self.capture.signals if s.name.upper() == "TRIGGER"), None)

    def goto_trigger(self):
        if not self.capture or self.worker:
            return
        sig = self.trigger_signal()
        if not sig:
            self.statusBar().showMessage("В файле нет столбца TRIGGER.", 5000)
            return
        self.start_work(lambda task: sig.search(1, -1, 1, task.isInterruptionRequested),
                        lambda result: self.found(result) if result else self.statusBar().showMessage("В захвате нет TRIGGER=1.", 5000),
                        "Поиск триггера…")

    def set_period(self):
        current = f"{self.view.period_ns:g} ns" if self.view.period_ns else ""
        text, ok = QInputDialog.getText(self, "Шкала времени CSV",
                                        "Такт выборки ILA: частота (100 MHz) или период (10 ns).\n"
                                        "Пусто — ось только в отсчётах. Значение сохраняется.", text=current)
        if not ok:
            return
        try:
            self.set_sample_period(parse_clock(text) if text.strip() else 0.0)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc), 7000)

    def set_sample_period(self, period_ns):
        """ILA sample period in ns; a general preference, since it describes the ILA setup."""
        self.view.period_ns = period_ns
        self.preferences.sample_period_ns = period_ns
        self.save_timer.start()
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
        QMessageBox.information(self, "Управление", """Щелчок по waveform — выбрать сигнал и установить курсор A.
Ctrl + щелчок по waveform — добавить или убрать сигнал из выделения.
Положение маркера округляется к ближайшему отсчёту.
Shift + щелчок — курсор B; обычный щелчок по диаграмме — убрать B.
Shift + щелчок в Name / Value — выделить диапазон строк без изменения B.
Radix, Signal Color и перетаскивание применяются ко всему выделению.
Протянуть ЛКМ по диаграмме — приблизить диапазон (от 2 отсчётов).
Перетаскивание за линию маркера — переместить маркер; для B удерживать Shift.
Copy Values… — для каждого сигнала свой Radix и диапазон битов; без B весь захват.
Условие поддерживает &&, ||, ! / ~, сравнения и выбор сигнала из списка.
При первом копировании передвиньте маркеры 1/2 и подтвердите период на панели.
Во время выбора периода ЛКМ двигает 1, Shift+ЛКМ — 2.
В окне условия можно выбрать период заново; B — исключающая граница.
Такт выборки — сигнал clk (передний, задний или оба фронта) или маркеры 1/2.
По сигналу такта выборка идёт только по реальным фронтам (подходит для SPI SCK);
«до фронта» берёт значения, которые видит регистр в момент фронта.
Частота такта в панели выбора задаёт шкалу времени CSV; её можно изменить в
«Анализ → Шкала времени CSV» и в настройках, значение сохраняется.
Reverse Bit Order переставляет и строки раскрытых битов (отключается в настройках).
Правый щелчок в Name/Value: Copy Name / Copy Value для текущей ячейки.
Подсветить участки по условию — группы под диаграммой с отдельной прозрачностью.
В окне копирования доступны разделитель и уникальные значения с сортировкой.
Name и Value имеют независимые границы; Value расширяется при длинных значениях.
Общий Radix, тема, заливка и стили подсветки находятся в «Настройки → Параметры…» (Ctrl+,).
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
«Триггер» появляется, только если в захвате есть столбец TRIGGER.
«Файл → Недавние файлы» открывает один из последних захватов.
Ctrl+F — поиск; Enter / F3 — вперёд; Shift+F3 — назад.
Ctrl+G — перейти к индексу отсчёта; Ctrl+O — открыть CSV, VCD или WLF.
Ctrl+M — именованный маркер в позиции A; Alt + ← / → — предыдущий / следующий маркер.
«Навигация → Маркеры…» — список маркеров: переход, переименование, удаление.
Для однобитного сигнала при установленном B строка состояния показывает число
фронтов ↑ между A и B и оценку периода/частоты (для VCD — в реальном времени).
Ctrl+A после Name/Value — все видимые сигналы; после waveform — A=начало, B=конец.
«Обновить файл» (F5) перечитывает захват, сохраняя оформление совпавших сигналов и действительные подсветки.
Перетаскивание границы столбцов — ширина области имён.
Правый щелчок — формат значения выбранного сигнала.

Поиск: точное значение целой шины; результат — начало интервала.
Без префикса используется выбранный HEX/UNSIGNED/SIGNED/BINARY.
Префиксы 0x и 0b задают формат явно. Галочка ↻ включает кольцевой поиск.
Плотные изменения на общем виде показаны заполненной полосой.
Приблизьте участок, чтобы увидеть отдельные переходы.

Для CSV ось X — порядок строк, начиная с 0. Sample in Buffer/Window
показываются отдельно. Время вычисляется только из введённого
периода: при захвате с пропусками это не реальное время.
Для VCD/WLF ось пропорциональна времени симуляции; индекс отсчёта означает
порядковый номер временной отметки с событиями. WLF требует wlf2vcd из Questa/ModelSim.
«Файл → Сохранить WLF как VCD…» создаёт VCD для просмотра без Questa.""")

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
        self.closing = True
        if self.active_editor is not None:
            self.active_editor.reject()
        self.save_preferences()
        self.highlights.reset()
        if self.capture:
            self.view.capture = None
            self.view.auto_scroll.stop()
            self.capture.close()
            self.capture = None
        event.accept()


