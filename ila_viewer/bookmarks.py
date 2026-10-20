# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Named sample bookmarks: capture-scoped presentation state, never preferences or samples."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal


@dataclass
class Bookmark:
    id: int
    name: str
    sample: int


class BookmarkModel(QObject):
    """Ordered named markers beyond A/B. Changes emit ``changed`` for redraws and lists."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: dict[int, Bookmark] = {}
        self._next_id = 1

    @property
    def items(self):
        return sorted(self._items.values(), key=lambda item: (item.sample, item.id))

    def __len__(self):
        return len(self._items)

    def get(self, bookmark_id):
        return self._items.get(bookmark_id)

    def at(self, sample):
        return next((item for item in self.items if item.sample == sample), None)

    def add(self, sample, name=None):
        bookmark_id = self._next_id
        self._next_id += 1
        name = (name or "").strip() or f"M{bookmark_id}"
        bookmark = Bookmark(bookmark_id, name, int(sample))
        self._items[bookmark_id] = bookmark
        self.changed.emit()
        return bookmark

    def rename(self, bookmark_id, name):
        bookmark = self._items.get(bookmark_id)
        name = (name or "").strip()
        if bookmark is None or not name or bookmark.name == name:
            return False
        bookmark.name = name
        self.changed.emit()
        return True

    def move(self, bookmark_id, sample):
        bookmark = self._items.get(bookmark_id)
        if bookmark is None or bookmark.sample == int(sample):
            return False
        bookmark.sample = int(sample)
        self.changed.emit()
        return True

    def remove(self, bookmark_id):
        if self._items.pop(bookmark_id, None) is None:
            return False
        self.changed.emit()
        return True

    def clear(self):
        """Forget every bookmark and restart default numbering (another file was opened)."""
        had_items = bool(self._items)
        self._items.clear()
        self._next_id = 1
        if had_items:
            self.changed.emit()

    def clamp(self, count):
        """Keep bookmarks after a refresh, limited to the last sample of the new capture."""
        if count <= 0:
            self.clear()
            return
        changed = False
        for bookmark in self._items.values():
            if bookmark.sample > count - 1:
                bookmark.sample = count - 1
                changed = True
        if changed:
            self.changed.emit()

    def next_after(self, sample):
        return next((item for item in self.items if item.sample > sample), None)

    def previous_before(self, sample):
        return next((item for item in reversed(self.items) if item.sample < sample), None)

    def visible(self, left, right):
        return [item for item in self.items if left <= item.sample <= right]
