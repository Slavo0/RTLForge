# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Vivado CSV reader. Transition arrays live on disk, not in Python lists."""
from __future__ import annotations

import csv
import re
import tempfile
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from .formats import FORMATS


class LoadCancelled(Exception):
    pass


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

    def append(self, row: int, raw: str):
        if self.signal is None:
            self.starts.append(int(raw.strip(), 10))
        else:
            if raw == self.last_raw:
                return
            self.last_raw = raw
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
            if code == self.last_code:
                return
            self.last_code = code
            self.starts.append(row)
            self.codes.append(code)
        if len(self.starts) >= 8192:
            self.flush()

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


def load_csv(path: str | Path, progress: Callable[[int, int], None] = lambda p, n: None,
             cancel: Callable[[], bool] = lambda: False) -> Capture:
    path = Path(path).resolve()
    storage = tempfile.TemporaryDirectory(prefix="ila_wave_")
    writers = []
    mapped = []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream)
            header = [s.strip() for s in next(reader, [])]
            radices = [s.strip().upper() for s in next(reader, [])]
            if not header or not radices or not radices[0].startswith("RADIX"):
                raise ValueError("Ожидаются заголовок Vivado CSV и строка 'Radix - ...'.")
            if len(header) != len(radices) or len(set(header)) != len(header):
                raise ValueError("Число столбцов заголовка и Radix различается или имена повторяются.")
            radices[0] = re.sub(r"^RADIX\s*-\s*", "", radices[0])
            signals = []
            for index, (name, radix) in enumerate(zip(header, radices)):
                if radix not in RADICES:
                    raise ValueError(f"{name}: неподдерживаемый Radix {radix}")
                sig = None
                if name.lower() not in META:
                    match = re.search(r"\[(\d+)\s*:\s*(\d+)\]$", name)
                    width = abs(int(match[1]) - int(match[2])) + 1 if match else 1
                    sig = SignalData(name, radix, width, bool(match))
                    signals.append(sig)
                writers.append(_Writer(Path(storage.name), index, sig))
            if not signals:
                raise ValueError("В CSV нет сигналов.")
            count = 0
            size = max(1, path.stat().st_size)
            for row in reader:
                if not row or all(not cell.strip() for cell in row):
                    continue
                if len(row) != len(header):
                    raise ValueError(f"Строка {reader.line_num}: ожидалось {len(header)} столбцов, получено {len(row)}.")
                for index, (writer, raw) in enumerate(zip(writers, row)):
                    try:
                        writer.append(count, raw)
                    except (ValueError, OverflowError) as exc:
                        raise ValueError(f"Строка {reader.line_num}, {header[index]}: {exc}") from exc
                count += 1
                if count % 4096 == 0:
                    if cancel():
                        raise LoadCancelled()
                    progress(min(99, int(stream.buffer.tell() / size * 100)), count)
            if cancel():
                raise LoadCancelled()
            if not count:
                raise ValueError("CSV не содержит отсчётов.")
            metadata = {}
            for name, writer in zip(header, writers):
                writer.close()
                starts = np.memmap(writer.start_path, dtype=np.int64, mode="r")
                mapped.append(starts)
                if writer.signal is not None:
                    codes = np.memmap(writer.code_path, dtype=np.uint32, mode="r")
                    mapped.append(codes)
                    writer.signal.starts, writer.signal.codes = starts, codes
                else:
                    metadata[name] = starts
            progress(100, count)
            return Capture(path, signals, count, metadata, storage)
    except BaseException:
        for writer in writers:
            writer.close()
        for data in mapped:
            data._mmap.close()
        storage.cleanup()
        raise
