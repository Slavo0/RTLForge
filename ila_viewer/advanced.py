# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Cancellable event-sequence matching on calibrated samples."""

from pathlib import Path
import struct
import tempfile

from .data import LoadCancelled
from .highlights import HighlightIntervals
from .sampling import as_sampling


class AdvancedHighlightService:
    """Keep active FSM paths so overlapping stage sequences remain detectable."""

    def build(self, capture, pattern, marker_a, marker_b, period, phase=0,
              progress=lambda percent, rows: None, cancel=lambda: False):
        """``period`` is a SamplingGrid, or a sample count combined with ``phase``."""
        sampling = as_sampling(period, phase)
        lower, upper = sorted((marker_a, marker_b))
        start_boundary = lower + (marker_b == lower)
        end_boundary = min(capture.count, upper + (marker_b != upper))
        active = []  # (next stage index, first matching sample)
        pending = None
        count = 0
        path = None
        try:
            with tempfile.NamedTemporaryFile(dir=capture.storage.name, prefix="advanced_",
                                             suffix=".bin", delete=False) as stream:
                path = Path(stream.name)
                points = sampling.points(start_boundary, end_boundary, capture.count, cancel)
                for index, (sample, next_start, at, previous) in enumerate(points):
                    if index % 4096 == 0:
                        if cancel():
                            raise LoadCancelled()
                        progress(round(100 * (sample - start_boundary) /
                                       max(1, end_boundary - start_boundary)), index)
                    next_active = []
                    for stage_index, start in active:
                        if not pattern.stages[stage_index].matches(at, previous):
                            continue
                        if stage_index + 1 < len(pattern.stages):
                            next_active.append((stage_index + 1, start))
                            continue
                        end = min(next_start, end_boundary)
                        if pending and start <= pending[1]:
                            pending = pending[0], max(pending[1], end)
                        else:
                            if pending:
                                stream.write(struct.pack("<qq", *pending))
                                count += 1
                            pending = start, end
                    if pattern.stages[0].matches(at, previous):
                        next_active.append((1, sample))
                    active = next_active
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
