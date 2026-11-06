# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Vivado CSV reader. Transition arrays live on disk, not in Python lists."""
from __future__ import annotations

import concurrent.futures
import csv
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
        return max(0, int(np.searchsorted(self.starts, sample, side="right")) - 1)

    def value_at(self, sample: int):
        return self.values[int(self.codes[self.run_index(sample)])]

    def format_value(self, value: int | str, radix: str = "HEX") -> str:
        return FORMATS.format(value, self.width, radix, source_radix=self.radix)

    def transition(self, sample: int, direction: int) -> int | None:
        # Row zero is the start of capture, not a transition.
        if direction > 0:
            index = int(np.searchsorted(self.starts, sample, side="right"))
        else:
            index = int(np.searchsorted(self.starts, sample, side="left")) - 1
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
            pivot = int(np.searchsorted(self.starts, sample, side="right"))
            ranges = [(pivot, n, False), (0, pivot, True)]
        else:
            pivot = int(np.searchsorted(self.starts, sample, side="left"))
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
    storage: tempfile.TemporaryDirectory
    derived: list = field(default_factory=list)
    times: np.ndarray | None = None
    timescale: str = ""
    split_buses: dict[str, list[SignalData]] = field(default_factory=dict)

    def time_at(self, sample):
        """Interpolate a simulator timestamp at an event index, including the end boundary."""
        if self.times is None:
            return sample
        index = float(sample)
        if self.count == 1:
            return float(self.times[0]) + index
        if index >= self.count - 1:
            return float(self.times[-1]) + (index - self.count + 1)
        low = max(0, int(index))
        return float(self.times[low]) + (index - low) * int(self.times[low + 1] - self.times[low])

    def sample_at_time(self, timestamp):
        if self.times is None:
            return float(timestamp)
        if self.count == 1:
            return float(timestamp) - float(self.times[0])
        if timestamp >= self.times[-1]:
            return self.count - 1 + (timestamp - float(self.times[-1]))
        index = max(0, int(np.searchsorted(self.times, timestamp, side="right")) - 1)
        return index + (timestamp - float(self.times[index])) / int(self.times[index + 1] - self.times[index])

    def samples_at_times(self, timestamps):
        """Vectorized inverse for viewport-width rendering only."""
        ticks = np.asarray(timestamps, dtype=np.float64)
        if self.count == 1:
            return ticks - float(self.times[0])
        indices = np.clip(np.searchsorted(self.times, ticks, side="right") - 1, 0, self.count - 2)
        low = self.times[indices].astype(np.float64)
        high = self.times[indices + 1].astype(np.float64)
        result = indices + (ticks - low) / (high - low)
        beyond = ticks >= self.times[-1]
        if np.any(beyond):
            result[beyond] = self.count - 1 + (ticks[beyond] - float(self.times[-1]))
        return result

    def format_time(self, sample):
        if self.times is None:
            return str(sample)
        return self.format_ticks(self.time_at(sample))

    def format_ticks(self, ticks):
        seconds = unit_seconds(self.timescale)
        if seconds:
            return format_seconds(ticks * seconds, seconds)
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
             cancel: Callable[[], bool] = lambda: False, pool=None) -> Capture:
    """Load a Vivado ILA CSV; ``pool`` (a WorkerPool) splits large files over processes."""
    path = Path(path).resolve()
    header, radices, body = _csv_header(path)
    specs = _column_specs(header, radices)
    storage = tempfile.TemporaryDirectory(prefix="ila_wave_")
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
            for block, block_lines in iter_blocks(stream, body, None):
                if cancel():
                    raise LoadCancelled()
                try:
                    cells, numbers = split_block(block, len(specs), lines)
                    for encoder, column in zip(encoders, cells):
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
        merged.append(_plan_column(folder, index, spec, results, jobs))
    _run([pool.submit(place_range, str(folder), f"part{k}", chunk_jobs) for k, chunk_jobs in enumerate(jobs)],
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
        concurrent.futures.wait(futures)
        raise


def _plan_column(folder, index, spec, results, jobs):
    """Global value codes and the place of every range in the final column file.

    Local codes map to global ones in first-appearance order; a range whose first run
    repeats the previous range's last value drops that run. The writing itself runs in
    the workers (``place_range``), each into its own slice of a preallocated file.
    """
    table, width = ValueTable(), spec.width
    last = None
    row_offset = offset = 0
    for k, result in enumerate(results):
        runs, exported, local_width, first, final = result["columns"][index]
        if spec.meta:
            jobs[k].append((index, None, 0, 0, offset))
            offset += runs
            continue
        lut = table.lut_for(exported)
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
