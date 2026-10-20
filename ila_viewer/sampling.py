# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Sampling grids for copy and highlight: a fixed period or the edges of a clock signal.

A grid yields points ``(start, end, at, previous_at)``:

* ``start`` - the sample where the point begins (used for ranges and highlighting);
* ``end`` - where the highlighted interval of this point ends;
* ``at`` - the sample whose values are evaluated and copied;
* ``previous_at`` - ``at`` of the preceding grid point, for ``prev(signal)``.

Edge sampling never assumes a free-running clock: gated or bursty clocks such as
SPI SCK only produce points where real edges exist.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import re

import numpy as np

from .data import LoadCancelled

CLOCK_NAME = re.compile(r"clk|clock|sck", re.IGNORECASE)


def clock_score(signal):
    """Higher means a more likely clock; 0 means the name does not look like a clock."""
    name = signal.short_name.casefold()
    stem = re.sub(r"\[\d+\]$", "", name)
    if stem in {"clk", "clock", "clk_i", "i_clk", "clk_in", "sclk", "sck", "aclk"}:
        return 3
    if re.search(r"(^|_)(clk|clock|sclk|sck)(_|$)", stem):
        return 2
    return 1 if CLOCK_NAME.search(stem) else 0


def clock_candidates(signals):
    """1-bit signals, likely clocks first; the original order is kept within each rank."""
    single = [signal for signal in signals if signal.width == 1]
    ranked = sorted(enumerate(single), key=lambda item: (-clock_score(item[1]), item[0]))
    return [signal for _, signal in ranked]


class SamplingGrid(ABC):
    @abstractmethod
    def points(self, lower, upper, count, cancel=lambda: False):
        """Yield grid points whose start lies in [lower, upper)."""

    @abstractmethod
    def describe(self) -> str: ...

    def rebind(self, capture):
        """Return an equivalent grid for a refreshed capture, or None if impossible."""
        return self


class PeriodicSampling(SamplingGrid):
    def __init__(self, period, phase=0):
        self.period = max(1, int(period))
        self.phase = int(phase)

    def points(self, lower, upper, count, cancel=lambda: False):
        period = self.period
        first = lower + ((self.phase - lower) % period)
        for index, sample in enumerate(range(first, upper, period)):
            if index % 4096 == 0 and cancel():
                raise LoadCancelled()
            yield sample, sample + period, sample, sample - period if sample >= period else None

    def describe(self):
        return f"период {self.period:,} отсч."

    def __eq__(self, other):
        return isinstance(other, PeriodicSampling) and (self.period, self.phase) == (other.period, other.phase)

    def __hash__(self):
        return hash((self.period, self.phase))


class EdgeSampling(SamplingGrid):
    """Sample on rising, falling or both edges of a 1-bit signal.

    ``before_edge`` evaluates the values just before the edge, as a flip-flop sees
    them; this matters for simulator dumps where registers update at the edge time.
    """
    EDGES = {"rising": "↑", "falling": "↓", "both": "↕"}
    CHUNK = 262144

    def __init__(self, signal, edge="rising", before_edge=True):
        if signal.width != 1:
            raise ValueError("Тактовым может быть только однобитный сигнал")
        if edge not in self.EDGES:
            raise ValueError(f"Неизвестный фронт: {edge}")
        self.signal = signal
        self.edge = edge
        self.before_edge = before_edge
        self._typical = None

    @property
    def name(self):
        return self.signal.name

    def describe(self):
        timing = "до фронта" if self.before_edge else "на фронте"
        return f"{self.signal.short_name} {self.EDGES[self.edge]}, значения {timing}"

    def rebind(self, capture):
        signal = next((s for s in capture.signals if s.name == self.signal.name and s.width == 1), None)
        return EdgeSampling(signal, self.edge, self.before_edge) if signal is not None else None

    def _levels(self):
        lut = np.full(max(1, len(self.signal.values)), -1, dtype=np.int8)
        for code, value in enumerate(self.signal.values):
            if isinstance(value, int) and value in (0, 1):
                lut[code] = value
        return lut

    def _mask(self, before, after):
        if self.edge == "rising":
            return (before == 0) & (after == 1)
        if self.edge == "falling":
            return (before == 1) & (after == 0)
        return ((before == 0) & (after == 1)) | ((before == 1) & (after == 0))

    def _edge_chunks(self, first_run, last_run, cancel, reverse=False):
        """Edge sample positions for transitions with run index in [first_run, last_run)."""
        starts, codes, levels = self.signal.starts, self.signal.codes, self._levels()
        first_run = max(1, first_run)  # run 0 is the capture start, never an edge
        bounds = range(first_run, last_run, self.CHUNK)
        for a in (reversed(bounds) if reverse else bounds):
            if cancel():
                raise LoadCancelled()
            b = min(last_run, a + self.CHUNK)
            run_codes = np.asarray(codes[a - 1:b], dtype=np.int64)
            states = levels[run_codes]
            edges = np.asarray(starts[a:b])[self._mask(states[:-1], states[1:])]
            yield edges[::-1] if reverse else edges

    def edges(self, lower, upper, cancel=lambda: False):
        """All edge samples in [lower, upper), chunked to bound memory."""
        first = int(np.searchsorted(self.signal.starts, lower, side="left"))
        last = int(np.searchsorted(self.signal.starts, upper, side="left"))
        yield from self._edge_chunks(first, last, cancel)

    def _edge_before(self, sample, cancel):
        last = int(np.searchsorted(self.signal.starts, sample, side="left"))
        for chunk in self._edge_chunks(1, last, cancel, reverse=True):
            if len(chunk):
                return int(chunk[0])
        return None

    def typical_interval(self, limit=65536):
        """Median distance between consecutive chosen edges, robust to idle gaps."""
        if self._typical is None:
            collected = []
            total = 0
            for chunk in self.edges(0, int(self.signal.starts[-1]) + 1 if len(self.signal.starts) else 0):
                collected.append(chunk[:limit - total])
                total += len(collected[-1])
                if total >= limit:
                    break
            positions = np.concatenate(collected) if collected else np.empty(0, dtype=np.int64)
            gaps = np.diff(positions)
            self._typical = float(np.median(gaps)) if len(gaps) else None
        return self._typical

    def count_edges(self, cancel=lambda: False):
        return sum(len(chunk) for chunk in self.edges(0, int(self.signal.starts[-1]) + 1, cancel)) \
            if len(self.signal.starts) else 0

    def points(self, lower, upper, count, cancel=lambda: False):
        typical = self.typical_interval()
        span = max(1, int(round(typical))) if typical else None
        previous = self._edge_before(lower, cancel)
        previous_at = self._at(previous) if previous is not None else None
        current = None
        for chunk in self.edges(lower, count, cancel):
            for edge in map(int, chunk):
                if current is not None:
                    yield self._point(current, edge, span, previous_at)
                    previous_at = self._at(current)
                if edge >= upper:
                    return
                current = edge
        if current is not None:
            yield self._point(current, count, span, previous_at)

    def _at(self, edge):
        return edge - 1 if self.before_edge and edge > 0 else edge

    def _point(self, start, next_start, span, previous_at):
        # A gated clock must not paint its idle gap: cap the interval at one typical period.
        end = min(next_start, start + span) if span else next_start
        return start, max(start + 1, end), self._at(start), previous_at

    def __eq__(self, other):
        return (isinstance(other, EdgeSampling) and other.signal is self.signal
                and (other.edge, other.before_edge) == (self.edge, self.before_edge))

    def __hash__(self):
        return hash((id(self.signal), self.edge, self.before_edge))


def as_sampling(period, phase=0):
    """Accept a grid or the legacy (period, phase) pair."""
    return period if isinstance(period, SamplingGrid) else PeriodicSampling(period, phase)
