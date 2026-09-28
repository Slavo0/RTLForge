# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Snapshot-based, cancellable text export; clipboard access stays on the GUI thread."""
from dataclasses import dataclass, replace
from io import StringIO

import numpy as np
from PySide6.QtWidgets import QApplication

from .bits import BitCodec
from .data import LoadCancelled
from .formats import FORMATS, RealSettings


SEPARATORS = {"space": " ", "comma": ",", "text": "", "tab": "\t", "newline": "\n"}


@dataclass(frozen=True)
class CopyChannel:
    source: object
    width: int
    offsets: tuple | None
    radix: str
    reverse: bool
    real: RealSettings

    def value(self, code):
        value = self.source.values[int(code)]
        if self.offsets is not None:
            if isinstance(value, int):
                packed = 0
                for offset in self.offsets:
                    packed = (packed << 1) | ((value >> offset) & 1)
                value = packed
            else:
                bits = "".join(str(BitCodec.bit(value, self.source.width, offset, self.source.radix)) for offset in self.offsets)
                value = int(bits, 2) if all(b in "01" for b in bits) else bits
        if self.reverse:
            value = BitCodec.reverse(value, self.width, "BINARY" if self.offsets is not None else self.source.radix)
        return value


@dataclass(frozen=True)
class CopyRequest:
    channels: tuple
    start: int
    end: int
    separator: str
    samples: str
    period: int = 1
    condition: object = None
    phase: int = 0
    exclude_sample: int | None = None


@dataclass(frozen=True)
class CopyResult:
    text: str
    count: int


class CopyValuesService:
    MAX_CHARACTERS = 32_000_000

    def snapshot(self, view, radix="DEFAULT", condition=None, period=1, phase=0):
        if not view.capture or view.cursor_b is None:
            raise ValueError("Установите маркеры A и B для копирования диапазона")
        selected = []
        def visit(node):
            if node.signal:
                selected.append(node)
            elif node.kind == "group":
                for child in node.children:
                    visit(child)
        for node in view.selected_nodes:
            visit(node)
        unique = list(dict.fromkeys(selected))
        full_buses = {n for n in unique if n.kind == "bus"}
        channels = []
        emitted = set()
        for node in unique:
            parent = node.parent if node.kind == "bit" else None
            if parent in full_buses:
                continue
            key = parent or node
            if key in emitted:
                continue
            emitted.add(key)
            trace = key.signal
            offsets = None
            if parent:
                labels = BitCodec.labels(trace.source)
                lookup = {label: trace.width - 1 - i for i, label in enumerate(labels)}
                offsets = tuple(sorted({lookup[int(n.label[1:-1])] for n in unique
                                        if n.kind == "bit" and n.parent is parent}, reverse=True))
            width = len(offsets) if offsets is not None else trace.width
            effective = view.display_radix(trace) if radix == "DEFAULT" else radix
            effective = FORMATS.effective(effective, trace.source.radix)
            real = replace(trace.options.real) if trace.options.real else RealSettings()
            if effective == "REAL" and real.mode == "float" and width not in (32, 64):
                raise ValueError("Для копирования Real IEEE 754 выберите 32/64 бита или другой Radix")
            channels.append(CopyChannel(trace.source, width, offsets, effective, trace.options.reverse, real))
        if not channels:
            raise ValueError("Выберите сигналы или биты для копирования")
        start, end = sorted((view.cursor_a, view.cursor_b))
        return CopyRequest(tuple(channels), start, end, SEPARATORS[view.preferences.copy_separator],
                           view.preferences.copy_samples, max(1, int(period)), condition,
                           int(phase), int(view.cursor_b))

    def build(self, request, progress=lambda p, n: None, cancel=lambda: False):
        output = StringIO()
        count = 0
        period = max(1, request.period)
        first_sample = request.start + ((request.phase - request.start) % period)
        total_samples = max(0, (request.end - first_sample) // period + 1) if first_sample <= request.end else 0
        if request.exclude_sample is not None and request.start <= request.exclude_sample <= request.end:
            if request.exclude_sample >= first_sample and (request.exclude_sample - first_sample) % period == 0:
                total_samples = max(0, total_samples - 1)
        total = len(request.channels) * total_samples
        processed = 0
        for channel_index, channel in enumerate(request.channels):
            first = True
            last_value = object()
            cache = {}
            source = channel.source
            if (request.condition is None and period == 1 and request.samples == "changes"
                    and request.exclude_sample != request.start):
                begin = source.run_index(request.start)
                transition_end = int(np.searchsorted(source.starts, request.end, side="right"))
                chunks = ((int(source.starts[i]), int(source.codes[i])) for i in range(begin, transition_end)
                          if int(source.starts[i]) != request.exclude_sample)
            elif request.condition is None and period == 1:
                def every_sample():
                    for a in range(first_sample, request.end + 1, 65536):
                        if cancel():
                            raise LoadCancelled()
                        b = min(request.end + 1, a + 65536)
                        samples = np.arange(a, b)
                        if request.exclude_sample is not None:
                            samples = samples[samples != request.exclude_sample]
                        codes = source.codes[np.maximum(0, np.searchsorted(source.starts, samples, side="right") - 1)]
                        yield from zip(map(int, samples), map(int, codes))
                chunks = every_sample()
            else:
                def sampled():
                    previous_candidate = None
                    previous_value = None
                    for offset, sample in enumerate(range(first_sample, request.end + 1, period)):
                        if offset % 4096 == 0 and cancel():
                            raise LoadCancelled()
                        if sample == request.exclude_sample:
                            continue
                        if request.condition is not None and not request.condition.matches(sample):
                            continue
                        code = int(source.codes[source.run_index(sample)])
                        if request.samples == "changes" and previous_candidate is not None and code == previous_value:
                            continue
                        previous_candidate, previous_value = sample, code
                        yield sample, code
                chunks = sampled()
            for index, (sample, code) in enumerate(chunks):
                if cancel():
                    raise LoadCancelled()
                cached = cache.get(code)
                if cached is None:
                    value = channel.value(code)
                    label = FORMATS.format(value, channel.width, channel.radix, channel.real)
                    cached = value, label
                    if len(cache) < 65536:
                        cache[code] = cached
                value, label = cached
                if request.samples == "changes" and not first and value == last_value:
                    continue
                extra = len(label) + (0 if first else len(request.separator))
                channel_separator = 1 if first and count else 0
                if output.tell() + extra + channel_separator > self.MAX_CHARACTERS:
                    raise ValueError("Результат превышает 32 млн символов. Уменьшите диапазон или выберите «Только изменения».")
                if first and count:
                    output.write("\n")
                if not first:
                    output.write(request.separator)
                output.write(label)
                first, last_value = False, value
                count += 1
                if index % 4096 == 0:
                    progress(min(99, int((processed + index) / max(1, total) * 100)), count)
            processed += total_samples
        if cancel():
            raise LoadCancelled()
        progress(100, count)
        return CopyResult(output.getvalue(), count)


class SystemClipboard:
    def write(self, text):
        QApplication.clipboard().setText(text)
