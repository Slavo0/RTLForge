# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Cancellable event-sequence matching on calibrated samples."""

from pathlib import Path
import struct
import tempfile

from .data import LoadCancelled
from .highlights import HighlightIntervals


class AdvancedHighlightService:
    """Keep active FSM paths so overlapping stage sequences remain detectable."""

    def build(self, capture, pattern, marker_a, marker_b, period, phase,
              progress=lambda percent, rows: None, cancel=lambda: False):
        period = max(1, int(period))
        lower, upper = sorted((marker_a, marker_b))
        start_boundary = lower + (marker_b == lower)
        end_boundary = min(capture.count, upper + (marker_b != upper))
        first = start_boundary + ((phase - start_boundary) % period)
        active = []  # (next stage index, first matching sample)
        pending = None
        count = 0
        path = None
        try:
            with tempfile.NamedTemporaryFile(dir=capture.storage.name, prefix="advanced_",
                                             suffix=".bin", delete=False) as stream:
                path = Path(stream.name)
                for index, sample in enumerate(range(first, end_boundary, period)):
                    if index % 4096 == 0:
                        if cancel():
                            raise LoadCancelled()
                        progress(round(100 * (sample - start_boundary) /
                                       max(1, end_boundary - start_boundary)), index)
                    previous = sample - period if sample >= period else None
                    next_active = []
                    for stage_index, start in active:
                        if not pattern.stages[stage_index].matches(sample, previous):
                            continue
                        if stage_index + 1 < len(pattern.stages):
                            next_active.append((stage_index + 1, start))
                            continue
                        end = min(sample + period, end_boundary)
                        if pending and start <= pending[1]:
                            pending = pending[0], max(pending[1], end)
                        else:
                            if pending:
                                stream.write(struct.pack("<qq", *pending))
                                count += 1
                            pending = start, end
                    if pattern.stages[0].matches(sample, previous):
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
