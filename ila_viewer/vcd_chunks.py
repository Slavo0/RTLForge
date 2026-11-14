# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Parallel VCD body parsing in worker processes; Qt-free.

The parent finds the end of the header and splits the body into byte ranges that each
start at a timestamp token (``range_starts``). A worker parses one range with row
numbers local to it (``parse_range``) and writes its timestamps and every signal's
local runs as segments of one file per range (file opens cost about 1 ms each on
Windows, so there is no file per signal per range). Only small data comes back: row
count, first and last timestamp, per signal the local value table, the run count and
the first and last local code, and the segment index. The parent maps local codes to
global ones; ``place_signals`` then copies the segments into the final index files.

The token loop follows ``VcdCaptureReader.load`` exactly. Changes go straight to the
signal's sink; only a second change of a signal within one timestamp is kept in a
dict and replaces the first one when the timestamp ends (the last change wins). A
range that fails runs once more in the order of the single-process loop (``exact``),
so the error that reaches the user is the one that loop would raise.
"""
from __future__ import annotations

from array import array
import contextlib
import os
import re
import time

import numpy as np

from .csv_chunks import LoadCancelled
from .data import SignalData, _Writer

ALLOWED = frozenset({"$dumpvars", "$dumpall", "$dumpon", "$dumpoff", "$end"})
# The ASCII characters str.split() treats as whitespace; non-ASCII bytes are never trusted.
_SPACE = b" \t\n\r\x0b\x0c\x1c\x1d\x1e\x1f"
_TOKEN = re.compile(rb"[^ \t\n\r\x0b\x0c\x1c-\x1f]+")
_IS_SPACE = np.zeros(256, dtype=bool)
_IS_SPACE[list(_SPACE)] = True
SEGMENT = 8192          # entries a sink keeps before writing a segment (96 KB, like a _Writer buffer)
BLOCK_BYTES = 8 << 20   # read size of a worker
TIMES = -1              # segment owner of the timestamps


class UnsafeRange(Exception):
    """The range holds a directive whose contents could hide a timestamp; parse on one process."""


def header_end(path, limit=64 << 20):
    """Byte offset just after the ``$end`` of ``$enddefinitions``, as ``_header`` reads it.

    None when the header is not plain ASCII or not found within ``limit`` bytes; the
    caller then parses on one process.
    """
    with open(path, "rb") as stream:
        prefix = stream.read(limit)
    tokens = _TOKEN.finditer(prefix)
    for directive in tokens:
        token = None
        for token in tokens:
            if token.group() == b"$end":
                break
        else:
            return None
        if token.end() >= len(prefix):
            return None  # the token may continue past the prefix
        if directive.group() == b"$enddefinitions":
            if max(prefix[:token.end()], default=0) >= 0x80:
                return None
            return token.end()
    return None


def boundary_in(window, origin, low):
    """First safe range start in ``window`` (bytes read from file offset ``origin``) at or after ``low``.

    A safe start is a token beginning with ``#`` whose preceding token does not begin
    with ``b``, ``B``, ``r``, ``R``, ``s`` or ``S``: only those values take a following
    identifier, and ``#`` is a legal identifier character. The token must be ``#`` and
    at most 18 digits, so parsing it can never fail, and candidates whose neighbourhood
    holds non-ASCII bytes (possible Unicode whitespace) are skipped. None if none found.
    """
    position = max(1, low - origin)
    while True:
        position = window.find(b"#", position)
        if position < 0:
            return None
        if window[position - 1] in _SPACE:
            end = position - 1
            while end >= 0 and window[end] in _SPACE:
                end -= 1
            start = end
            while start >= 0 and window[start] not in _SPACE:
                start -= 1
            following = position
            while following < len(window) and window[following] not in _SPACE:
                following += 1
            if (start >= 0 and following < len(window) and window[start + 1] not in b"bBrRsS"
                    and 1 < following - position <= 19 and window[position + 1:following].isdigit()
                    and max(window[start:following + 1]) < 0x80):
                return origin + position
        position += 1


def range_starts(path, body, size, parts, window_bytes=1 << 20, back_bytes=4096):
    """Offsets of up to ``parts`` ranges of the body; the first is ``body``."""
    starts = [body]
    with open(path, "rb") as stream:
        for k in range(1, parts):
            target = body + (size - body) * k // parts
            low = max(target, starts[-1] + 1)
            found = None
            while found is None and low < size:
                origin = max(body, low - back_bytes)
                stream.seek(origin)
                window = stream.read(low - origin + window_bytes)
                if len(window) <= low - origin:
                    break
                found = boundary_in(window, origin, low)
                if found is None:
                    low = origin + len(window) - 64  # keep a short overlap for a token cut at the edge
                    if origin + len(window) >= size:
                        break
            if found is None:
                break
            if found > starts[-1]:
                starts.append(found)
    return starts


class _Part:
    """The one intermediate file of a range: segments of (rows, codes) per signal."""

    def __init__(self, path):
        self.path = path
        self.stream = open(path, "wb")
        self.segments = {}

    def write(self, owner, rows, codes=None):
        position = self.stream.tell()
        rows.tofile(self.stream)
        if codes is not None:
            codes.tofile(self.stream)
        self.segments.setdefault(owner, []).append((position, len(rows)))

    def close(self):
        if not self.stream.closed:
            self.stream.close()


class _Sink:
    """Local runs of one signal in one range. ``append`` writes one change; ``replace``
    swaps the change already written at ``row`` for a later one at the same row."""

    __slots__ = ("owner", "part", "segment", "rows", "codes", "last_raw", "last_code", "first", "runs",
                 "spilled_last", "new_row", "new_key")

    def __init__(self, owner, part, segment):
        self.owner, self.part, self.segment = owner, part, segment
        self.rows = array("q")
        self.codes = array("I")
        self.last_raw = None
        self.last_code = -1
        self.first = None
        self.runs = 0
        self.spilled_last = -1   # last code written to disk
        self.new_row = -1        # row whose change introduced the newest table entry
        self.new_key = None

    def spill(self):
        if self.rows:
            if self.first is None:
                self.first = self.codes[0]
            self.spilled_last = self.codes[-1]
            self.runs += len(self.rows)
            if self.part is not None:
                self.part.write(self.owner, self.rows, self.codes)
            self.rows = array("q")
            self.codes = array("I")

    def summary(self):
        """(runs, local table, first code, last code) after ``spill``."""
        last = self.spilled_last
        return self.runs, self.table(), (-1 if self.first is None else self.first), last


class _ValueSink(_Sink):
    """A signal's value runs: raw texts are parsed once, equal values form one run (``_Writer``)."""

    __slots__ = ("signal", "cache")

    def __init__(self, owner, part, segment, width):
        super().__init__(owner, part, segment)
        self.signal = SignalData("", "BINARY", width, True)
        self.cache = {}

    def append(self, row, raw):
        if raw == self.last_raw:
            return
        self.last_raw = raw
        code = self.cache.get(raw)
        if code is None:
            known = len(self.signal.values)
            code = _Writer._learn(self, raw)
            self.cache[raw] = code
            if code == known:
                self.new_row, self.new_key = row, raw
        if code == self.last_code:
            return
        self.last_code = code
        if len(self.rows) >= self.segment:
            self.spill()
        self.rows.append(row)
        self.codes.append(code)

    def replace(self, row, raw):
        rows = self.rows
        if rows and rows[-1] == row:
            rows.pop()
            code = self.codes.pop()
            values = self.signal.values
            if self.new_row == row and code == len(values) - 1:
                # The replaced change was this value's only appearance so far: forget it.
                del self.signal.value_ids[values.pop()]
                del self.cache[self.new_key]
                self.new_row = -1
            self.last_code = self.codes[-1] if self.codes else self.spilled_last
        self.last_raw = None
        self.append(row, raw)

    def table(self):
        return self.signal.values


class _TextSink(_Sink):
    """A split-bus member bit: its raw texts, as ``RawTimeline`` records them."""

    __slots__ = ("texts", "ids")

    def __init__(self, owner, part, segment, width=1):
        super().__init__(owner, part, segment)
        self.texts = []
        self.ids = {}

    def append(self, row, raw):
        if raw == self.last_raw:
            return
        self.last_raw = raw
        code = self.ids.get(raw)
        if code is None:
            code = self.ids[raw] = len(self.texts)
            self.texts.append(raw)
            self.new_row = row
        self.last_code = code
        if len(self.rows) >= self.segment:
            self.spill()
        self.rows.append(row)
        self.codes.append(code)

    def replace(self, row, raw):
        rows = self.rows
        if rows and rows[-1] == row:
            rows.pop()
            code = self.codes.pop()
            if self.new_row == row and code == len(self.texts) - 1:
                del self.ids[self.texts.pop()]
                self.new_row = -1
            self.last_code = self.codes[-1] if self.codes else self.spilled_last
            self.last_raw = None if self.last_code < 0 else self.texts[self.last_code]
        self.append(row, raw)

    def table(self):
        return self.texts


def _tokens(stream, end, cancel, block_bytes=BLOCK_BYTES):
    """Whitespace tokens of the bytes up to ``end``; blocks are cut after an ASCII space."""
    tail = b""
    while True:
        if cancel():
            raise LoadCancelled()
        size = end - stream.tell()
        block = stream.read(min(block_bytes, size)) if size > 0 else b""
        if not block:
            if tail:
                yield from tail.decode("utf-8", "replace").split()
            return
        data = tail + block
        cut = len(data)
        while cut and data[cut - 1] not in _SPACE:
            cut -= 1
        if not cut:
            tail = data
            continue
        tail = data[cut:]
        yield from data[:cut].decode("utf-8", "replace").split()


class RangeParser:
    """Parse one byte range of a VCD body; see the module docstring."""

    def __init__(self, path, start, end, plan, part_path, cancel_path=None, segment=SEGMENT,
                 block_bytes=BLOCK_BYTES):
        self.path, self.start, self.end = path, start, end
        self.widths, self.by_id, self.members, self.undumped = plan
        self.part_path = part_path
        self.cancel_path = cancel_path
        self.segment = segment
        self.block_bytes = block_bytes

    def cancelled(self):
        return bool(self.cancel_path) and os.path.exists(self.cancel_path)

    def run(self):
        began, cpu = time.perf_counter(), time.process_time()
        result = self._run()
        result["seconds"] = time.perf_counter() - began
        result["cpu_seconds"] = time.process_time() - cpu
        return result

    def _run(self):
        try:
            return self._attempt(exact=False)
        except UnsafeRange:
            return {"unsafe": True}
        except ValueError:
            pass
        # A change failed: parse again in the single-process order, so the first error
        # (or none, if the failing value was replaced within its timestamp) is the same.
        try:
            return self._attempt(exact=True)
        except UnsafeRange:
            return {"unsafe": True}
        except ValueError as exc:
            return {"error": str(exc), "early": self.early}

    def _attempt(self, exact):
        part = _Part(self.part_path)
        self.early = True  # no timestamp has been committed yet
        try:
            sinks = [(_TextSink if index in self.members else _ValueSink)(index, part, self.segment, width)
                     for index, width in enumerate(self.widths)]
            with open(self.path, "rb") as stream:
                stream.seek(self.start)
                tokens = _tokens(stream, self.end, self.cancelled, self.block_bytes)
                rows, first_time, last_time = self._parse(tokens, sinks, part, exact)
            for sink in sinks:
                sink.spill()
        finally:
            part.close()
        return {"rows": rows, "first_time": first_time, "last_time": last_time,
                "columns": [sink.summary() for sink in sinks], "segments": part.segments}

    def _parse(self, tokens, sinks, part, exact):
        count = 0
        current_time = 0
        previous_time = -1
        first_time = None
        started = False
        pending = {}
        seen = [-1] * len(sinks)
        if exact:
            # Every change goes through the dict, committed in its insertion order.
            appends = [(lambda row, value, index=index: pending.__setitem__(index, value))
                       for index in range(len(sinks))]
        else:
            appends = [sink.append for sink in sinks]
        widths = self.widths
        single = {symbol: (indices[0], widths[indices[0]], appends[indices[0]])
                  for symbol, indices in self.by_id.items() if len(indices) == 1}
        shared = {symbol: indices for symbol, indices in self.by_id.items() if len(indices) > 1}
        target = single.get
        lookup = shared.get
        scalar = {"0": "0", "1": "1", "x": "x", "X": "x", "z": "z", "Z": "z"}.get
        undumped = self.undumped
        times = array("q")
        segment = self.segment
        cancelled = self.cancelled

        def commit():
            nonlocal count, previous_time, times
            if current_time <= previous_time:
                raise ValueError("В VCD временные отметки идут не по возрастанию.")
            self.early = False
            if count or undumped is None:
                for index, value in pending.items():
                    sinks[index].replace(count, value)
            else:
                # An undumped signal is unknown in every bit, like a later explicit bx.
                for index, unknown in undumped:
                    if index in pending:
                        sinks[index].replace(0, pending[index])
                    elif seen[index] != 0:
                        sinks[index].append(0, unknown)
            pending.clear()
            if len(times) >= segment:
                part.write(TIMES, times)
                times = array("q")
            times.append(current_time)
            count += 1
            previous_time = current_time
            if count % 4096 == 0 and cancelled():
                raise LoadCancelled()

        for token in tokens:
            first = token[0]
            if first == "b" or first == "B":
                symbol = next(tokens, None)
                if symbol is None:
                    raise ValueError(f"Незавершённое изменение VCD: {token[:80]}")
                value = token[1:]
                if not value.isdecimal():
                    value = value.lower()
            elif (value := scalar(first)) is not None:
                symbol = token[1:]
            elif first == "#":
                timestamp = int(token[1:])
                if timestamp < 0 or timestamp > 2**63 - 1:
                    raise ValueError("Временная отметка VCD вне диапазона int64.")
                if first_time is None:
                    first_time = timestamp
                if started and timestamp != current_time:
                    commit()
                current_time = timestamp
                started = True
                continue
            elif first == "$":
                if token not in ALLOWED:
                    raise UnsafeRange()
                continue
            elif first in "rRsS":
                if next(tokens, None) is None:
                    raise ValueError(f"Незавершённое изменение VCD: {token[:80]}")
                continue
            else:
                raise ValueError(f"Некорректное изменение VCD: {token[:80]}")
            if not symbol or not value:
                raise ValueError(f"Некорректное изменение VCD: {token[:80]}")
            started = True
            plan = target(symbol)
            if plan is not None:
                index, width, append = plan
                if width > 1 and len(value) < width and value[0] in "xz":
                    value = value.rjust(width, value[0])
                if seen[index] == count:
                    pending[index] = value  # a second change in this timestamp: the last one wins
                else:
                    seen[index] = count
                    append(count, value)
                continue
            for index in lookup(symbol, ()):
                padded = value
                if len(value) < widths[index] and value[0] in "xz":
                    padded = value.rjust(widths[index], value[0])
                if seen[index] == count:
                    pending[index] = padded
                else:
                    seen[index] = count
                    appends[index](count, padded)
        if started:
            commit()
        if times:
            part.write(TIMES, times)
        return count, first_time, (current_time if count else None)


def parse_range(path, start, end, plan, part_path, cancel_path=None, segment=SEGMENT, block_bytes=BLOCK_BYTES):
    """Worker entry point: parse ``[start, end)`` of the VCD at ``path`` into ``part_path``.

    ``plan`` is (signal widths, {identifier: signal indices}, member indices, undumped
    list or None). ``undumped`` is given to the first range only: its first row gives
    every undumped signal an all-X value. Returns a dict with ``rows``, ``first_time``,
    ``last_time``, ``columns`` and ``segments``; or ``unsafe``; or ``error`` with the
    message and ``early`` (the error came before the range's first commit).
    """
    return RangeParser(path, start, end, plan, part_path, cancel_path, segment, block_bytes).run()


def place_signals(parts, jobs, cancel_path=None):
    """Copy segments of the part files into final index files, remapping codes and rows.

    ``jobs`` is a list of (starts path, codes path or None, entries); each entry is
    (part number, [(offset, count)], lut or None, row offset, drop). ``drop`` skips the
    first run of that range (it repeats the previous range's last value).
    """
    sources = {}
    try:
        for start_path, code_path, entries in jobs:
            with contextlib.ExitStack() as stack:
                starts_out = stack.enter_context(open(start_path, "wb"))
                codes_out = stack.enter_context(open(code_path, "wb")) if code_path else None
                for k, segments, lut, row_offset, drop in entries:
                    source = sources.get(k)
                    if source is None:
                        source = sources[k] = open(parts[k], "rb")
                    for position, count in segments:
                        if cancel_path and os.path.exists(cancel_path):
                            raise LoadCancelled()
                        source.seek(position)
                        rows = np.fromfile(source, dtype=np.int64, count=count)
                        codes = np.fromfile(source, dtype=np.uint32, count=count) if code_path else None
                        if drop:
                            rows = rows[1:]
                            codes = codes[1:] if codes is not None else None
                            drop = 0
                        if row_offset:
                            rows += row_offset
                        rows.tofile(starts_out)
                        if codes is not None:
                            lut[codes].tofile(codes_out)
    finally:
        for source in sources.values():
            source.close()
    return len(jobs)
