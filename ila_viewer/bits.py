# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Cancellable, chunked, disk-backed extraction of actual bit transitions."""
from dataclasses import dataclass
from pathlib import Path
import re
from uuid import uuid4

import numpy as np

from .data import SignalData, LoadCancelled


class BitCodec:
    @staticmethod
    def labels(signal):
        match = re.search(r"\[(\d+)\s*:\s*(\d+)\]$", signal.name)
        if not match:
            return list(range(signal.width - 1, -1, -1))
        left, right = int(match[1]), int(match[2])
        return list(range(left, right + (1 if right >= left else -1), 1 if right >= left else -1))

    @staticmethod
    def binary(value, width, radix):
        if isinstance(value, int):
            return format(value & ((1 << width) - 1), f"0{width}b")
        group = {"HEX": 4, "OCTAL": 3, "OCT": 3, "BIN": 1, "BINARY": 1}.get(radix, 1)
        expanded = "".join(c * group if c in "XZ?" else format(int(c, 16), f"0{group}b") for c in value.upper())
        padding = expanded[0] if expanded and all(c == expanded[0] for c in expanded) and expanded[0] in "XZ?" else "0"
        return expanded.rjust(width, padding)[-width:]

    @classmethod
    def bit(cls, value, width, offset, radix):
        if isinstance(value, int):
            return (value >> offset) & 1
        bit = cls.binary(value, width, radix)[width - 1 - offset]
        return int(bit) if bit in "01" else bit

    @classmethod
    def reverse(cls, value, width, radix):
        binary = cls.binary(value, width, radix)[::-1]
        return int(binary, 2) if all(c in "01" for c in binary) else binary


@dataclass
class BitExpansionResult:
    signals: list
    paths: list

    def close(self):
        for signal in self.signals:
            for data in (signal.starts, signal.codes):
                data._mmap.close()
        for path in self.paths:
            Path(path).unlink(missing_ok=True)


class BitExpansionService:
    def build(self, signal, folder, progress=lambda p, n: None, cancel=lambda: False):
        result = BitExpansionResult([], [])
        prefix = uuid4().hex
        labels = BitCodec.labels(signal)
        try:
            for position, label in enumerate(labels):
                if cancel():
                    raise LoadCancelled()
                offset = signal.width - 1 - position
                values = [0, 1, "X", "Z", "?"]
                lookup_ids = {v: i for i, v in enumerate(values)}
                lut = np.empty(len(signal.values), dtype=np.uint32)
                for a in range(0, len(lut), 65536):
                    if cancel():
                        raise LoadCancelled()
                    b = min(len(lut), a + 65536)
                    lut[a:b] = [lookup_ids[BitCodec.bit(v, signal.width, offset, signal.radix)] for v in signal.values[a:b]]
                start_path = Path(folder) / f"{prefix}-{offset}.starts"
                code_path = Path(folder) / f"{prefix}-{offset}.codes"
                result.paths.extend([start_path, code_path])
                previous = None
                with start_path.open("wb") as sf, code_path.open("wb") as cf:
                    for a in range(0, len(signal.starts), 262144):
                        if cancel():
                            raise LoadCancelled()
                        b = min(len(signal.starts), a + 262144)
                        codes = lut[signal.codes[a:b]]
                        changed = np.empty(len(codes), dtype=bool)
                        changed[0] = previous is None or codes[0] != previous
                        changed[1:] = codes[1:] != codes[:-1]
                        signal.starts[a:b][changed].tofile(sf)
                        codes[changed].tofile(cf)
                        previous = codes[-1]
                starts = np.memmap(start_path, dtype=np.int64, mode="r")
                try:
                    codes = np.memmap(code_path, dtype=np.uint32, mode="r")
                except BaseException:
                    starts._mmap.close()
                    raise
                base = re.sub(r"\[\d+\s*:\s*\d+\]$", "", signal.name)
                child = SignalData(f"{base}[{label}]", "BINARY", 1, True, values, starts, codes, lookup_ids)
                result.signals.append(child)
                progress(int((position + 1) / signal.width * 100), position + 1)
            return result
        except BaseException:
            result.close()
            raise
