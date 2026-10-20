# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Time axes and Vivado-style time text: grouped digits, a readable unit, no exponents."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Callable

UNITS = (("fs", 1e-15), ("ps", 1e-12), ("ns", 1e-9), ("us", 1e-6), ("ms", 1e-3), ("s", 1.0))
_SCALE = dict(UNITS)


def unit_seconds(timescale):
    """Seconds per tick for a VCD timescale such as ``1ns`` or ``10 ps``."""
    match = re.fullmatch(r"\s*(\d+)\s*(fs|ps|ns|us|ms|s)\s*", timescale or "")
    return int(match[1]) * _SCALE[match[2]] if match else None


def group_digits(value, decimals=0):
    """``1003100.5`` -> ``1,003,100.5``; trailing fractional zeros are dropped."""
    text = f"{abs(value):,.{max(0, decimals)}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return ("-" if value < 0 and text.strip("0.,") else "") + text


def _decimals(unit, resolution):
    if not resolution or resolution >= unit:
        return 0
    return min(6, max(0, math.ceil(math.log10(unit / resolution) - 1e-9)))


def format_seconds(seconds, resolution=1e-12):
    """Cursor-style text in ns (or a coarser unit when the resolution is coarser), like Vivado."""
    unit_name, unit = next(((name, scale) for name, scale in reversed(UNITS)
                            if scale <= max(resolution, 1e-9)), ("ns", 1e-9))
    return f"{group_digits(seconds / unit, _decimals(unit, resolution))} {unit_name}"


def format_ruler(seconds, step_seconds):
    """Ruler label: the unit follows the tick step, so labels stay short and integral."""
    unit_name, unit = next(((name, scale) for name, scale in reversed(UNITS)
                            if scale <= step_seconds * (1 + 1e-9)), UNITS[0])
    return f"{group_digits(seconds / unit, _decimals(unit, step_seconds))} {unit_name}"


def nice_step(ideal):
    """The 1/2/5 × 10ⁿ step not smaller than ``ideal``."""
    if ideal <= 0:
        return 1.0
    decade = 10 ** math.floor(math.log10(ideal))
    return next(m * decade for m in (1, 2, 5, 10) if m * decade >= ideal * (1 - 1e-12))


@dataclass(frozen=True)
class TimeAxis:
    """Maps sample indices to axis values (``ticks``) and formats them as time."""
    to_ticks: Callable[[float], float]
    to_sample: Callable[[float], float]
    tick_seconds: float
    resolution: float

    def readout(self, sample):
        return format_seconds(self.to_ticks(sample) * self.tick_seconds, self.resolution)

    def delta(self, first, second):
        ticks = abs(self.to_ticks(second) - self.to_ticks(first))
        return format_seconds(ticks * self.tick_seconds, self.resolution)

    def ruler(self, left, right, pixels, spacing=110):
        """(sample, label) pairs for grid lines between two sample positions."""
        low, high = self.to_ticks(left), self.to_ticks(right)
        if high <= low:
            return []
        step = nice_step((high - low) / max(1.0, pixels / spacing))
        step_seconds = step * self.tick_seconds
        first = math.ceil(low / step - 1e-9)
        last = math.floor(high / step + 1e-9)
        return [(self.to_sample(k * step), format_ruler(k * step * self.tick_seconds, step_seconds))
                for k in range(max(0, first), last + 1)]


def sample_axis_ruler(left, right, pixels, spacing=110):
    """Grid for captures without time information: whole sample indices."""
    step = max(1, int(nice_step((right - left) / max(1.0, pixels / spacing))))
    first = max(0, math.ceil(left / step))
    return [(k * step, group_digits(k * step)) for k in range(first, math.floor(right / step) + 1)]


def capture_axis(capture, sample_period_ns=0.0):
    """Simulator time when the capture has timestamps, otherwise the user's sample period."""
    if capture is None:
        return None
    if capture.times is not None:
        seconds = unit_seconds(capture.timescale) or 1e-9
        return TimeAxis(capture.time_at, capture.sample_at_time, seconds, seconds)
    if sample_period_ns and sample_period_ns > 0:
        period = float(sample_period_ns)
        return TimeAxis(lambda sample: sample * period, lambda ticks: ticks / period, 1e-9, 1e-12)
    return None


def parse_clock(text):
    """Parse ``100 MHz``, ``25MHz``, ``10 ns`` or ``10`` (ns) into a period in ns."""
    match = re.fullmatch(r"\s*([0-9]+(?:[.,][0-9]+)?)\s*([a-zA-Zµ]*)\s*", text or "")
    if not match:
        raise ValueError("Введите частоту (например 100 MHz) или период (например 10 ns)")
    value = float(match[1].replace(",", "."))
    unit = match[2].lower().replace("µ", "u")
    if value <= 0:
        raise ValueError("Значение должно быть больше нуля")
    frequency = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}
    if unit in frequency:
        return 1e9 / (value * frequency[unit])
    period = {"": 1.0, "ns": 1.0, "ps": 1e-3, "fs": 1e-6, "us": 1e3, "ms": 1e6, "s": 1e9}
    if unit in period:
        return value * period[unit]
    raise ValueError(f"Неизвестная единица: {match[2]}")
