# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Original scalar implementations, kept as oracles for the vectorized ones."""
from array import array
import csv
import math
import re
import tempfile
from pathlib import Path

import numpy as np

from ila_viewer.data import META, RADICES, Capture, LoadCancelled, SignalData, _Writer


def reference_load_csv(path, progress=lambda p, n: None,
                       cancel=lambda: False):
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


def reference_highlight(capture, condition, marker_a, marker_b, sampling):
    """Per-point highlight build with ``Condition.matches``; returns merged (start, end) spans."""
    lower, upper = sorted((marker_a, marker_b))
    spans, pending = [], None
    for point in sampling.points(lower, upper, capture.count):
        if condition is not None and not condition.matches(point.at, point.previous_at):
            continue
        start = max(point.start, lower)
        end = min(point.end, capture.count, upper)
        if end <= start:
            continue
        if pending and start <= pending[1]:
            pending = pending[0], max(pending[1], end)
        else:
            if pending:
                spans.append(pending)
            pending = start, end
    if pending:
        spans.append(pending)
    return spans


def reference_advanced(capture, pattern, marker_a, marker_b, sampling):
    """The original FSM of active stage paths."""
    lower, upper = sorted((marker_a, marker_b))
    start_boundary = lower
    end_boundary = min(capture.count, upper)
    active, spans, pending = [], [], None
    for point in sampling.points(start_boundary, end_boundary, capture.count):
        at, previous = point.at, point.previous_at
        next_active = []
        for stage_index, start in active:
            if not pattern.stages[stage_index].matches(at, previous):
                continue
            if stage_index + 1 < len(pattern.stages):
                next_active.append((stage_index + 1, start))
                continue
            end = min(point.end, end_boundary)
            if pending and start <= pending[1]:
                pending = pending[0], max(pending[1], end)
            else:
                if pending:
                    spans.append(pending)
                pending = start, end
        if pattern.stages[0].matches(at, previous):
            next_active.append((1, max(point.start, start_boundary)))
        active = next_active
    if pending:
        spans.append(pending)
    return spans


def reference_samples_at_offsets(capture, offsets, origin=None):
    """Capture.samples_at_offsets before vectorization: one Python step per offset."""
    origin = int(capture.times[0]) if origin is None else int(origin)
    ticks = np.asarray(offsets, dtype=np.float64)
    queries = np.fromiter((max(0, min(2**63 - 1, origin + math.floor(t))) for t in ticks.flat),
                          dtype=np.int64, count=ticks.size).reshape(ticks.shape)
    indices = np.clip(np.searchsorted(capture.times, queries, side="right") - 1, 0, capture.count - 1)
    low = (capture.times[indices] - origin).astype(np.float64)
    following = np.minimum(indices + 1, capture.count - 1)
    widths = np.maximum(1, capture.times[following] - capture.times[indices])
    return indices + (ticks - low) / widths


def reference_sample_x(capture, left, span, plot_left, plot_width, sample):
    """WaveformView.sample_x before the viewport transform: origin and edges per call."""
    if capture is not None and capture.times is not None:
        origin = int(capture.times[max(0, min(capture.count - 1, int(left)))])
        low = capture.time_offset_at(left, origin)
        high = capture.time_offset_at(left + span, origin)
        return plot_left + (capture.time_offset_at(sample, origin) - low) / max(1, high - low) * plot_width
    return plot_left + (sample - left) / span * plot_width


def reference_x_sample(capture, left, span, plot_left, plot_width, x):
    """WaveformView.x_sample before the viewport transform."""
    if capture is not None and capture.times is not None:
        origin = int(capture.times[max(0, min(capture.count - 1, int(left)))])
        low = capture.time_offset_at(left, origin)
        high = capture.time_offset_at(left + span, origin)
        offset = low + (x - plot_left) / plot_width * (high - low)
        return float(reference_samples_at_offsets(capture, [offset], origin)[0])
    return left + (x - plot_left) / plot_width * span


def reference_pixel_runs(indices, codes, plot_width):
    """The per-pixel loop of WaveformRenderer.draw_signal before task 4, as (start, end, busy, code).

    ``indices`` are the run indices at the ``plot_width + 1`` pixel edges and ``codes``
    the row's full code array, read one element at a time as the old loop did.
    """
    runs = []
    run_start = 0
    while run_start < plot_width:
        busy = indices[run_start + 1] != indices[run_start]
        code = codes[indices[run_start]]
        run_end = run_start + 1
        while run_end < plot_width:
            next_busy = indices[run_end + 1] != indices[run_end]
            if next_busy != busy or (not busy and codes[indices[run_end]] != code):
                break
            run_end += 1
        runs.append((run_start, run_end, bool(busy), int(code)))
        run_start = run_end
    return runs


def reference_draw_segment(painter, view, sig, code, x1, x2, transition, band):
    """WaveformRenderer.draw_segment before task 4: a color, pen and draw call per run."""
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QPen, QPolygonF
    v = view
    if x2 <= x1:
        return
    value = sig.values[int(code)]
    unknown = isinstance(value, str)
    color = QColor(v.themes.current.unknown) if unknown else v.signal_color(sig)
    painter.setPen(QPen(color, 1.2))
    hi, lo = band
    if not sig.is_bus and not unknown:
        level = hi if value else lo
        if value and v.preferences.fill_high:
            painter.fillRect(QRectF(x1, hi, x2 - x1, lo - hi + 1),
                             QColor(color.red(), color.green(), color.blue(), 65))
        painter.drawLine(QPointF(x1, level), QPointF(x2, level))
        if transition:
            painter.drawLine(QPointF(x1, hi), QPointF(x1, lo))
        return
    notch = min(4, (x2 - x1) / 2)
    polygon = QPolygonF([QPointF(x1, (hi + lo) / 2), QPointF(x1 + notch, hi),
                         QPointF(x2 - notch, hi), QPointF(x2, (hi + lo) / 2),
                         QPointF(x2 - notch, lo), QPointF(x1 + notch, lo)])
    filled = v.preferences.fill_bus
    painter.setBrush(QColor(color.red(), color.green(), color.blue(), 65) if filled else Qt.BrushStyle.NoBrush)
    painter.drawPolygon(polygon)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    if x2 - x1 > 20:
        label = sig.format_value(value, v.display_radix(sig))
        label = painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, int(x2 - x1 - 10))
        painter.drawText(QRectF(x1 + 5, hi - 12, x2 - x1 - 10, lo - hi + 24), Qt.AlignmentFlag.AlignCenter, label)


# --- VCD loading before WLF performance task 5 -------------------------------------------


class ReferenceSplitBusAssembler:
    """SplitBusAssembler before task 5: rebuilds each split bus inside the parse loop."""

    BIT_NAME = re.compile(r"^(.*)\[(\d+)\]$")

    def __init__(self, signals):
        original = list(signals)
        groups = {}
        native = {match[1] for signal in original if signal.width > 1
                  if (match := re.match(r"^(.*)\[\d+:\d+\]$", signal.name))}
        for index, signal in enumerate(original):
            match = self.BIT_NAME.fullmatch(signal.name)
            if signal.width == 1 and match and match[1] not in native:
                groups.setdefault(match[1], []).append((int(match[2]), index))
        self.members = {}
        self.children = {}
        self.states = {}
        self.affected = {}
        for base, entries in groups.items():
            numbers = [number for number, _ in entries]
            if (len(entries) < 2 or len(set(numbers)) != len(numbers)
                    or max(numbers) - min(numbers) + 1 != len(numbers)):
                continue
            if not all(abs(a - b) == 1 for a, b in zip(numbers, numbers[1:])):
                # Some writers declare bits in arbitrary order; present them MSB first.
                entries = sorted(entries, reverse=True)
                numbers = [number for number, _ in entries]
            name = f"{base}[{numbers[0]}:{numbers[-1]}]"
            parent_index = len(signals)
            signals.append(SignalData(name, "BINARY", len(entries), True))
            self.members[parent_index] = [index for _, index in entries]
            self.children[name] = [original[index] for _, index in entries]
            self.states[parent_index] = ["x"] * len(entries)
            for offset, (_, index) in enumerate(entries):
                self.affected[index] = (parent_index, offset)

    def append(self, writers, changes, row):
        if row == 0:
            for index, writer in enumerate(writers):
                if index not in self.members:
                    # An undumped vector is unknown in every bit, like a later explicit bx.
                    writer.append(0, changes.get(index, "x" * writer.signal.width))
            for parent, members in self.members.items():
                state = self.states[parent]
                for offset, index in enumerate(members):
                    state[offset] = changes.get(index, "x").lower()
                writers[parent].append(0, "".join(state))
            return
        affected = set()
        for index, value in changes.items():
            writers[index].append(row, value)
            member = self.affected.get(index)
            if member:
                parent, offset = member
                self.states[parent][offset] = value.lower()
                affected.add(parent)
        for parent in affected:
            writers[parent].append(row, "".join(self.states[parent]))



def _reference_vcd_reader():
    from ila_viewer.vcd import VcdCaptureReader

    class ReferenceVcdReader(VcdCaptureReader):
        """VcdCaptureReader.load before task 5: split buses assembled per timestamp."""

        def load(self, location, progress=lambda percent, rows: None,
                 cancel=lambda: False, source_path=None):
            path = Path(location).resolve()
            storage = tempfile.TemporaryDirectory(prefix="ila_vcd_")
            writers = []
            mapped = []
            try:
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    tokens = self._tokens(stream, cancel)
                    signals, by_id, timescale = self._header(tokens)
                    if not signals:
                        raise ValueError("В VCD нет поддерживаемых битовых сигналов.")
                    buses = ReferenceSplitBusAssembler(signals)
                    folder = Path(storage.name)
                    writers = [_Writer(folder, index, sig) for index, sig in enumerate(signals)]
                    time_path = folder / "timestamps.i64"
                    changes = {}
                    count = 0
                    current_time = 0
                    previous_time = -1
                    size = max(1, path.stat().st_size)
                    with time_path.open("wb") as time_file:
                        time_buffer = array("q")

                        def commit():
                            nonlocal count, previous_time
                            if current_time <= previous_time:
                                raise ValueError("В VCD временные отметки идут не по возрастанию.")
                            buses.append(writers, changes, count)
                            changes.clear()
                            time_buffer.append(current_time)
                            if len(time_buffer) >= 8192:
                                time_buffer.tofile(time_file)
                                del time_buffer[:]
                            count += 1
                            previous_time = current_time
                            if count % 4096 == 0:
                                if cancel():
                                    raise LoadCancelled()
                                progress(min(99, int(stream.buffer.tell() / size * 100)), count)

                        started = False
                        widths = [signal.width for signal in signals]
                        lookup = by_id.get
                        for token in tokens:
                            first = token[0]
                            if first in "01xXzZ":
                                symbol, value = token[1:], first.lower()
                            elif first == "#":
                                timestamp = int(token[1:])
                                if timestamp < 0 or timestamp > 2**63 - 1:
                                    raise ValueError("Временная отметка VCD вне диапазона int64.")
                                if started and timestamp != current_time:
                                    commit()
                                current_time = timestamp
                                started = True
                                continue
                            elif first == "$":
                                if token not in {"$dumpvars", "$dumpall", "$dumpon", "$dumpoff", "$end"}:
                                    for item in tokens:
                                        if item == "$end":
                                            break
                                continue
                            elif first in "bBrRsS":
                                symbol = next(tokens, None)
                                if symbol is None:
                                    raise ValueError(f"Незавершённое изменение VCD: {token[:80]}")
                                if first not in "bB":
                                    continue  # real/string declarations are deliberately unsupported
                                value = token[1:].lower()
                            else:
                                raise ValueError(f"Некорректное изменение VCD: {token[:80]}")
                            if not symbol or not value:
                                raise ValueError(f"Некорректное изменение VCD: {token[:80]}")
                            started = True
                            for index in lookup(symbol, ()):
                                if len(value) < widths[index] and value[0] in "xz":
                                    changes[index] = value.rjust(widths[index], value[0])
                                else:
                                    changes[index] = value
                        if started:
                            commit()
                        time_buffer.tofile(time_file)
                    if cancel():
                        raise LoadCancelled()
                    if not count:
                        raise ValueError("VCD не содержит временных отметок или изменений сигналов.")
                    for writer in writers:
                        writer.close()
                        starts = np.memmap(writer.start_path, dtype=np.int64, mode="r")
                        codes = np.memmap(writer.code_path, dtype=np.uint32, mode="r")
                        mapped.extend((starts, codes))
                        writer.signal.starts, writer.signal.codes = starts, codes
                    times = np.memmap(time_path, dtype=np.int64, mode="r")
                    mapped.append(times)
                    progress(100, count)
                    return Capture(Path(source_path or path).resolve(), signals, count, {}, storage,
                                   times=times, timescale=timescale, split_buses=buses.children)
            except BaseException:
                for writer in writers:
                    writer.close()
                for data in mapped:
                    data._mmap.close()
                storage.cleanup()
                raise

    return ReferenceVcdReader()


def reference_load_vcd(location, **kwargs):
    return _reference_vcd_reader().load(location, **kwargs)
