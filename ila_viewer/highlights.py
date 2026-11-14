# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Capture-scoped, disk-backed intervals for conditional waveform highlighting."""

from dataclasses import dataclass, field
from pathlib import Path
import tempfile
from uuid import uuid4

import numpy as np
from PySide6.QtCore import QObject, Signal

from .data import LoadCancelled, search_sorted
from .sampling import as_sampling


class HighlightIntervals:
    def __init__(self, path=None, count=0):
        self.path = Path(path) if path else None
        self.count = count
        self.data = np.memmap(self.path, dtype="<i8", mode="r", shape=(count, 2)) if count else np.empty((0, 2), dtype="<i8")

    def visible(self, start, end, pixels=None, edges=None):
        """Intervals intersecting [start, end); merged per pixel when there are too many.

        ``edges`` are the sample positions of the plot's pixel boundaries. Simulator
        captures are drawn on their time axis, where equal row counts are not equal widths,
        so binning must follow those boundaries rather than an even split of rows.
        """
        if not self.count:
            return
        # Columns of the (count, 2) map are strided; search_sorted bisects them in place.
        first = search_sorted(self.data[:, 1], start, "right")
        stop = search_sorted(self.data[:, 0], end, "left")
        if pixels and stop - first > pixels * 2:
            if edges is None:
                edges = np.linspace(start, end, max(1, pixels) + 1)
            edges = np.asarray(edges, dtype=np.float64)
            indices = search_sorted(self.data[:, 1], edges[:-1], "right")
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

    def interval(self, index):
        start, end = self.data[index]
        return int(start), int(end)

    def index_at(self, sample):
        """Index of the interval containing ``sample``, or None."""
        if not self.count:
            return None
        index = search_sorted(self.data[:, 0], sample, "right") - 1
        return index if index >= 0 and sample < self.data[index, 1] else None

    def step(self, sample, direction, wrap=True):
        """Next (direction > 0) or previous interval start relative to ``sample``, like Find in an editor."""
        if not self.count:
            return None
        starts = self.data[:, 0]
        if direction > 0:
            index = search_sorted(starts, sample, "right")
            return index if index < self.count else (0 if wrap else None)
        index = search_sorted(starts, sample, "left") - 1
        return index if index >= 0 else (self.count - 1 if wrap else None)

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
    # Resolved at creation: full signal names and the radix each literal was written in.
    # A later radix change in the view must not reinterpret the saved expression.
    radixes: dict = field(default_factory=dict)

    @property
    def dependencies(self):
        return frozenset(self.radixes)

    def __post_init__(self):
        if not self.id:
            self.id = uuid4().hex

    @property
    def sampling(self):
        """``period`` holds a SamplingGrid, or a legacy sample count used with ``phase``."""
        return as_sampling(self.period, self.phase)

    def close(self):
        self.intervals.close()


class HighlightModel(QObject):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.groups = []
        self.master_enabled = True
        self.active_id = None  # group last navigated with ← / →, shown as n/total

    @property
    def active_group(self):
        return next((group for group in self.groups if group.id == self.active_id), None)

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
        self.active_id = None
        self.changed.emit()


class IntervalWriter:
    """Merge sorted-by-start intervals into a disk-backed ``HighlightIntervals`` file.

    An interval that starts at or before the end of the pending one extends it, so the
    file holds disjoint spans; touching spans of neighbouring chunks are joined too.
    """

    def __init__(self, folder, prefix):
        self.stream = tempfile.NamedTemporaryFile(dir=folder, prefix=prefix, suffix=".bin", delete=False)
        self.path = Path(self.stream.name)
        self.count = 0
        self.pending = None

    def add(self, starts, ends):
        starts = np.asarray(starts, dtype=np.int64)
        ends = np.asarray(ends, dtype=np.int64)
        if not len(starts):
            return
        if self.pending is not None:
            starts = np.concatenate(([self.pending[0]], starts))
            ends = np.concatenate(([self.pending[1]], ends))
        reach = np.maximum.accumulate(ends)
        opens = np.empty(len(starts), dtype=bool)
        opens[0] = True
        opens[1:] = starts[1:] > reach[:-1]
        first = np.flatnonzero(opens)
        merged = np.column_stack((starts[first], np.maximum.reduceat(ends, first)))
        merged[:-1].astype("<i8").tofile(self.stream)
        self.count += len(merged) - 1
        self.pending = int(merged[-1, 0]), int(merged[-1, 1])

    def finish(self):
        if self.pending is not None:
            np.asarray([self.pending], dtype="<i8").tofile(self.stream)
            self.count += 1
            self.pending = None
        self.stream.close()
        if not self.count:
            self.path.unlink(missing_ok=True)
            return HighlightIntervals()
        return HighlightIntervals(self.path, self.count)

    def discard(self):
        self.stream.close()
        self.path.unlink(missing_ok=True)


class HighlightService:
    """Evaluate one condition at each period phase without touching CSV samples."""

    def build(self, capture, condition, marker_a, marker_b, period, phase=0,
              progress=lambda percent, rows: None, cancel=lambda: False):
        """``period`` is a SamplingGrid, or a sample count combined with ``phase``."""
        sampling = as_sampling(period, phase)
        lower, upper = sorted((marker_a, marker_b))
        # Paint the span the sampled values describe, clipped to A…B.
        low = max(0, lower)
        high = min(capture.count, upper)
        writer = IntervalWriter(capture.storage.name, "highlight_")
        try:
            points = 0
            for chunk in sampling.point_chunks(low, high, capture.count, cancel):
                if cancel():
                    raise LoadCancelled()
                keep = np.ones(len(chunk.sample), dtype=bool)
                if condition is not None:
                    keep &= condition.evaluate(chunk.at, chunk.previous_at)
                start = np.maximum(chunk.start, low)
                end = np.minimum(chunk.end, high)
                keep &= end > start
                writer.add(start[keep], end[keep])
                points += len(chunk.sample)
                progress(round(100 * (int(chunk.sample[-1]) - lower) / max(1, upper - lower)), points)
            result = writer.finish()
            progress(100, result.count)
            return result
        except BaseException:
            writer.discard()
            raise
