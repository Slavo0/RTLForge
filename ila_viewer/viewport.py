# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Mapping between sample positions and plot x for one viewport geometry; Qt-free.

A transform is built once per frame (or per view change) from the capture, the
visible range and the plot geometry, so the frame origin and the edge offsets are
computed once instead of in every ``sample_x`` call. ``x`` and ``xs`` give the same
floating-point results, so scalar and vectorized drawing put a line on the same pixel.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class ViewportTransform(ABC):
    """Sample position <-> x for ``[left, left + span)`` drawn in ``plot_width`` pixels."""

    def __init__(self, left, span, plot_left, plot_width):
        self.left, self.span = left, span
        self.plot_left, self.plot_width = plot_left, plot_width
        self._edges = None

    @abstractmethod
    def x(self, sample) -> float:
        """Plot x of one sample position (may lie outside the plot)."""

    @abstractmethod
    def xs(self, samples) -> np.ndarray:
        """Plot x of an array of sample positions; equal to ``x`` element by element."""

    @abstractmethod
    def sample(self, x) -> float:
        """Inverse of ``x``: the sample position at plot x."""

    @abstractmethod
    def _pixel_edges(self) -> np.ndarray:
        """Sample positions of the ``plot_width + 1`` pixel boundaries."""

    @property
    def edges(self) -> np.ndarray:
        """Pixel boundaries as sample positions, computed once per transform."""
        if self._edges is None:
            self._edges = self._pixel_edges()
        return self._edges


class UniformTransform(ViewportTransform):
    """Captures without timestamps (CSV): x is linear in the sample index."""

    def x(self, sample):
        return self.plot_left + (sample - self.left) / self.span * self.plot_width

    def xs(self, samples):
        samples = np.asarray(samples, dtype=np.float64)
        return self.plot_left + (samples - self.left) / self.span * self.plot_width

    def sample(self, x):
        return self.left + (x - self.plot_left) / self.plot_width * self.span

    def _pixel_edges(self):
        return np.linspace(self.left, self.left + self.span, self.plot_width + 1)


class TimeTransform(ViewportTransform):
    """Simulator captures: x is proportional to real time between timestamps.

    Offsets are taken from an integer origin, the timestamp of the first visible row,
    before converting to float, so long captures keep tick precision when zoomed in.
    """

    def __init__(self, capture, left, span, plot_left, plot_width):
        super().__init__(left, span, plot_left, plot_width)
        self.capture = capture
        self.times = capture.times
        self.last = capture.count - 1
        self.origin = int(capture.times[max(0, min(self.last, int(left)))])
        self.low = capture.time_offset_at(left, self.origin)
        self.high = capture.time_offset_at(left + span, self.origin)
        self.scale = max(1, self.high - self.low)

    def x(self, sample):
        return self.plot_left + (self.capture.time_offset_at(sample, self.origin) - self.low) / self.scale \
            * self.plot_width

    def offsets(self, samples):
        """Vectorized ``Capture.time_offset_at`` with this origin, with the same float steps."""
        position = np.asarray(samples, dtype=np.float64)
        low = np.clip(np.trunc(position), 0, self.last).astype(np.int64)
        base = self.times[low] - self.origin
        following = np.minimum(low + 1, self.last)
        widths = self.times[following] - self.times[low]
        fraction = position - low
        fraction = np.where(low == self.last, fraction, fraction * widths)
        return base + fraction

    def xs(self, samples):
        return self.plot_left + (self.offsets(samples) - self.low) / self.scale * self.plot_width

    def sample(self, x):
        offset = self.low + (x - self.plot_left) / self.plot_width * (self.high - self.low)
        return float(self.capture.samples_at_offsets([offset], self.origin)[0])

    def _pixel_edges(self):
        offsets = np.linspace(self.low, self.high, self.plot_width + 1)
        return self.capture.samples_at_offsets(offsets, self.origin)


def pixel_runs(indices, codes):
    """Runs of pixels drawn as one piece in a row with more changes than pixels.

    ``indices`` holds the row's run index at each of the ``width + 1`` pixel edges and
    ``codes`` the value code of the run at each pixel's left edge (``width`` entries).
    A pixel whose two edges fall in different runs contains a transition and is busy;
    neighbouring busy pixels form one run, and neighbouring quiet pixels form one run
    while their value code stays the same. Returns ``(starts, ends, busy, codes)`` with
    one entry per run, ``ends`` exclusive, all as numpy arrays.
    """
    indices = np.asarray(indices)
    codes = np.asarray(codes)
    width = len(indices) - 1
    if width <= 0:
        empty = np.zeros(0, dtype=np.int64)
        return empty, empty, np.zeros(0, dtype=bool), codes[:0]
    busy = indices[1:] != indices[:-1]
    change = np.empty(width, dtype=bool)
    change[0] = True
    change[1:] = (busy[1:] != busy[:-1]) | (~busy[1:] & (codes[1:width] != codes[:width - 1]))
    starts = np.flatnonzero(change)
    ends = np.append(starts[1:], width)
    return starts, ends, busy[starts], codes[starts]


def viewport_transform(capture, left, span, plot_left, plot_width) -> ViewportTransform:
    """The transform for a capture: time-proportional when it has timestamps."""
    if capture is not None and capture.times is not None:
        return TimeTransform(capture, left, span, plot_left, plot_width)
    return UniformTransform(left, span, plot_left, plot_width)
