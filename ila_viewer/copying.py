# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Snapshot-based, cancellable text export; clipboard access stays on the GUI thread."""
from dataclasses import dataclass, replace
from io import StringIO
import re

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
    sort: str = "original"


@dataclass(frozen=True)
class CopyResult:
    text: str
    count: int


class CopyValuesService:
    MAX_CHARACTERS = 32_000_000

    @staticmethod
    def parse_bit_ranges(expression, labels):
        """Map inclusive physical bit labels to descending-significance offsets."""
        available = set(labels)
        if not expression.strip():
            return None
        selected = set()
        for token in re.split(r"[\s,]+", expression.strip()):
            match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
            if not match:
                raise ValueError(f"Неверный диапазон битов: {token}")
            first = int(match[1])
            last = int(match[2]) if match[2] is not None else first
            selected.update(range(min(first, last), max(first, last) + 1))
        if not selected <= available:
            raise ValueError("Выбранный диапазон содержит биты вне шины")
        positions = {label: len(labels) - index - 1 for index, label in enumerate(labels)}
        return tuple(sorted((positions[label] for label in selected), reverse=True))

    def selected_targets(self, view):
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
        targets = []
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
            targets.append((key, offsets))
        return targets

    def snapshot(self, view, radix="DEFAULT", condition=None, period=1, phase=0,
                 channel_options=None, separator=None, samples="all", sort="original", targets=None):
        if not view.capture:
            raise ValueError("Откройте CSV для копирования")
        channels = []
        for key, selected_offsets in (targets if targets is not None else self.selected_targets(view)):
            trace = key.signal
            options = (channel_options or {}).get(key.id, {})
            offsets = selected_offsets
            bit_range = options.get("bits", "").strip()
            if bit_range:
                offsets = self.parse_bit_ranges(bit_range, BitCodec.labels(trace.source))
            width = len(offsets) if offsets is not None else trace.width
            chosen_radix = options.get("radix", radix)
            effective = view.display_radix(trace) if chosen_radix == "DEFAULT" else chosen_radix
            effective = FORMATS.effective(effective, trace.source.radix)
            real = replace(trace.options.real) if trace.options.real else RealSettings()
            if effective == "REAL" and real.mode == "float" and width not in (32, 64):
                raise ValueError("Для копирования Real IEEE 754 выберите 32/64 бита или другой Radix")
            channels.append(CopyChannel(trace.source, width, offsets, effective, trace.options.reverse, real))
        if not channels:
            raise ValueError("Выберите сигналы или биты для копирования")
        if view.cursor_b is None:
            start, end, excluded = 0, view.capture.count - 1, None
        else:
            start, end = sorted((view.cursor_a, view.cursor_b))
            excluded = int(view.cursor_b)
        separator_key = separator or view.preferences.copy_separator
        return CopyRequest(tuple(channels), start, end, SEPARATORS[separator_key],
                           samples, max(1, int(period)), condition,
                           int(phase), excluded, sort)

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
            unique_values = {} if request.samples == "unique" else None
            unique_characters = 0
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
                        if request.condition is not None and not request.condition.matches(
                                sample, sample - period if sample >= period else None):
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
                if unique_values is not None:
                    key = str(value)
                    if key not in unique_values:
                        unique_values[key] = value, label
                        unique_characters += len(label) + len(request.separator)
                        if output.tell() + unique_characters > self.MAX_CHARACTERS:
                            raise ValueError("Результат превышает 32 млн символов. Уменьшите диапазон.")
                    if index % 4096 == 0:
                        progress(min(99, int((processed + index) / max(1, total) * 100)), len(unique_values))
                    continue
                extra = len(label) + (0 if first else len(request.separator))
                channel_separator = 1 if first and count else 0
                if output.tell() + extra + channel_separator > self.MAX_CHARACTERS:
                    raise ValueError("Результат превышает 32 млн символов. Уменьшите диапазон или выберите уникальные значения.")
                if first and count:
                    output.write("\n")
                if not first:
                    output.write(request.separator)
                output.write(label)
                first, last_value = False, value
                count += 1
                if index % 4096 == 0:
                    progress(min(99, int((processed + index) / max(1, total) * 100)), count)
            if unique_values is not None:
                entries = list(unique_values.values())
                if request.sort != "original":
                    def sort_key(item):
                        value, label = item
                        if channel.radix in {"SIGNED", "SIGNED_MAGNITUDE", "UNSIGNED"}:
                            try:
                                return 0, int(label)
                            except ValueError:
                                pass
                        if channel.radix == "REAL":
                            try:
                                return 0, float(label)
                            except ValueError:
                                pass
                        return (0, value) if isinstance(value, (int, float)) else (1, str(value))
                    entries.sort(key=sort_key,
                                 reverse=request.sort == "descending")
                if entries and count:
                    output.write("\n")
                output.write(request.separator.join(label for _, label in entries))
                count += len(entries)
            processed += total_samples
        if cancel():
            raise LoadCancelled()
        progress(100, count)
        return CopyResult(output.getvalue(), count)


class SystemClipboard:
    def write(self, text):
        QApplication.clipboard().setText(text)
