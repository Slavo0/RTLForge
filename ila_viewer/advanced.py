# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Cancellable event-sequence matching on calibrated samples."""

import numpy as np

from .data import LoadCancelled
from .highlights import IntervalWriter
from .sampling import as_sampling


class AdvancedHighlightService:
    """A sequence of k stages matches at point i when stage j holds at point i + j.

    Every start point is tried, so overlapping sequences are all found. Stage results
    are numpy arrays per chunk of grid points; the last k - 1 points of a chunk are
    carried into the next one, so sequences that cross a chunk border are kept.
    """

    def build(self, capture, pattern, marker_a, marker_b, period, phase=0,
              progress=lambda percent, rows: None, cancel=lambda: False):
        """``period`` is a SamplingGrid, or a sample count combined with ``phase``."""
        sampling = as_sampling(period, phase)
        lower, upper = sorted((marker_a, marker_b))
        start_boundary = max(0, lower)
        end_boundary = min(capture.count, upper)
        stages = pattern.stages
        k = len(stages)
        writer = IntervalWriter(capture.storage.name, "advanced_")
        carry = None  # (stage masks, starts, ends) of points that cannot finish a sequence yet
        points = 0
        try:
            for chunk in sampling.point_chunks(start_boundary, end_boundary, capture.count, cancel):
                if cancel():
                    raise LoadCancelled()
                masks = np.stack([stage.evaluate(chunk.at, chunk.previous_at) for stage in stages])
                starts, ends = chunk.start, chunk.end
                if carry is not None:
                    masks = np.concatenate((carry[0], masks), axis=1)
                    starts = np.concatenate((carry[1], starts))
                    ends = np.concatenate((carry[2], ends))
                n = masks.shape[1]
                complete = n - k + 1
                if complete > 0:
                    found = masks[0, :complete].copy()
                    for j in range(1, k):
                        found &= masks[j, j:j + complete]
                    hits = np.flatnonzero(found)
                    # Painted from the span of the first point to the end of the last one.
                    writer.add(np.maximum(starts[hits], start_boundary),
                               np.minimum(ends[hits + k - 1], end_boundary))
                    tail = slice(complete, n)
                else:
                    tail = slice(0, n)
                carry = masks[:, tail], starts[tail], ends[tail]
                points += len(chunk.sample)
                progress(round(100 * (int(chunk.sample[-1]) - start_boundary) /
                               max(1, end_boundary - start_boundary)), points)
            result = writer.finish()
            progress(100, result.count)
            return result
        except BaseException:
            writer.discard()
            raise
