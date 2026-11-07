# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Vectorized Vivado CSV body parsing; Qt-free, so worker processes can import it.

A block of complete lines is split into cells with numpy on the raw bytes. Each
column becomes a fixed-width byte array, neighbouring cells are compared as arrays
and only cells that differ from the previous row are parsed. A raw text is parsed
once per column (``ColumnEncoder.cache``), so a toggling bit costs one dictionary
lookup per change instead of a full ``parse_value`` call.

Vivado CSV has no quoted cells. A block that contains a quote goes through the
``csv`` module instead, so quoted files still load (on one process).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
import io
import os
from pathlib import Path

import numpy as np

BLOCK_BYTES = 8 << 20
GATHER_BUDGET = 16 << 20  # padded output limit; gather scratch is separately batched
_COMMA, _NEWLINE, _RETURN = 44, 10, 13
_BLANK = np.zeros(256, dtype=bool)
_BLANK_BYTES = bytes([_COMMA, 32, 9, _RETURN, _NEWLINE])
_BLANK[list(_BLANK_BYTES)] = True


class LoadCancelled(Exception):
    pass


class CellError(ValueError):
    """A bad cell or row; ``line`` counts physical lines from the start of the parsed range."""

    def __init__(self, line, message, column=None):
        super().__init__(message)
        self.line = line
        self.column = column


class QuotedCsv(Exception):
    """The range contains quotes; the parallel path hands the file to one process."""


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    radix: str
    width: int = 1
    explicit: bool = False
    meta: bool = False


class _FileSink:
    def __init__(self, start_path, code_path):
        self.start_path, self.code_path = Path(start_path), Path(code_path) if code_path else None
        self.sf = self.start_path.open("wb")
        self.cf = self.code_path.open("wb") if self.code_path else None
        self.runs = 0

    def write(self, starts, codes=None):
        np.asarray(starts, dtype=np.int64).tofile(self.sf)
        if self.cf is not None and codes is not None:
            np.asarray(codes, dtype=np.uint32).tofile(self.cf)
        self.runs += len(starts)

    def close(self):
        for stream in (self.sf, self.cf):
            if stream is not None and not stream.closed:
                stream.close()


class ValueTable:
    """Distinct values of a column in first-appearance order; ints are looked up with numpy.

    ``values[code]`` is the value, ``ids[value]`` its code. A sorted int64 index of
    the integer values serves whole arrays at once, so a counter or a data bus costs
    one ``np.unique`` per block instead of a dictionary lookup per change.
    """

    def __init__(self):
        self.values = []
        self.ids = {}
        self._keys = np.empty(0, dtype=np.int64)
        self._codes = np.empty(0, dtype=np.uint32)
        self._dirty = False

    def code(self, value):
        code = self.ids.get(value)
        if code is None:
            code = self.ids[value] = len(self.values)
            self.values.append(value)
            self._dirty = self._dirty or type(value) is int
        return code

    def _index(self):
        if self._dirty:
            pairs = [(v, c) for c, v in enumerate(self.values) if type(v) is int and _INT64[0] <= v <= _INT64[1]]
            keys = np.fromiter((v for v, _ in pairs), dtype=np.int64, count=len(pairs))
            codes = np.fromiter((c for _, c in pairs), dtype=np.uint32, count=len(pairs))
            order = np.argsort(keys, kind="stable")
            self._keys, self._codes, self._dirty = keys[order], codes[order], False
        return self._keys, self._codes

    def codes_for(self, numbers):
        """Codes of int64 ``numbers``; unseen values get new codes in order of first appearance.

        Returns (codes, index in ``numbers`` of the first unseen value or -1, its largest new value).
        """
        unique, first, inverse = np.unique(numbers, return_index=True, return_inverse=True)
        keys, codes = self._index()
        position = np.minimum(np.searchsorted(keys, unique), max(0, len(keys) - 1))
        known = (keys[position] == unique) if len(keys) else np.zeros(len(unique), dtype=bool)
        result = np.empty(len(unique), dtype=np.uint32)
        result[known] = codes[position[known]]
        fresh = np.flatnonzero(~known)
        first_new, largest = -1, None
        if len(fresh):
            fresh = fresh[np.argsort(first[fresh], kind="stable")]
            new_codes = np.arange(len(self.values), len(self.values) + len(fresh), dtype=np.uint32)
            result[fresh] = new_codes
            added = unique[fresh].tolist()
            self.values.extend(added)
            self.ids.update(zip(added, new_codes.tolist()))
            merged_keys = np.concatenate((keys, unique[fresh]))
            merged_codes = np.concatenate((codes, new_codes))
            order = np.argsort(merged_keys, kind="stable")
            self._keys, self._codes = merged_keys[order], merged_codes[order]
            first_new, largest = int(first[fresh[0]]), int(unique[fresh].max())
        return result[inverse.reshape(-1)], first_new, largest

    def lut_for(self, exported, cancel=lambda: False, chunk_values=262144):
        """Global codes for a worker's local table (see ``ColumnEncoder.exported_values``)."""
        kind = exported[0]
        if kind == "list":
            values = exported[1]
            result = np.empty(len(values), dtype=np.uint32)
            for a in range(0, len(values), chunk_values):
                if cancel():
                    raise LoadCancelled()
                b = min(len(values), a + chunk_values)
                result[a:b] = np.fromiter(map(self.code, values[a:b]), dtype=np.uint32, count=b - a)
            return result
        if kind == "ints":
            values = exported[1]
            result = np.empty(len(values), dtype=np.uint32)
            for a in range(0, len(values), chunk_values):
                if cancel():
                    raise LoadCancelled()
                result[a:a + chunk_values] = self.codes_for(values[a:a + chunk_values])[0]
            return result
        _, ints, positions, others = exported
        lut = np.empty(len(ints) + len(others), dtype=np.uint32)
        begin = 0
        # Walk in local code order so new values keep their first-appearance order.
        for code, value in [*others, (len(lut), None)]:
            if cancel():
                raise LoadCancelled()
            stop = int(np.searchsorted(positions, code))
            if stop > begin:
                for a in range(begin, stop, chunk_values):
                    if cancel():
                        raise LoadCancelled()
                    b = min(stop, a + chunk_values)
                    lut[positions[a:b]] = self.codes_for(ints[a:b])[0]
            if value is not None:
                lut[code] = self.code(value)
            begin = stop
        return lut


_INT64 = (-(1 << 63), (1 << 63) - 1)
# Bases whose plain digit cells are parsed with numpy; SIGNED keeps the scalar parser.
_FAST_BASES = {"HEX": 16, "BINARY": 2, "BIN": 2, "OCTAL": 8, "OCT": 8, "UNSIGNED": 10, "DECIMAL": 10, "DEC": 10}
_MAX_DIGITS = {16: 15, 2: 62, 8: 20, 10: 18}
_DIGIT = np.full(256, 255, dtype=np.uint8)
for _value, _chars in enumerate(zip("0123456789abcdef", "0123456789ABCDEF")):
    for _char in _chars:
        _DIGIT[ord(_char)] = _value
_DIGIT[0] = 254  # padding of the fixed-width cell array


def parse_digits(cells, base):
    """Plain digit cells -> (int64 values, ok mask); cells with X/Z, 0x, _ … are not ok."""
    width = cells.dtype.itemsize
    if cells.dtype.kind != "S":
        return None, np.zeros(len(cells), dtype=bool)
    if not len(cells):
        return np.zeros(0, dtype=np.int64), np.ones(0, dtype=bool)
    if width > _MAX_DIGITS[base]:
        return None, np.zeros(len(cells), dtype=bool)
    digits = _DIGIT[cells.view(np.uint8).reshape(len(cells), width)]
    present = digits != 254
    ok = present[:, 0] & ~((digits >= base) & present).any(axis=1)
    if width > 1:
        # Only trailing NULs are padding; an embedded NUL is data (the scalar parser rejects it).
        ok &= ~(present[:, 1:] & ~present[:, :-1]).any(axis=1)
    if not ok.all():
        digits = np.where(ok[:, None], digits, 0)
        present &= ok[:, None]
    result = np.zeros(len(cells), dtype=np.int64)
    for column in range(width):
        here = present[:, column]
        if here.all():
            result = result * base + digits[:, column]
        else:
            result[here] = result[here] * base + digits[here, column]
    return result, ok


class ColumnEncoder:
    """Run-length encodes one column over consecutive blocks; output goes to a sink."""

    def __init__(self, spec, sink, parse):
        self.spec = spec
        self.sink = sink
        self.parse = parse
        self.width = spec.width
        self.table = ValueTable()
        self.base = _FAST_BASES.get(spec.radix)
        self.cache = {}
        self.last_raw = None
        self.last_code = None
        self.first_code = None

    @property
    def values(self):
        return self.table.values

    @property
    def value_ids(self):
        return self.table.ids

    def feed(self, cells, rows, lines):
        """``cells`` holds one column of a block, ``rows`` the row index of its first cell."""
        if self.spec.meta:
            numbers = _decimal(cells)
            if numbers is None:
                numbers = np.empty(len(cells), dtype=np.int64)
                for index, raw in enumerate(cells.tolist()):
                    try:
                        numbers[index] = int(_text(raw).strip(), 10)
                    except (ValueError, OverflowError) as exc:
                        raise CellError(int(lines[index]), str(exc), self.spec.name) from exc
            self.sink.write(numbers)
            return
        count = len(cells)
        if not count:
            return
        changed = np.empty(count, dtype=bool)
        changed[0] = self.last_raw is None or cells[0] != self.last_raw
        np.not_equal(cells[1:], cells[:-1], out=changed[1:])
        self.last_raw = cells[-1]
        positions = np.flatnonzero(changed)
        if not len(positions):
            return
        changed_cells = cells[positions]
        numbers, ok = parse_digits(changed_cells, self.base) if self.base else (None, None)
        if numbers is not None and ok.all():
            codes = self._numeric(numbers, changed_cells, positions, lines)
        elif numbers is not None and np.count_nonzero(~ok) * 64 < len(ok):
            codes = self._mixed(numbers, ok, changed_cells, positions, lines)
        else:
            codes = self._scalar(changed_cells.tolist(), positions, lines)
        keep = np.empty(len(codes), dtype=bool)
        keep[0] = self.last_code is None or codes[0] != self.last_code
        np.not_equal(codes[1:], codes[:-1], out=keep[1:])
        self.last_code = int(codes[-1])
        if self.first_code is None:
            self.first_code = int(codes[0])
        if keep.any():
            self.sink.write(positions[keep].astype(np.int64) + rows, codes[keep])

    def _numeric(self, numbers, cells, positions, lines):
        spec = self.spec
        if spec.explicit:
            over = numbers >= (1 << self.width) if self.width < 63 else np.zeros(len(numbers), dtype=bool)
            if over.any():
                index = int(np.argmax(over))
                raise CellError(int(lines[positions[index]]),
                                f"значение {_text(bytes(cells[index]))} "
                                f"выходит за ширину {self.width} бит", spec.name)
        codes, _, largest = self.table.codes_for(numbers)
        if largest is not None and not spec.explicit:
            self.width = max(self.width, max(1, largest.bit_length()))
        return codes

    def _mixed(self, numbers, ok, cells, positions, lines):
        """Numeric runs through numpy, the few X/Z cells between them one by one, in order."""
        codes = np.empty(len(numbers), dtype=np.uint32)
        begin = 0
        for stop in [*np.flatnonzero(~ok).tolist(), len(numbers)]:
            if stop > begin:
                part = slice(begin, stop)
                codes[part] = self._numeric(numbers[part], cells[part], positions[part], lines)
            if stop < len(numbers):
                codes[stop] = self._scalar([bytes(cells[stop])], positions[stop:stop + 1], lines)[0]
            begin = stop + 1
        return codes

    def _scalar(self, raws, positions, lines):
        codes = list(map(self.cache.get, raws))
        if None in codes:
            for index, code in enumerate(codes):
                if code is None:
                    raw = raws[index]
                    code = self.cache.get(raw)
                    if code is None:
                        code = self._learn(raw, int(lines[positions[index]]))
                    codes[index] = code
        return np.fromiter(codes, dtype=np.uint32, count=len(codes))

    def _learn(self, raw, line):
        spec = self.spec
        text = _text(raw)
        try:
            value = self.parse(text, spec.radix)
            if isinstance(value, int):
                required = max(1, value.bit_length() + (1 if value < 0 or spec.radix == "SIGNED" else 0))
                if spec.explicit and (value >= (1 << self.width) or value < -(1 << (self.width - 1))):
                    raise ValueError(f"значение {text} выходит за ширину {self.width} бит")
                if spec.explicit and spec.radix == "SIGNED" and value >= (1 << (self.width - 1)):
                    # A two's-complement pattern (255 in 8 bits): store it as the signed value
                    # it displays as, so search and conditions compare the same number.
                    value -= 1 << self.width
                if not spec.explicit:
                    self.width = max(self.width, required)
            elif not spec.explicit:
                self.width = max(self.width, len(value) * {"HEX": 4, "OCTAL": 3, "OCT": 3}.get(spec.radix, 1))
        except (ValueError, OverflowError) as exc:
            raise CellError(line, str(exc), spec.name) from exc
        code = self.table.code(value)
        self.cache[raw] = code
        return code

    def exported_values(self):
        """The value table for the parent process, cheap to pickle and to merge.

        ``("ints", array)`` when every value is an int64; ``("mixed", ints, their codes,
        [(code, other value)])`` when a few X/Z texts sit between them; else ``("list", values)``.
        """
        values = self.table.values
        is_int = [type(v) is int and _INT64[0] <= v <= _INT64[1] for v in values]
        if all(is_int):
            return "ints", np.asarray(values, dtype=np.int64)
        others = [(code, value) for code, (value, ok) in enumerate(zip(values, is_int)) if not ok]
        if len(others) * 8 > len(values):
            return "list", values
        positions = np.flatnonzero(np.asarray(is_int, dtype=bool))
        ints = np.fromiter((values[i] for i in positions.tolist()), dtype=np.int64, count=len(positions))
        return "mixed", ints, positions, others


def _text(raw):
    return raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw


def split_block(block, columns, first_line=0):
    """Complete lines of ``block`` -> (one cell array per column, line number of each row).

    Blank lines and lines of empty cells are skipped like the ``csv`` reader path does.
    Line numbers are ``first_line`` + 1-based position in the block.
    """
    if b'"' in block:
        return _split_quoted(block, columns, first_line)
    buffer = np.frombuffer(block, dtype=np.uint8)
    newlines = np.flatnonzero(buffer == _NEWLINE)
    if not len(newlines) or newlines[-1] != len(buffer) - 1:
        raise ValueError("Блок CSV должен заканчиваться переводом строки")
    line_start = np.empty(len(newlines), dtype=np.int64)
    line_start[0] = 0
    line_start[1:] = newlines[:-1] + 1
    line_end = newlines.astype(np.int64)
    has_return = line_end > line_start
    has_return[has_return] = buffer[line_end[has_return] - 1] == _RETURN
    line_end[has_return] -= 1
    commas = np.flatnonzero(buffer == _COMMA)
    # Commas per line from a running count sampled at line boundaries.
    before = np.searchsorted(commas, line_start)
    per_line = np.searchsorted(commas, line_end) - before
    # Only a line that starts with a blank byte can consist of empty cells.
    keep = np.ones(len(newlines), dtype=bool)
    for index in np.flatnonzero(_BLANK[buffer[line_start]]):
        keep[index] = bool(block[line_start[index]:line_end[index]].strip(_BLANK_BYTES))
    bad = keep & (per_line != columns - 1)
    if bad.any():
        index = int(np.argmax(bad))
        raise CellError(first_line + index + 1,
                        f"ожидалось {columns} столбцов, получено {int(per_line[index]) + 1}.")
    if keep.all():
        numbers = np.arange(first_line + 1, first_line + 1 + len(newlines), dtype=np.int64)
        starts, ends, grid = line_start, line_end, commas
    else:
        numbers = np.flatnonzero(keep) + first_line + 1
        starts, ends = line_start[keep], line_end[keep]
        grid = commas[np.repeat(keep, per_line)]
    grid = grid.reshape(len(starts), columns - 1)
    cells = []
    for c in range(columns):
        left = starts if c == 0 else grid[:, c - 1] + 1
        right = ends if c == columns - 1 else grid[:, c]
        cells.append(_gather(buffer, left, right))
    return cells, numbers


def _gather(buffer, starts, ends):
    """Byte slices -> fixed-width cells, or compact objects for skewed cell widths."""
    lengths = ends - starts
    if not len(lengths):
        return np.zeros(0, dtype="S1")
    width = int(lengths.max())
    if width == 0:
        return np.zeros(len(starts), dtype="S1")
    scratch_per_row = width * 10
    if len(starts) * width > GATHER_BUDGET or scratch_per_row > GATHER_BUDGET:
        # Copy slices directly when padding or even one row's gather scratch is too large.
        return np.fromiter((buffer[a:b].tobytes() for a, b in zip(starts, ends)),
                           dtype=object, count=len(starts))
    if len(starts) * scratch_per_row > GATHER_BUDGET:
        # Keep ordinary numeric columns vectorized, splitting the gather scratch
        # space instead of falling back to Python merely because there are many rows.
        result = np.empty(len(starts), dtype=f"S{width}")
        batch = GATHER_BUDGET // scratch_per_row
        for a in range(0, len(starts), batch):
            b = min(len(starts), a + batch)
            result[a:b] = _gather(buffer, starts[a:b], ends[a:b])
        return result
    offsets = np.arange(width, dtype=np.int64)
    if int(lengths.min()) == width:
        matrix = buffer[starts[:, None] + offsets]
    else:
        index = np.minimum(starts[:, None] + offsets, len(buffer) - 1)
        matrix = buffer[index]
        matrix[offsets >= lengths[:, None]] = 0
    return matrix.view(f"S{width}").ravel()


def _decimal(cells):
    """Unsigned decimal cells -> int64 without Python per cell; None if any cell is not plain digits."""
    if not len(cells):
        return np.zeros(0, dtype=np.int64)
    width = cells.dtype.itemsize
    if cells.dtype.kind != "S" or width > 18:
        return None
    matrix = cells.view(np.uint8).reshape(len(cells), width)
    present = matrix != 0
    digits = matrix.astype(np.int64) - 48
    if ((digits < 0) | (digits > 9))[present].any() or not present[:, 0].all():
        return None
    if width > 1 and (present[:, 1:] & ~present[:, :-1]).any():
        return None  # embedded NUL: not padding, let the scalar path report the cell
    result = np.zeros(len(cells), dtype=np.int64)
    for column in range(width):
        here = present[:, column]
        result[here] = result[here] * 10 + digits[here, column]
    return result


def _split_quoted(block, columns, first_line):
    reader = csv.reader(io.StringIO(block.decode("utf-8", "replace"), newline=""))
    rows, numbers = [], []
    for row in reader:
        if not row or all(not cell.strip() for cell in row):
            continue
        if len(row) != columns:
            raise CellError(first_line + reader.line_num, f"ожидалось {columns} столбцов, получено {len(row)}.")
        rows.append(row)
        numbers.append(first_line + reader.line_num)
    cells = [_cell_array([cell.encode("utf-8") for cell in column]) if rows else np.zeros(0, "S1")
             for column in (zip(*rows) if rows else [()] * columns)]
    return cells, np.asarray(numbers, dtype=np.int64)


def _cell_array(values):
    width = max((len(value) for value in values), default=1)
    dtype = object if len(values) * width > GATHER_BUDGET else "S"
    return np.array(values, dtype=dtype)


def iter_blocks(stream, start, end, block_bytes=BLOCK_BYTES, cancel=lambda: False):
    """Yield complete CSV records; quoted newlines never split a record between blocks."""
    stream.seek(start)
    position, tail = start, b""
    while end is None or position < end:
        if cancel():
            raise LoadCancelled()
        size = block_bytes if end is None else min(block_bytes, end - position)
        data = stream.read(size)
        position += len(data)
        if not data:
            break
        data = tail + data
        if b'"' in data:
            buffer = np.frombuffer(data, dtype=np.uint8)
            quotes = np.flatnonzero(buffer == 34)
            newlines = np.flatnonzero(buffer == _NEWLINE)
            complete = newlines[np.searchsorted(quotes, newlines) % 2 == 0]
            cut = int(complete[-1]) + 1 if len(complete) else 0
        else:
            cut = data.rfind(b"\n") + 1
        if not cut:
            tail = data
            continue
        block, tail = data[:cut], data[cut:]
        yield block, block.count(b"\n")
    if tail:
        yield tail + b"\n", tail.count(b"\n") + 1


def line_aligned_ranges(path, start, parts):
    """Split [start, EOF) into up to ``parts`` ranges that begin at line starts."""
    size = os.path.getsize(path)
    bounds = [start]
    with open(path, "rb") as stream:
        for index in range(1, parts):
            guess = start + (size - start) * index // parts
            if guess <= bounds[-1]:
                continue
            stream.seek(guess)
            stream.readline()
            position = stream.tell()
            if bounds[-1] < position < size:
                bounds.append(position)
    bounds.append(size)
    return [(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]


def parse_range(path, start, end, specs, folder, tag, cancel_path=None):
    """Worker entry point: encode one byte range into per-column files in ``folder``.

    Returns per-column (runs, values, width), the row count and the physical line count.
    Rows and lines are local to the range; the caller adds the preceding offsets.
    A ``CellError`` comes back as a value so its local line can be made global.
    """
    from .data import parse_value
    folder = Path(folder)
    encoders = [ColumnEncoder(spec, _FileSink(folder / f"{tag}_{i}.starts",
                                              None if spec.meta else folder / f"{tag}_{i}.codes"), parse_value)
                for i, spec in enumerate(specs)]
    rows = lines = 0
    try:
        with open(path, "rb") as stream:
            for block, block_lines in iter_blocks(stream, start, end,
                                                  cancel=lambda: bool(cancel_path and os.path.exists(cancel_path))):
                if cancel_path and os.path.exists(cancel_path):
                    raise LoadCancelled()
                if b'"' in block:
                    raise QuotedCsv()
                cells, numbers = split_block(block, len(specs), lines)
                for encoder, column in zip(encoders, cells):
                    if cancel_path and os.path.exists(cancel_path):
                        raise LoadCancelled()
                    encoder.feed(column, rows, numbers)
                rows += len(numbers)
                lines += block_lines
    except CellError as exc:
        return {"error": (exc.line, exc.column, str(exc))}
    except QuotedCsv:
        return {"quoted": True}
    finally:
        for encoder in encoders:
            encoder.sink.close()
    return {"rows": rows, "lines": lines,
            "columns": [(e.sink.runs, e.exported_values(), e.width, e.first_code, e.last_code) for e in encoders]}


def place_range(folder, tag, jobs, cancel_path=None, chunk_runs=262144):
    """Merge disjoint worker slices with bounded buffers and cooperative cancellation."""
    folder = Path(folder)
    for index, lut, row_offset, drop, offset in jobs:
        start_path = folder / f"{tag}_{index}.starts"
        code_path = folder / f"{tag}_{index}.codes"
        with start_path.open("rb") as source, (folder / f"{index}.starts").open("r+b") as target:
            source.seek(drop * 8)
            target.seek(offset * 8)
            codes_source = code_path.open("rb") if lut is not None else None
            codes_target = (folder / f"{index}.codes").open("r+b") if lut is not None else None
            try:
                if codes_source is not None:
                    codes_source.seek(drop * 4)
                    codes_target.seek(offset * 4)
                while True:
                    if cancel_path and os.path.exists(cancel_path):
                        raise LoadCancelled()
                    starts = np.fromfile(source, dtype=np.int64, count=chunk_runs)
                    if not len(starts):
                        break
                    if codes_source is not None:
                        codes = np.fromfile(codes_source, dtype=np.uint32, count=len(starts))
                        lut[codes].tofile(codes_target)
                        starts += row_offset
                    starts.tofile(target)
            finally:
                if codes_source is not None:
                    codes_source.close()
                    codes_target.close()
        start_path.unlink()
        if lut is not None:
            code_path.unlink()
