# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Edge counting and period estimation for a 1-bit signal between two markers."""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from .timebase import format_seconds, group_digits


@dataclass(frozen=True)
class EdgeMeasurement:
    """Transitions whose sample lies in (start, end]: changes between the values at A and B."""

    start: int
    end: int
    rising: int
    falling: int
    first_rise: int | None
    last_rise: int | None

    @property
    def period_samples(self):
        if self.rising < 2:
            return None
        return (self.last_rise - self.first_rise) / (self.rising - 1)


class EdgeMeasurementService:
    """Reads the run-length index only; samples are never decoded or modified.

    A clean 0/1 signal alternates on every run, so counts come from binary
    searches in O(log n). Signals with unknown states fall back to a scan of
    the transitions inside the range.
    """

    def measure(self, signal, start, end):
        if signal is None or signal.width != 1 or len(signal.starts) == 0:
            return None
        start, end = sorted((int(start), int(end)))
        starts = signal.starts
        # Run indices of transitions with start < sample <= end; run 0 is the capture start.
        first = max(1, int(np.searchsorted(starts, start, side="right")))
        last = max(first, int(np.searchsorted(starts, end, side="right")))
        if last == first:
            return EdgeMeasurement(start, end, 0, 0, None, None)
        levels = self._levels(signal)
        if len(signal.values) <= 2 and (levels >= 0).all():
            return self._alternating(signal, levels, start, end, first, last)
        return self._scan(signal, levels, start, end, first, last)

    @staticmethod
    def _levels(signal):
        """Map value codes to 0/1, or -1 for unknown states such as X and Z."""
        lut = np.full(max(1, len(signal.values)), -1, dtype=np.int8)
        for code, value in enumerate(signal.values):
            if isinstance(value, int) and value in (0, 1):
                lut[code] = value
        return lut

    def _alternating(self, signal, levels, start, end, first, last):
        transitions = last - first
        rising_first = levels[int(signal.codes[first])] == 1
        rising = (transitions + 1) // 2 if rising_first else transitions // 2
        first_rise_index = first if rising_first else first + 1
        last_index = last - 1
        last_rise_index = last_index if levels[int(signal.codes[last_index])] == 1 else last_index - 1
        has_rise = rising > 0
        return EdgeMeasurement(start, end, rising, transitions - rising,
                               int(signal.starts[first_rise_index]) if has_rise else None,
                               int(signal.starts[last_rise_index]) if has_rise else None)

    def _scan(self, signal, levels, start, end, first, last):
        before = levels[np.asarray(signal.codes[first - 1:last - 1], dtype=np.int64)]
        after = levels[np.asarray(signal.codes[first:last], dtype=np.int64)]
        rises = np.flatnonzero((before == 0) & (after == 1))
        falls = int(np.count_nonzero((before == 1) & (after == 0)))
        if not len(rises):
            return EdgeMeasurement(start, end, 0, falls, None, None)
        return EdgeMeasurement(start, end, len(rises), falls,
                               int(signal.starts[first + rises[0]]), int(signal.starts[first + rises[-1]]))


_UNITS = {"fs": 1e-15, "ps": 1e-12, "ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0}


def format_frequency(hertz):
    for factor, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if hertz >= factor:
            return f"{hertz / factor:.6g} {unit}"
    return f"{hertz:.6g} Hz"


class MeasurementFormatter:
    """Turns an EdgeMeasurement into readout text using the capture's time axis."""

    def parts(self, measurement, capture, period_ns=0.0, signal_name=""):
        if measurement is None:
            return []
        label = f"↑{signal_name}" if signal_name else "↑"
        parts = [f"{label} A…B: {measurement.rising:,}"]
        if measurement.rising < 2:
            return parts
        if capture.times is not None:
            ticks = (capture.time_at(measurement.last_rise) - capture.time_at(measurement.first_rise)) / (measurement.rising - 1)
            parts.append(f"T≈ {capture.format_ticks(round(ticks, 6))}")
            seconds = self.tick_seconds(capture.timescale)
            if seconds and ticks > 0:
                parts.append(f"f≈ {format_frequency(1 / (ticks * seconds))}")
        else:
            samples = measurement.period_samples
            parts.append(f"T≈ {group_digits(samples, 3)} отсч.")
            if period_ns and samples > 0:
                parts.append(f"T≈ {format_seconds(samples * period_ns * 1e-9)}")
                parts.append(f"f≈ {format_frequency(1e9 / (samples * period_ns))}")
        return parts

    @staticmethod
    def tick_seconds(timescale):
        match = re.fullmatch(r"(\d+)(fs|ps|ns|us|ms|s)", timescale or "")
        return int(match[1]) * _UNITS[match[2]] if match else None
