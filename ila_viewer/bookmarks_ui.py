# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Menu commands and a nonmodal manager for named bookmarks on the waveform."""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QListWidget, QListWidgetItem,
                               QPushButton, QVBoxLayout)


class BookmarkManagerDialog(QDialog):
    """Lists bookmarks; double-click jumps, F2 or the button renames inline, Delete removes."""

    def __init__(self, model, controller, parent=None):
        super().__init__(parent)
        self.model = model
        self.controller = controller
        self.setWindowTitle("Маркеры")
        self.setModal(False)
        self.resize(360, 320)
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.setEditTriggers(QListWidget.EditTrigger.EditKeyPressed | QListWidget.EditTrigger.SelectedClicked)
        self.list.itemDoubleClicked.connect(lambda item: self.controller.jump(item.data(Qt.ItemDataRole.UserRole)))
        self.list.itemChanged.connect(self._renamed)
        layout.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        self.jump_button = QPushButton("Перейти")
        self.jump_button.clicked.connect(lambda: self.controller.jump(self.current_id()))
        self.rename_button = QPushButton("Переименовать")
        self.rename_button.clicked.connect(self._edit_current)
        self.delete_button = QPushButton("Удалить")
        self.delete_button.clicked.connect(lambda: self.model.remove(self.current_id()))
        self.clear_button = QPushButton("Удалить все")
        self.clear_button.clicked.connect(self.model.clear)
        for button in (self.jump_button, self.rename_button, self.delete_button, self.clear_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        delete = QAction(self)
        delete.setShortcut(Qt.Key.Key_Delete)
        delete.triggered.connect(lambda: self.model.remove(self.current_id()))
        self.list.addAction(delete)
        self.list.currentRowChanged.connect(lambda _row: self._sync_buttons())
        model.changed.connect(self.reload)
        self.reload()

    def current_id(self):
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def reload(self):
        selected = self.current_id()
        self.list.blockSignals(True)
        self.list.clear()
        for bookmark in self.model.items:
            item = QListWidgetItem(bookmark.name)
            item.setData(Qt.ItemDataRole.UserRole, bookmark.id)
            item.setToolTip(self.controller.describe(bookmark))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.list.addItem(item)
            if bookmark.id == selected:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)
        self._sync_buttons()

    def _sync_buttons(self):
        selected = self.current_id() is not None
        for button in (self.jump_button, self.rename_button, self.delete_button):
            button.setEnabled(selected)
        self.clear_button.setEnabled(len(self.model) > 0)

    def _edit_current(self):
        item = self.list.currentItem()
        if item:
            self.list.editItem(item)

    def _renamed(self, item):
        bookmark_id = item.data(Qt.ItemDataRole.UserRole)
        if not self.model.rename(bookmark_id, item.text()):
            self.reload()  # Empty names are rejected; restore the stored name.


class BookmarkController(QObject):
    """Owns bookmark commands; the window only installs them into a menu."""

    message = Signal(str)

    def __init__(self, view, model=None, parent=None):
        super().__init__(parent)
        self.view = view
        self.model = model if model is not None else view.bookmarks
        self.dialog = None
        self.actions = []

    def install(self, menu, window):
        def action(text, callback, shortcut=None):
            item = QAction(text, window)
            if shortcut:
                item.setShortcut(shortcut)
                item.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            item.triggered.connect(callback)
            menu.addAction(item)
            self.actions.append(item)
            return item
        self.add_action = action("Поставить маркер в A", lambda: self.add_at_cursor(), "Ctrl+M")
        self.previous_action = action("Предыдущий маркер", lambda: self.step(-1), "Alt+Left")
        self.next_action = action("Следующий маркер", lambda: self.step(1), "Alt+Right")
        self.manage_action = action("Маркеры…", lambda: self.show_manager(window))

    def describe(self, bookmark):
        capture = self.view.capture
        if capture is not None and capture.times is not None:
            return f"Отсчёт {bookmark.sample:,} · {capture.format_time(bookmark.sample)}"
        return f"Отсчёт {bookmark.sample:,}"

    def add_at_cursor(self, name=None):
        if self.view.capture is None:
            self.message.emit("Сначала откройте захват.")
            return None
        sample = self.view.cursor_a
        existing = self.model.at(sample)
        if existing is not None:
            self.message.emit(f"Маркер {existing.name} уже стоит на отсчёте {sample:,}.")
            return existing
        bookmark = self.model.add(sample, name)
        self.message.emit(f"Маркер {bookmark.name} · отсчёт {sample:,}")
        return bookmark

    def step(self, direction):
        if self.view.capture is None:
            return None
        sample = self.view.cursor_a
        bookmark = self.model.next_after(sample) if direction > 0 else self.model.previous_before(sample)
        if bookmark is None:
            self.message.emit("Дальше маркеров нет." if len(self.model) else "Маркеров нет: Ctrl+M ставит маркер в A.")
            return None
        self.jump(bookmark.id)
        return bookmark

    def jump(self, bookmark_id):
        bookmark = self.model.get(bookmark_id)
        if bookmark is None or self.view.capture is None:
            return
        self.view.set_cursor(bookmark.sample)
        self.message.emit(f"{bookmark.name} · {self.describe(bookmark)}")

    def show_manager(self, parent=None):
        if self.dialog is None:
            self.dialog = BookmarkManagerDialog(self.model, self, parent or self.view)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()
        return self.dialog
