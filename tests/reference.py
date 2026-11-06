# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Original scalar implementations, kept as oracles for the vectorized ones."""
import csv
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
    for point in sampling.points(lower, upper + 1, capture.count):
        if point.sample == marker_b or (condition is not None and not condition.matches(point.at, point.previous_at)):
            continue
        start = max(point.start, lower + (marker_b == lower))
        end = min(point.end, capture.count, upper + (marker_b != upper))
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
    start_boundary = lower + (marker_b == lower)
    end_boundary = min(capture.count, upper + (marker_b != upper))
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
