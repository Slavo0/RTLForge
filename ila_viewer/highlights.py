# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Capture-scoped, disk-backed intervals for conditional waveform highlighting."""

from dataclasses import dataclass
import os
from pathlib import Path
import struct
import tempfile
from uuid import uuid4

import numpy as np
from PySide6.QtCore import QObject, Signal

from .data import LoadCancelled


class HighlightIntervals:
    def __init__(self, path=None, count=0):
        self.path = Path(path) if path else None
        self.count = count
        self.data = np.memmap(self.path, dtype="<i8", mode="r", shape=(count, 2)) if count else np.empty((0, 2), dtype="<i8")

    def visible(self, start, end, pixels=None):
        if not self.count:
            return
        first = int(np.searchsorted(self.data[:, 1], start, side="right"))
        stop = int(np.searchsorted(self.data[:, 0], end, side="left"))
        if pixels and stop - first > pixels * 2:
            edges = np.linspace(start, end, max(1, pixels) + 1)
            indices = np.searchsorted(self.data[:, 1], edges[:-1], side="right")
            valid = indices < self.count
            active = np.zeros(len(indices), dtype=bool)
            active[valid] = self.data[indices[valid], 0] < edges[1:][valid]
            starts = np.flatnonzero(np.diff(np.r_[False, active, False].astype(np.int8)) == 1)
            ends = np.flatnonzero(np.diff(np.r_[False, active, False].astype(np.int8)) == -1)
            for left, right in zip(starts, ends):
                yield float(edges[left]), float(edges[right])
            return
        for index in range(first, stop):
            left, right = self.data[index]
            yield max(float(left), start), min(float(right), end)

    def close(self):
        if isinstance(self.data, np.memmap):
            self.data._mmap.close()
        self.data = np.empty((0, 2), dtype="<i8")
        if self.path:
            self.path.unlink(missing_ok=True)
            self.path = None
        self.count = 0


@dataclass
class HighlightGroup:
    name: str
    expression: str
    color: str
    intervals: HighlightIntervals
    marker_a: int
    marker_b: int
    period: int
    phase: int
    visible: bool = True
    id: str = ""
    opacity: int = 28
    mode: str = "simple"

    def __post_init__(self):
        if not self.id:
            self.id = uuid4().hex


class HighlightModel(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.groups = []
        self.master_enabled = True

    def set_master_enabled(self, enabled, restore_visibility=True):
        if self.master_enabled == enabled:
            return
        self.master_enabled = enabled
        if enabled and not restore_visibility:
            for group in self.groups:
                group.visible = True
        self.changed.emit()

    def add(self, group):
        self.groups.append(group)
        self.changed.emit()

    def replace(self, old, new):
        index = self.groups.index(old)
        new.id = old.id
        new.visible = old.visible
        self.groups[index] = new
        old.intervals.close()
        self.changed.emit()

    def remove(self, group):
        self.groups.remove(group)
        group.intervals.close()
        self.changed.emit()

    def reset(self):
        for group in self.groups:
            group.intervals.close()
        self.groups.clear()
        self.master_enabled = True
        self.changed.emit()


class HighlightService:
    """Evaluate one condition at each period phase without touching CSV samples."""

    def build(self, capture, condition, marker_a, marker_b, period, phase,
              progress=lambda percent, rows: None, cancel=lambda: False):
        period = max(1, int(period))
        lower, upper = sorted((marker_a, marker_b))
        first = lower + ((phase - lower) % period)
        path = None
        count = 0
        pending = None
        try:
            with tempfile.NamedTemporaryFile(dir=capture.storage.name, prefix="highlight_", suffix=".bin", delete=False) as stream:
                path = Path(stream.name)
                for index, sample in enumerate(range(first, upper + 1, period)):
                    if index % 4096 == 0:
                        if cancel():
                            raise LoadCancelled()
                        progress(round(100 * (sample - lower) / max(1, upper - lower)), index)
                    previous = sample - period if sample >= period else None
                    if sample == marker_b or (condition is not None and not condition.matches(sample, previous)):
                        continue
                    start = max(sample, lower + (marker_b == lower))
                    end = min(sample + period, capture.count, upper + (marker_b != upper))
                    if end <= start:
                        continue
                    if pending and start <= pending[1]:
                        pending = pending[0], max(pending[1], end)
                    else:
                        if pending:
                            stream.write(struct.pack("<qq", *pending))
                            count += 1
                        pending = start, end
                if pending:
                    stream.write(struct.pack("<qq", *pending))
                    count += 1
            progress(100, count)
            if not count:
                path.unlink(missing_ok=True)
                return HighlightIntervals()
            return HighlightIntervals(path, count)
        except BaseException:
            if path:
                path.unlink(missing_ok=True)
            raise
