# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Presentation-only annotations drawn on top of signal rows (decoded bytes, clock arrows).

Owners (for example the SPI controller) register annotations keyed by the source
signal name and clear them when their result goes away. The registry never touches
samples; the renderer asks it what to draw for each visible signal row.
"""
from __future__ import annotations


class SignalAnnotations:
    def __init__(self, changed=lambda: None):
        self._items = {}   # owner -> [(signal name, annotation)]
        self.enabled = True
        self.changed = changed

    def set(self, owner, items):
        items = list(items)
        if items:
            self._items[owner] = items
        else:
            self._items.pop(owner, None)
        self.changed()

    def clear(self, owner):
        if self._items.pop(owner, None) is not None:
            self.changed()

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.changed()

    def for_signal(self, signal):
        """Annotations for a displayed signal (matched by its source name)."""
        if not self.enabled or not self._items or signal is None:
            return []
        source = getattr(signal, "source", signal)
        name = getattr(source, "name", None)
        return [annotation for items in self._items.values() for key, annotation in items if key == name]
