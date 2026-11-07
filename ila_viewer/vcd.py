# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Streaming VCD importer for simulator captures, with disk-backed transitions."""

from array import array
from pathlib import Path
import re
import tempfile

import numpy as np

from .data import Capture, LoadCancelled, SignalData, _Writer


class SplitBusAssembler:
    """Rebuild contiguous scalar VCD declarations as a presentation-ready bus."""

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


class VcdCaptureReader:
    """Map each distinct VCD timestamp to one event row without expanding idle time."""

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
                buses = SplitBusAssembler(signals)
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

    @staticmethod
    def _tokens(stream, cancel=lambda: False, block_chars=65536):
        """Whitespace tokens with carry across bounded reads, independent of lines."""
        tail = ""
        while True:
            if cancel():
                raise LoadCancelled()
            block = stream.read(block_chars)
            if not block:
                if tail:
                    yield tail
                return
            text = tail + block
            parts = text.split()
            tail = ""
            if text and not text[-1].isspace():
                tail = parts.pop() if parts else text
            yield from parts

    @staticmethod
    def _header(tokens):
        signals, by_id, scopes = [], {}, []
        timescale = "1 ns"
        for directive in tokens:
            parts = []
            for token in tokens:
                if token == "$end":
                    break
                if directive in {"$scope", "$var", "$timescale"}:
                    parts.append(token)
            else:
                break
            if directive == "$scope" and len(parts) >= 2:
                scopes.append(parts[1])
            elif directive == "$upscope" and scopes:
                scopes.pop()
            elif directive == "$timescale":
                timescale = "".join(parts)
            elif directive == "$var" and len(parts) >= 4 and parts[0].lower() not in {"real", "realtime", "string"}:
                width = int(parts[1])
                if width < 1:
                    raise ValueError("Некорректная ширина сигнала VCD.")
                symbol = parts[2]
                reference = parts[3].lstrip("\\")
                suffix = "".join(parts[4:])
                if suffix.startswith("[") and not reference.endswith("]"):
                    reference += suffix
                name = "/".join([*scopes, reference])
                by_id.setdefault(symbol, []).append(len(signals))
                signals.append(SignalData(name, "BINARY", width, True))
            elif directive == "$enddefinitions":
                return signals, by_id, timescale
        raise ValueError("В VCD нет завершения заголовка $enddefinitions.")
