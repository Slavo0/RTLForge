# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Convert a WLF as time slices in parallel and join them into one VCD; Qt-free.

``wlf2vcd`` has no time or signal options (``-help``: ``-o``, ``-quiet``), but
``wlfman filter -begin <t> -end <t>`` writes a WLF slice. Measured with Questa 2024.1
on vsim.wlf (docs/WLF_PERF_TASKS.md, task 7):

- a slice keeps the events of ``[begin, end]``, ``end`` included;
- its VCD has the same header as the whole file, then a row at ``begin`` that lists
  every signal's value (followed by the events at ``begin``, if any); a slice that ends
  between events has no row at its end;
- slice 0 whole plus every later slice from its second timestamp loads to exactly the
  capture of one ``wlf2vcd`` run, with edges at event times and between them;
- the first row states every signal's value at the last event at or before ``begin``.
  When ``begin`` is an event time it lists the state after that event (every signal,
  then the signals that change at ``begin`` once more, with the same values); between
  events it lists the state of the last event before ``begin``, one line per signal.

The join relies only on these observations, and each one that the output can show is
checked again on every conversion: equal headers, and a later slice's first timestamp
equal to its ``begin``. When a check fails, a tool is missing or a slice fails,
``convert`` returns None and the caller converts in one ``wlf2vcd`` run instead.

The join drops each later slice's first row. ``convert`` keeps those rows in a
``SeamCheck``; once the joined VCD is loaded, ``SeamCheck.mismatches`` compares every
signal of every row (the last change of a signal in the row wins) with the capture's
value at the last row at or before the seam's ``begin``. Any mismatch means the join
cannot be trusted, and the caller loads the file again through one ``wlf2vcd`` run.
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil

from .csv_chunks import LoadCancelled
from .data import parse_value, search_sorted
from .vcd import VcdCaptureReader
from .vcd_chunks import boundary_in, header_end

_TIME = re.compile(r"(Start|End) Time:\s*(\d+)")
_DATE = re.compile(rb"\$date\s.*?\$end", re.DOTALL)
_SEARCH = 4 << 20  # bytes searched for a slice's second timestamp (its first row lists ~1 line per signal)


def find_wlfman(wlf2vcd):
    name = "wlfman.exe" if os.name == "nt" else "wlfman"
    candidate = Path(wlf2vcd).with_name(name)
    return str(candidate) if candidate.is_file() else shutil.which("wlfman")


@dataclass(frozen=True)
class Seam:
    """Where a later slice joins: its ``begin`` and its dropped first row (changes only)."""
    begin: int
    row: bytes


@dataclass(frozen=True)
class SeamMismatch:
    seam: int          # 1-based; 0 when the signal lists differ
    seams: int
    begin: int
    signal: str
    in_slice: object   # None: the signal is missing from the row
    in_capture: object

    def describe(self):
        if not self.seam:
            return f"список сигналов среза ({self.in_slice}) не совпадает с захватом ({self.in_capture})"
        stated = "нет в строке среза" if self.in_slice is None else f"в срезе {self.in_slice}"
        return (f"стык {self.seam} из {self.seams} (время {self.begin:,}), сигнал {self.signal}: "
                f"{stated}, в захвате {self.in_capture}")


class SeamCheck:
    """The dropped first rows of a sliced conversion, checked against the loaded capture."""

    def __init__(self, header, seams):
        self.header = header  # the slices' common header bytes, without $date
        self.seams = list(seams)

    @staticmethod
    def _canonical(value, width):
        # parse_value gives an int for 0/1 text and upper-case text with X/Z; VCD extends a
        # shorter vector on the left with 0, or with its leading X/Z.
        if isinstance(value, str) and value and len(value) < width:
            lead = value[0]
            return value.rjust(width, lead if lead in "XZ?" else "0")
        return value

    @staticmethod
    def _row(row):
        """{identifier: raw text} of one row; the last change of an identifier wins."""
        tokens = iter(row.decode("ascii", "replace").split())
        values = {}
        for token in tokens:
            first = token[0]
            if first in "bB":
                symbol = next(tokens, None)
                if symbol is None:
                    raise ValueError(token)
                values[symbol] = token[1:].lower()
            elif first in "01xXzZ":
                values[token[1:]] = first.lower()
            elif first in "rRsS":
                next(tokens, None)  # real/string variables are not loaded
            elif first != "$":  # $dumpvars / $end directives carry no value
                raise ValueError(token)
        return values

    def mismatches(self, capture):
        """Every disagreement between the dropped rows and ``capture``; empty when the join holds."""
        signals, by_id, _ = VcdCaptureReader._header(iter(self.header.decode("utf-8", "replace").split()))
        loaded = capture.signals
        if len(loaded) < len(signals) or any(a.name != b.name for a, b in zip(signals, loaded)):
            return [SeamMismatch(0, len(self.seams), 0, "", len(signals), len(loaded))]
        found = []
        for number, seam in enumerate(self.seams, 1):
            row = search_sorted(capture.times, seam.begin, "right") - 1
            try:
                values = self._row(seam.row)
            except ValueError:
                values = {}  # an unreadable row states nothing: every signal mismatches
            for symbol, indices in by_id.items():
                text = values.get(symbol)
                for index in indices:
                    signal = loaded[index]
                    actual = self._canonical(signal.value_at(row), signal.width) if row >= 0 else None
                    stated = None
                    if text is not None:
                        try:
                            stated = self._canonical(parse_value(text, signal.radix), signal.width)
                        except ValueError:
                            stated = text
                    if stated is None or stated != actual:
                        found.append(SeamMismatch(number, len(self.seams), seam.begin, signal.name, stated, actual))
        return found


class SlicedWlfConversion:
    """``slices`` parallel ``wlfman filter`` + ``wlf2vcd`` runs, joined into ``target``."""

    def __init__(self, wlf2vcd, wlfman, runner, slices):
        self.wlf2vcd, self.wlfman, self.runner, self.slices = str(wlf2vcd), str(wlfman), runner, int(slices)

    def edges(self, source, cancel):
        """Slice edges from ``wlfman info``, or None when the time range cannot be read."""
        code, detail = self.runner([self.wlfman, "info", str(source)], cancel)
        times = dict((kind, int(value)) for kind, value in _TIME.findall(detail or ""))
        if code or set(times) != {"Start", "End"} or times["End"] <= times["Start"]:
            return None
        start, end = times["Start"], times["End"]
        count = min(self.slices, end - start)
        if count < 2:
            return None
        return [start + (end - start) * k // count for k in range(count)] + [end]

    def convert(self, source, target, cancel=lambda: False):
        """A ``SeamCheck`` when ``target`` holds the joined VCD; None to convert in one run instead."""
        source, target = Path(source), Path(target)
        edges = self.edges(source, cancel)
        if edges is None:
            return None
        parts = [target.with_name(f"{target.stem}.slice{k}.vcd") for k in range(len(edges) - 1)]
        joined = False
        try:
            if not self._run(source, edges, parts, cancel):
                return None
            found = self._offsets(edges, parts)
            if found is None:
                return None
            starts, check = found
            self._join(parts, starts, target, cancel)
            joined = True
            return check
        finally:
            for part in parts:
                part.unlink(missing_ok=True)
                part.with_suffix(".wlf").unlink(missing_ok=True)
            if not joined:
                target.unlink(missing_ok=True)  # never leave a partial join behind

    def _run(self, source, edges, parts, cancel):
        def one(k):
            wlf = parts[k].with_suffix(".wlf")
            code, _ = self.runner([self.wlfman, "filter", "-begin", str(edges[k]), "-end", str(edges[k + 1]),
                                   "-o", str(wlf), str(source)], cancel)
            if not code:
                code, _ = self.runner([self.wlf2vcd, str(wlf), "-o", str(parts[k])], cancel)
            wlf.unlink(missing_ok=True)
            return not code and parts[k].is_file()

        with concurrent.futures.ThreadPoolExecutor(len(parts)) as executor:
            futures = [executor.submit(one, k) for k in range(len(parts))]
        errors = [future.exception() for future in futures if future.exception() is not None]
        if errors:
            # Cancellation and a full disk stop every slice; other errors fall back.
            for error in errors:
                if isinstance(error, (LoadCancelled, OSError)):
                    raise error
            return False
        return all(future.result() for future in futures)

    @staticmethod
    def _offsets(edges, parts):
        """(offsets, SeamCheck): the byte offset where each slice joins (0, then each later
        slice's second timestamp), and the first rows that the join drops.

        A slice without a second timestamp has no event after its ``begin`` and joins
        empty. None when a header differs or a slice does not start at its ``begin``.
        Headers are compared without their ``$date`` block: wlf2vcd writes the time of the
        conversion there, so slices finishing in different seconds differ only in it (the
        reader ignores ``$date``).
        """
        header = None
        offsets, seams = [], []
        for k, part in enumerate(parts):
            body = header_end(part)
            if body is None:
                return None
            size = part.stat().st_size
            with open(part, "rb") as stream:
                head = _DATE.sub(b"", stream.read(body), count=1)
                window = stream.read(_SEARCH)
            if header is None:
                header = head
                offsets.append(0)
                continue
            if head != header:
                return None
            first = re.match(rb"\s*(#\d+)(?=\s)", window)
            if first is None or first.group(1) != b"#%d" % edges[k]:
                return None
            second = boundary_in(window, body, body + first.end())
            if second is None:
                if body + len(window) < size:
                    return None  # the first row is longer than expected; do not guess
                second = size
            offsets.append(second)
            seams.append(Seam(edges[k], window[first.end():second - body]))
        return offsets, SeamCheck(header, seams)

    @staticmethod
    def _join(parts, offsets, target, cancel, block=8 << 20):
        os.replace(parts[0], target)
        with open(target, "ab") as out:
            for part, offset in zip(parts[1:], offsets[1:]):
                with open(part, "rb") as stream:
                    stream.seek(offset)
                    while True:
                        if cancel():
                            raise LoadCancelled()
                        data = stream.read(block)
                        if not data:
                            break
                        out.write(data)
                part.unlink()
