# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Sampling grids for copy and highlight: a fixed period or the edges of a clock signal.

A grid yields ``GridPoint(sample, start, end, at, previous_at)``:

* ``sample`` - the grid position (period sample or clock edge), used for A…B ranges;
* ``start``/``end`` - the span the sampled values describe, painted by highlights;
* ``at`` - the sample whose values are evaluated and copied;
* ``previous_at`` - ``at`` of the preceding grid point, for ``prev(signal)``.

Keeping ``sample`` and the painted span apart matters for "before the edge"
sampling: those values belong to the cycle that *ends* at the edge.

Edge sampling never assumes a free-running clock: gated or bursty clocks such as
SPI SCK only produce points where real edges exist.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
import re
from typing import NamedTuple

import numpy as np

from .data import LoadCancelled, search_sorted

CLOCK_NAME = re.compile(r"clk|clock|sck", re.IGNORECASE)


class GridPoint(NamedTuple):
    sample: int
    start: int
    end: int
    at: int
    previous_at: int | None


class GridChunk(NamedTuple):
    """Consecutive grid points as int64 arrays; ``previous_at`` is -1 where there is none."""
    sample: np.ndarray
    start: np.ndarray
    end: np.ndarray
    at: np.ndarray
    previous_at: np.ndarray


CHUNK_POINTS = 262144


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

    def point_chunks(self, lower, upper, count, cancel=lambda: False):
        """``points`` as arrays, so callers can evaluate conditions with numpy."""
        batch = []
        for point in self.points(lower, upper, count, cancel):
            batch.append(point)
            if len(batch) == CHUNK_POINTS:
                yield _chunk(batch)
                batch = []
        if batch:
            yield _chunk(batch)

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
            yield GridPoint(sample, sample, sample + period, sample,
                            sample - period if sample >= period else None)

    def point_chunks(self, lower, upper, count, cancel=lambda: False):
        period = self.period
        first = lower + ((self.phase - lower) % period)
        step = CHUNK_POINTS * period
        for a in range(first, upper, step):
            if cancel():
                raise LoadCancelled()
            sample = np.arange(a, min(upper, a + step), period, dtype=np.int64)
            previous = np.where(sample >= period, sample - period, -1)
            yield GridChunk(sample, sample, sample + period, sample, previous)

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

    def __init__(self, signal, edge="rising", before_edge=True, times=None):
        """``times`` holds a simulator capture's per-row timestamps (None for ILA CSV).

        VCD rows are value changes, not clock cycles: the distance between two rows depends
        on unrelated activity, so periods and idle caps are measured in real time there.
        """
        if signal.width != 1:
            raise ValueError("Тактовым может быть только однобитный сигнал")
        if edge not in self.EDGES:
            raise ValueError(f"Неизвестный фронт: {edge}")
        self.signal = signal
        self.edge = edge
        self.before_edge = before_edge
        self.times = times
        self._typical = None
        self._typical_ticks = None

    @property
    def name(self):
        return self.signal.name

    def describe(self):
        timing = "до фронта" if self.before_edge else "на фронте"
        return f"{self.signal.short_name} {self.EDGES[self.edge]}, значения {timing}"

    def rebind(self, capture):
        signal = next((s for s in capture.signals if s.name == self.signal.name and s.width == 1), None)
        return EdgeSampling(signal, self.edge, self.before_edge, capture.times) if signal is not None else None

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
        first = search_sorted(self.signal.starts, lower, "left")
        last = search_sorted(self.signal.starts, upper, "left")
        yield from self._edge_chunks(first, last, cancel)

    def _edge_before(self, sample, cancel):
        last = search_sorted(self.signal.starts, sample, "left")
        for chunk in self._edge_chunks(1, last, cancel, reverse=True):
            if len(chunk):
                return int(chunk[0])
        return None

    def _first_edges(self, limit, cancel=lambda: False):
        collected = []
        total = 0
        for chunk in self.edges(0, int(self.signal.starts[-1]) + 1 if len(self.signal.starts) else 0, cancel):
            collected.append(np.asarray(chunk[:limit - total], dtype=np.int64))
            total += len(collected[-1])
            if total >= limit:
                break
        return np.concatenate(collected) if collected else np.empty(0, dtype=np.int64)

    def typical_interval(self, limit=65536, cancel=lambda: False):
        """Median distance in rows between consecutive chosen edges, robust to idle gaps."""
        if self._typical is None:
            gaps = np.diff(self._first_edges(limit, cancel))
            self._typical = float(np.median(gaps)) if len(gaps) else None
        return self._typical

    def typical_ticks(self, limit=65536, cancel=lambda: False):
        """Median time between consecutive chosen edges in capture ticks (simulator dumps only)."""
        if self.times is None:
            return None
        if self._typical_ticks is None:
            positions = self._first_edges(limit, cancel)
            gaps = np.diff(np.asarray(self.times[positions], dtype=np.int64))
            gaps = gaps[gaps > 0]
            self._typical_ticks = int(np.median(gaps)) if len(gaps) else None
        return self._typical_ticks

    def _span_starts(self, edges, ticks):
        """First row whose time lies within one typical period before each edge."""
        times = np.asarray(self.times[edges], dtype=np.int64)
        return search_sorted(self.times, times - ticks, "left").astype(np.int64)

    def _span_ends(self, edges, ticks):
        """Exclusive row bound one typical period after each edge."""
        times = np.asarray(self.times[edges], dtype=np.int64)
        return search_sorted(self.times, times + ticks, "left").astype(np.int64)

    def _span(self):
        """Idle cap: (rows, None) for CSV rows, (None, ticks) for timestamped captures."""
        if self.times is not None:
            return None, self.typical_ticks()
        typical = self.typical_interval()
        return (max(1, int(round(typical))) if typical else None), None

    def count_edges(self, cancel=lambda: False):
        return sum(len(chunk) for chunk in self.edges(0, int(self.signal.starts[-1]) + 1, cancel)) \
            if len(self.signal.starts) else 0

    def points(self, lower, upper, count, cancel=lambda: False):
        span = self._span()
        previous = self._edge_before(lower, cancel)
        current = None
        for chunk in self.edges(lower, count, cancel):
            for edge in map(int, chunk):
                if current is not None:
                    yield self._point(current, previous, edge, span)
                    previous = current
                if edge >= upper:
                    return
                current = edge
        if current is not None:
            yield self._point(current, previous, count, span)

    def point_chunks(self, lower, upper, count, cancel=lambda: False):
        """Vectorized ``points``: each edge needs the previous and the next edge."""
        span = self._span()
        before = self._edge_before(lower, cancel)
        carry = np.empty(0, dtype=np.int64)  # the last edge, whose next edge is not known yet
        previous = -1 if before is None else before
        for chunk in self.edges(lower, count, cancel):
            edges = np.concatenate((carry, np.asarray(chunk, dtype=np.int64)))
            beyond = search_sorted(edges, upper, "left")
            if beyond < len(edges):
                if beyond:
                    yield self._chunk(edges[:beyond], previous, edges[1:beyond + 1], span)
                return
            if len(edges) > 1:
                yield self._chunk(edges[:-1], previous, edges[1:], span)
                previous = int(edges[-2])
            carry = edges[-1:]
        if len(carry):
            yield self._chunk(carry, previous, np.array([count], dtype=np.int64), span)

    def _chunk(self, edges, previous, following, span):
        prior = np.empty(len(edges), dtype=np.int64)
        prior[0] = previous
        prior[1:] = edges[:-1]
        has_prior = prior >= 0
        shift = 1 if self.before_edge else 0
        at = np.where(edges > 0, edges - shift, edges)
        previous_at = np.where(has_prior, np.where(prior > 0, prior - shift, prior), -1)
        rows, ticks = span
        if self.before_edge:
            start = np.where(has_prior, prior, 0)
            if rows:
                start = np.maximum(start, edges - rows)
            elif ticks:
                start = np.maximum(start, self._span_starts(edges, ticks))
            start = np.maximum(0, np.minimum(start, edges - 1))
            return GridChunk(edges, start, edges.copy(), at, previous_at)
        if rows:
            end = np.minimum(following, edges + rows)
        elif ticks:
            end = np.minimum(following, self._span_ends(edges, ticks))
        else:
            end = following.copy()
        return GridChunk(edges, edges.copy(), np.maximum(edges + 1, end), at, previous_at)

    def _at(self, edge):
        return edge - 1 if self.before_edge and edge > 0 else edge

    def _point(self, edge, previous_edge, next_edge, span):
        """The span shown for an edge is the one its sampled values describe.

        Before the edge, values belong to the cycle that ends at the edge; at the
        edge, to the cycle that starts there. A gated clock must not paint its idle
        gap, so either span is capped at one typical clock period.
        """
        previous_at = self._at(previous_edge) if previous_edge is not None else None
        rows, ticks = span
        single = np.array([edge], dtype=np.int64)
        if self.before_edge:
            start = previous_edge if previous_edge is not None else 0
            if rows:
                start = max(start, edge - rows)
            elif ticks:
                start = max(start, int(self._span_starts(single, ticks)[0]))
            return GridPoint(edge, max(0, min(start, edge - 1)), edge, self._at(edge), previous_at)
        if rows:
            end = min(next_edge, edge + rows)
        elif ticks:
            end = min(next_edge, int(self._span_ends(single, ticks)[0]))
        else:
            end = next_edge
        return GridPoint(edge, edge, max(edge + 1, end), self._at(edge), previous_at)

    def __eq__(self, other):
        return (isinstance(other, EdgeSampling) and other.signal is self.signal
                and (other.edge, other.before_edge) == (self.edge, self.before_edge))

    def __hash__(self):
        return hash((id(self.signal), self.edge, self.before_edge))


def _chunk(points):
    columns = list(zip(*points))
    previous = [-1 if value is None else value for value in columns[4]]
    return GridChunk(*(np.asarray(column, dtype=np.int64) for column in (*columns[:4], previous)))


def as_sampling(period, phase=0):
    """Accept a grid or the legacy (period, phase) pair."""
    return period if isinstance(period, SamplingGrid) else PeriodicSampling(period, phase)
