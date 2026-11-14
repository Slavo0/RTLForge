# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Vivado CSV reader. Transition arrays live on disk, not in Python lists."""
from __future__ import annotations

import concurrent.futures
import csv
from decimal import Decimal
import math
import re
import tempfile
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from .csv_chunks import (CellError, ColumnEncoder, ColumnSpec, LoadCancelled, ValueTable, _FileSink, iter_blocks,
                         line_aligned_ranges, parse_range, place_range, split_block)
from .formats import FORMATS
from .timebase import format_seconds, group_digits, unit_seconds
from .tempstore import storage_or_default


RADICES = {"HEX": 16, "BINARY": 2, "BIN": 2, "OCTAL": 8,
           "OCT": 8, "UNSIGNED": 10, "SIGNED": 10, "DECIMAL": 10, "DEC": 10}
META = {"sample in buffer", "sample in window"}


def parse_value(text: str, radix: str) -> int | str:
    token = text.strip().replace("_", "")
    if not token:
        raise ValueError("пустое значение")
    base = RADICES.get(radix.upper())
    if base is None:
        raise ValueError(f"неподдерживаемая система счисления: {radix}")
    # Check numeric prefixes before X/Z, so 0xFF is a number, not unknown.
    digits = token.lower()
    if base == 16 and digits.startswith("0x"):
        digits = digits[2:]
    elif base == 2 and digits.startswith("0b"):
        digits = digits[2:]
    if any(c in digits for c in "xz?"):
        alphabet = {2: "01xz?", 8: "01234567xz?", 10: "xz?", 16: "0123456789abcdefxz?"}[base]
        if not digits or any(c not in alphabet for c in digits):
            raise ValueError(f"некорректное значение: {text}")
        return digits.upper()
    return int(token, base)


def search_sorted(array, keys, side="left"):
    """``numpy.searchsorted`` that never converts or copies the searched array.

    numpy brings both operands to a common type and to contiguous memory, so a float
    key on an int64 index or a strided view (a column of a 2-D array, a field of a
    structured array) copies the whole array on every call. Integer arrays are
    searched with integer keys instead; this gives the same answer for any real key:
    ``searchsorted(a, x, "right") == searchsorted(a, floor(x), "right")`` and
    ``searchsorted(a, x, "left") == searchsorted(a, ceil(x), "left")``. Keys beyond the
    dtype's range, infinities and NaN (sorted last, as numpy does) are answered without
    searching. Strided arrays are bisected in place, reading O(keys * log n) elements.

    Returns a Python int for a scalar key and an int64 array of the key's shape otherwise.
    """
    scalar = np.ndim(keys) == 0
    flat = np.asarray(keys).reshape(-1)
    if array.dtype.kind not in "iu":
        result = np.asarray(_search_inside(array, flat, side), dtype=np.int64)
        return int(result[0]) if scalar else result.reshape(np.shape(keys))
    info = np.iinfo(array.dtype)
    if flat.dtype.kind == "f":
        flat = np.floor(flat) if side == "right" else np.ceil(flat)
        low, high = flat < info.min, (flat > info.max) | np.isnan(flat)
    elif flat.dtype.kind in "iu":
        low, high = flat < info.min, flat > info.max
    else:  # Python integers beyond 64 bits arrive as objects
        low = np.array([key < info.min for key in flat.tolist()], dtype=bool)
        high = np.array([key > info.max for key in flat.tolist()], dtype=bool)
    inside = ~(low | high)
    if flat.dtype != array.dtype:
        converted = np.zeros(len(flat), dtype=array.dtype)
        converted[inside] = flat[inside].astype(array.dtype)
        flat = converted
    result = np.where(high, len(array), 0).astype(np.int64)
    if inside.all():
        result = np.asarray(_search_inside(array, flat, side), dtype=np.int64)
    elif inside.any():
        result[inside] = _search_inside(array, flat[inside], side)
    return int(result[0]) if scalar else result.reshape(np.shape(keys))


def _search_inside(array, keys, side):
    if array.ndim != 1 or array.flags.c_contiguous:
        return np.searchsorted(array, keys, side=side)
    if len(keys) * max(1, len(array)).bit_length() > len(array):
        # Bulk lookups (exports, sampling) read the whole array anyway: copy it once.
        return np.searchsorted(np.ascontiguousarray(array), keys, side=side)
    # Vectorized bisection on the strided view, instead of numpy's contiguous copy.
    lo = np.zeros(len(keys), dtype=np.int64)
    hi = np.full(len(keys), len(array), dtype=np.int64)
    active = np.flatnonzero(lo < hi)
    while len(active):
        middle = (lo[active] + hi[active]) >> 1
        values = array[middle]
        right = values <= keys[active] if side == "right" else values < keys[active]
        lo[active] = np.where(right, middle + 1, lo[active])
        hi[active] = np.where(right, hi[active], middle)
        active = active[lo[active] < hi[active]]
    return lo


@dataclass
class SignalData:
    name: str
    radix: str
    width: int = 1
    explicit_width: bool = False
    values: list[int | str] = field(default_factory=list)
    starts: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.int64))
    codes: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.uint32))
    value_ids: dict[int | str, int] = field(default_factory=dict, repr=False)

    @property
    def short_name(self):
        return self.name.rsplit("/", 1)[-1]

    @property
    def is_bus(self):
        return self.width > 1

    def run_index(self, sample: int | float) -> int:
        return max(0, search_sorted(self.starts, sample, "right") - 1)

    def value_at(self, sample: int):
        return self.values[int(self.codes[self.run_index(sample)])]

    def format_value(self, value: int | str, radix: str = "HEX") -> str:
        return FORMATS.format(value, self.width, radix, source_radix=self.radix)

    def transition(self, sample: int, direction: int) -> int | None:
        # Row zero is the start of capture, not a transition.
        if direction > 0:
            index = search_sorted(self.starts, sample, "right")
        else:
            index = search_sorted(self.starts, sample, "left") - 1
        return int(self.starts[index]) if 0 < index < len(self.starts) else None

    def search(self, value: int | str, sample: int, direction: int,
               cancel: Callable[[], bool] = lambda: False,
               wrap: bool = True) -> tuple[int, bool] | None:
        """Find next matching interval start in bounded blocks; wrap if requested."""
        code = self.value_ids.get(value)
        if code is None:
            return None
        n = len(self.starts)
        if direction > 0:
            pivot = search_sorted(self.starts, sample, "right")
            ranges = [(pivot, n, False), (0, pivot, True)]
        else:
            pivot = search_sorted(self.starts, sample, "left")
            ranges = [(0, pivot, False), (pivot, n, True)]
        if not wrap:
            ranges = ranges[:1]
        for lo, hi, wrapped in ranges:
            while lo < hi:
                if cancel():
                    raise LoadCancelled()
                a, b = (lo, min(hi, lo + 262144)) if direction > 0 else (max(lo, hi - 262144), hi)
                hits = np.flatnonzero(self.codes[a:b] == code)
                if len(hits):
                    index = a + int(hits[0 if direction > 0 else -1])
                    return int(self.starts[index]), wrapped
                if direction > 0:
                    lo = b
                else:
                    hi = a
        return None

    def query_value(self, text: str, radix: str) -> int | str:
        value = FORMATS.parse(text, self.width, radix, source_radix=self.radix)
        if isinstance(value, int):
            bits = value & ((1 << self.width) - 1)
            value = bits - (1 << self.width) if self.radix == "SIGNED" and bits & (1 << (self.width - 1)) else bits
        return value


@dataclass
class Capture:
    path: Path
    signals: list[SignalData]
    count: int
    metadata: dict[str, np.ndarray]
    storage: object  # tempstore.TempFolder (or anything with ``name`` and ``cleanup()``)
    derived: list = field(default_factory=list)
    times: np.ndarray | None = None
    timescale: str = ""
    split_buses: dict[str, list[SignalData]] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)  # load messages for the status bar

    def _time_parts(self, sample):
        index = float(sample)
        low = max(0, min(self.count - 1, int(index)))
        if low == self.count - 1:
            return int(self.times[low]), index - low
        return int(self.times[low]), (index - low) * int(self.times[low + 1] - self.times[low])

    def time_at(self, sample):
        """Absolute ticks; integral positions stay Python integers, fractions use Decimal."""
        if self.times is None:
            return sample
        base, fraction = self._time_parts(sample)
        return base if not fraction else Decimal(base) + Decimal(str(fraction))

    def time_offset_at(self, sample, origin=None):
        """Subtract an integer local origin *before* converting to floating point."""
        base, fraction = self._time_parts(sample)
        origin = int(self.times[0]) if origin is None else int(origin)
        return (base - origin) + fraction

    def sample_at_time(self, timestamp):
        if self.times is None:
            return float(timestamp)
        # An integer key keeps numpy from converting the whole int64 index (see search_sorted).
        index = max(0, search_sorted(self.times, math.floor(timestamp), "right") - 1)
        offset = float(timestamp - int(self.times[index]))
        if index == self.count - 1:
            return index + offset
        return index + offset / int(self.times[index + 1] - self.times[index])

    def samples_at_offsets(self, offsets, origin=None):
        """Viewport-sized inverse using offsets from an integer timestamp origin."""
        origin = int(self.times[0]) if origin is None else int(origin)
        shape = np.shape(offsets)
        ticks = np.asarray(offsets, dtype=np.float64).reshape(-1)
        # Query origin + floor(t) clamped to [0, 2**63 - 1], in integers (no float rounding).
        floors = np.floor(ticks)
        huge = np.abs(floors) > 2.0**62
        whole = np.where(huge, 0, floors).astype(np.int64)
        with np.errstate(over="ignore"):  # sums beyond the limit are replaced below
            queries = whole + np.int64(origin)
        queries[whole > np.int64(2**63 - 1 - origin)] = 2**63 - 1
        np.maximum(queries, 0, out=queries)
        if huge.any():  # offsets beyond ±2**62 ticks take the exact per-element rule
            queries[huge] = [max(0, min(2**63 - 1, origin + math.floor(t))) for t in ticks[huge]]
        ticks, queries = ticks.reshape(shape), queries.reshape(shape)
        indices = np.clip(search_sorted(self.times, queries, "right") - 1, 0, self.count - 1)
        low = (self.times[indices] - origin).astype(np.float64)
        following = np.minimum(indices + 1, self.count - 1)
        widths = np.maximum(1, self.times[following] - self.times[indices])
        return indices + (ticks - low) / widths

    def samples_at_times(self, timestamps):
        """Absolute integer ticks retain precision; rendering uses local offsets instead."""
        ticks = np.asarray(timestamps)
        if ticks.dtype.kind in "iu":
            ticks = ticks.astype(np.int64)
            indices = np.clip(search_sorted(self.times, ticks, "right") - 1, 0, self.count - 1)
            following = np.minimum(indices + 1, self.count - 1)
            widths = np.maximum(1, self.times[following] - self.times[indices])
            return indices + (ticks - self.times[indices]) / widths
        return np.asarray([self.sample_at_time(t) for t in ticks.flat]).reshape(ticks.shape)

    def format_time(self, sample):
        if self.times is None:
            return str(sample)
        return self.format_ticks(self.time_at(sample))

    def format_ticks(self, ticks):
        seconds = unit_seconds(self.timescale)
        if seconds:
            return format_seconds(Decimal(str(ticks)) * Decimal(str(seconds)), seconds)
        return f"{group_digits(ticks, 3)} × {self.timescale}"

    def close(self):
        for result in self.derived:
            result.close()
        self.derived.clear()
        for sig in self.signals:
            for data in (sig.starts, sig.codes):
                if isinstance(data, np.memmap):
                    data._mmap.close()
        for data in self.metadata.values():
            if isinstance(data, np.memmap):
                data._mmap.close()
        if isinstance(self.times, np.memmap):
            self.times._mmap.close()
        self.storage.cleanup()


class _Writer:
    def __init__(self, folder: Path, index: int, signal: SignalData | None):
        self.signal = signal
        self.start_path = folder / f"{index}.starts"
        self.code_path = folder / f"{index}.codes"
        self.sf = self.start_path.open("wb")
        self.cf = self.code_path.open("wb") if signal else None
        self.starts = array("q")
        self.codes = array("I")
        self.last_raw = None
        self.last_code = None
        self.cache = {}

    def append(self, row: int, raw: str):
        if self.signal is None:
            self.starts.append(int(raw.strip(), 10))
        else:
            if raw == self.last_raw:
                return
            self.last_raw = raw
            code = self.cache.get(raw)
            if code is None:
                code = self.cache[raw] = self._learn(raw)
            if code == self.last_code:
                return
            self.last_code = code
            self.starts.append(row)
            self.codes.append(code)
        if len(self.starts) >= 8192:
            self.flush()

    def _learn(self, raw):
        """Parse a raw text once; later occurrences reuse its code."""
        sig = self.signal
        value = parse_value(raw, sig.radix)
        if isinstance(value, int):
            required = max(1, value.bit_length() + (1 if value < 0 or sig.radix == "SIGNED" else 0))
            if sig.explicit_width and (value >= (1 << sig.width) or value < -(1 << (sig.width - 1))):
                raise ValueError(f"значение {raw} выходит за ширину {sig.width} бит")
            if not sig.explicit_width:
                sig.width = max(sig.width, required)
        elif not sig.explicit_width:
            sig.width = max(sig.width, len(value) * {"HEX": 4, "OCTAL": 3, "OCT": 3}.get(sig.radix, 1))
        code = sig.value_ids.get(value)
        if code is None:
            code = len(sig.values)
            sig.value_ids[value] = code
            sig.values.append(value)
        return code

    def flush(self):
        self.starts.tofile(self.sf)
        self.starts = array("q")
        if self.cf:
            self.codes.tofile(self.cf)
            self.codes = array("I")

    def close(self):
        if not self.sf.closed:
            self.flush()
            self.sf.close()
            if self.cf:
                self.cf.close()


MIN_PARALLEL_CHUNK = 16 << 20  # bytes per worker; smaller files stay on one process


def _csv_header(path):
    """(header, radices, offset of the first data line)."""
    with path.open("rb") as stream:
        first, second = stream.readline(), stream.readline()
        offset = stream.tell()
    header = [s.strip() for s in next(csv.reader([first.decode("utf-8-sig")]), [])]
    radices = [s.strip().upper() for s in next(csv.reader([second.decode("utf-8")]), [])]
    if not header or not radices or not radices[0].startswith("RADIX"):
        raise ValueError("Ожидаются заголовок Vivado CSV и строка 'Radix - ...'.")
    if len(header) != len(radices) or len(set(header)) != len(header):
        raise ValueError("Число столбцов заголовка и Radix различается или имена повторяются.")
    radices[0] = re.sub(r"^RADIX\s*-\s*", "", radices[0])
    return header, radices, offset


def _column_specs(header, radices):
    specs = []
    for name, radix in zip(header, radices):
        if radix not in RADICES:
            raise ValueError(f"{name}: неподдерживаемый Radix {radix}")
        match = re.search(r"\[(\d+)\s*:\s*(\d+)\]$", name)
        width = abs(int(match[1]) - int(match[2])) + 1 if match else 1
        specs.append(ColumnSpec(name, radix, width, bool(match), name.lower() in META))
    if all(spec.meta for spec in specs):
        raise ValueError("В CSV нет сигналов.")
    return specs


def _cell_error(exc, line_offset):
    line = exc.line + line_offset
    where = f"Строка {line}, {exc.column}" if exc.column else f"Строка {line}"
    return ValueError(f"{where}: {exc}")


def load_csv(path: str | Path, progress: Callable[[int, int], None] = lambda p, n: None,
             cancel: Callable[[], bool] = lambda: False, pool=None, temp=None) -> Capture:
    """Load a Vivado ILA CSV; ``pool`` (a WorkerPool) splits large files over processes.

    ``temp`` (a ``tempstore.TempStorage``) creates the index folder; default: the system one.
    """
    path = Path(path).resolve()
    header, radices, body = _csv_header(path)
    specs = _column_specs(header, radices)
    storage = storage_or_default(temp).create("ila_wave_")
    folder = Path(storage.name)
    mapped = []
    try:
        size = path.stat().st_size
        workers = pool.workers_for(size - body, MIN_PARALLEL_CHUNK) if pool is not None else 1
        loaded = None
        if workers > 1:
            loaded = _load_parallel(path, body, specs, folder, pool, workers, progress, cancel)
        if loaded is None:
            loaded = _load_single(path, body, specs, folder, progress, cancel)
        count, columns = loaded
        if not count:
            raise ValueError("CSV не содержит отсчётов.")
        signals, metadata = [], {}
        for index, (spec, (values, value_ids, width)) in enumerate(zip(specs, columns)):
            starts = np.memmap(folder / f"{index}.starts", dtype=np.int64, mode="r")
            mapped.append(starts)
            if spec.meta:
                metadata[spec.name] = starts
                continue
            codes = np.memmap(folder / f"{index}.codes", dtype=np.uint32, mode="r")
            mapped.append(codes)
            signals.append(SignalData(spec.name, spec.radix, width, spec.explicit, values, starts, codes, value_ids))
        progress(100, count)
        return Capture(path, signals, count, metadata, storage)
    except BaseException:
        for data in mapped:
            data._mmap.close()
        storage.cleanup()
        raise


def _load_single(path, body, specs, folder, progress, cancel):
    encoders = [ColumnEncoder(spec, _FileSink(folder / f"{i}.starts", None if spec.meta else folder / f"{i}.codes"),
                              parse_value) for i, spec in enumerate(specs)]
    rows = lines = 0
    size = max(1, path.stat().st_size)
    try:
        with path.open("rb") as stream:
            for block, block_lines in iter_blocks(stream, body, None, cancel=cancel):
                if cancel():
                    raise LoadCancelled()
                try:
                    cells, numbers = split_block(block, len(specs), lines)
                    for encoder, column in zip(encoders, cells):
                        if cancel():
                            raise LoadCancelled()
                        encoder.feed(column, rows, numbers)
                except CellError as exc:
                    raise _cell_error(exc, 2) from exc
                rows += len(numbers)
                lines += block_lines
                progress(min(99, int(stream.tell() / size * 100)), rows)
        if cancel():
            raise LoadCancelled()
    finally:
        for encoder in encoders:
            encoder.sink.close()
    return rows, [(e.values, e.value_ids, e.width) for e in encoders]


def _load_parallel(path, body, specs, folder, pool, workers, progress, cancel):
    """Encode line-aligned byte ranges in worker processes, then merge them in order.

    Workers write local runs to files in the capture folder; only value tables come
    back through pickling. Returns None when the file needs the quoted (csv) path.
    """
    ranges = line_aligned_ranges(path, body, workers)
    flag = folder / "cancel.flag"
    futures = [pool.submit(parse_range, str(path), a, b, specs, str(folder), f"part{k}", str(flag))
               for k, (a, b) in enumerate(ranges)]
    results = _run(futures, flag, cancel, lambda done, total: progress(min(90, 90 * done // total), 0))
    if any(result.get("quoted") for result in results):
        for file in folder.glob("part*"):
            file.unlink(missing_ok=True)
        return None
    line_offset = 2
    for result in results:
        if "error" in result:
            line, column, message = result["error"]
            raise _cell_error(CellError(line, message, column), line_offset)
        line_offset += result["lines"]
    merged, jobs = [], [[] for _ in results]
    for index, spec in enumerate(specs):
        if cancel():
            raise LoadCancelled()
        merged.append(_plan_column(folder, index, spec, results, jobs, cancel))
    _run([pool.submit(place_range, str(folder), f"part{k}", chunk_jobs, str(flag)) for k, chunk_jobs in enumerate(jobs)],
         flag, cancel)
    rows = sum(result["rows"] for result in results)
    progress(99, rows)
    return rows, merged


def _run(futures, flag, cancel, progress=None):
    try:
        while True:
            done, pending = concurrent.futures.wait(futures, timeout=0.05)
            if cancel():
                raise LoadCancelled()
            if progress:
                progress(len(done), len(futures))
            if not pending:
                return [future.result() for future in futures]
    except BaseException:
        # Workers stop at their next block; their files must be closed before cleanup.
        flag.touch()
        for future in futures:
            future.cancel()  # queued ranges need not run before the capture can be released
        concurrent.futures.wait(futures)
        raise


def _plan_column(folder, index, spec, results, jobs, cancel=lambda: False):
    """Global value codes and the place of every range in the final column file.

    Local codes map to global ones in first-appearance order; a range whose first run
    repeats the previous range's last value drops that run. The writing itself runs in
    the workers (``place_range``), each into its own slice of a preallocated file.
    """
    table, width = ValueTable(), spec.width
    last = None
    row_offset = offset = 0
    for k, result in enumerate(results):
        if cancel():
            raise LoadCancelled()
        runs, exported, local_width, first, final = result["columns"][index]
        if spec.meta:
            jobs[k].append((index, None, 0, 0, offset))
            offset += runs
            continue
        lut = table.lut_for(exported, cancel)
        drop = int(bool(runs) and last is not None and int(lut[first]) == last)
        if runs:
            last = int(lut[final])
        jobs[k].append((index, lut, row_offset, drop, offset))
        offset += runs - drop
        row_offset += result["rows"]
        if not spec.explicit:
            width = max(width, local_width)
    for suffix, size in ((".starts", 8), *(() if spec.meta else ((".codes", 4),))):
        with open(folder / f"{index}{suffix}", "wb") as stream:
            stream.truncate(offset * size)
    return table.values, table.ids, width
