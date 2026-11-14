# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Field-by-field comparison of two loaded captures (VCD optimizations against the oracle)."""
import numpy as np


def capture_differences(actual, expected):
    """Every difference between two captures as text; empty when they are equal."""
    problems = []
    if actual.count != expected.count:
        problems.append(f"count {actual.count} != {expected.count}")
    if actual.timescale != expected.timescale:
        problems.append(f"timescale {actual.timescale!r} != {expected.timescale!r}")
    if not np.array_equal(np.asarray(actual.times), np.asarray(expected.times)):
        problems.append("times differ")
    names = [s.name for s in actual.signals]
    if names != [s.name for s in expected.signals]:
        problems.append(f"signal names differ: {names} != {[s.name for s in expected.signals]}")
        return problems
    for got, want in zip(actual.signals, expected.signals):
        for field in ("radix", "width", "explicit_width", "values", "value_ids"):
            if getattr(got, field) != getattr(want, field):
                problems.append(f"{got.name}.{field}: {getattr(got, field)!r} != {getattr(want, field)!r}")
        for field, dtype in (("starts", np.int64), ("codes", np.uint32)):
            a, b = np.asarray(getattr(got, field)), np.asarray(getattr(want, field))
            if a.dtype != dtype or b.dtype != dtype or not np.array_equal(a, b):
                problems.append(f"{got.name}.{field} differ ({a.dtype}/{len(a)} vs {b.dtype}/{len(b)})")
    split = {name: [s.name for s in bits] for name, bits in (actual.split_buses or {}).items()}
    if split != {name: [s.name for s in bits] for name, bits in (expected.split_buses or {}).items()}:
        problems.append("split_buses differ")
    if list(split) != list(expected.split_buses or {}):
        problems.append("split_buses order differs")
    return problems
