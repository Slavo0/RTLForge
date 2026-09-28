# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Value-format strategies, independent of Qt and CSV storage."""
from __future__ import annotations

import math
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class RealSettings:
    mode: str = "float"
    fractional_bits: int = 0
    signed: bool = True


@dataclass
class DisplayOptions:
    radix: str = "DEFAULT"
    color: str | None = None
    reverse: bool = False
    real: RealSettings | None = None


class ValueFormat(ABC):
    key: str
    label: str

    @abstractmethod
    def format(self, bits: int, width: int, settings: RealSettings) -> str: ...

    @abstractmethod
    def parse(self, text: str, width: int, settings: RealSettings) -> int: ...


class IntegerFormat(ValueFormat):
    def __init__(self, key, label, base=10, signed=False, magnitude=False):
        self.key, self.label, self.base = key, label, base
        self.signed, self.magnitude = signed, magnitude

    def format(self, bits, width, settings):
        if self.magnitude:
            return ("-" if bits & (1 << (width - 1)) else "") + str(bits & ((1 << (width - 1)) - 1))
        if self.signed and bits & (1 << (width - 1)):
            return str(bits - (1 << width))
        if self.base == 10:
            return str(bits)
        spec, divisor = {2: ("b", 1), 8: ("o", 3), 16: ("X", 4)}[self.base]
        return format(bits, f"0{math.ceil(width / divisor)}{spec}")

    def parse(self, text, width, settings):
        value = int(text.replace("_", ""), self.base)
        if self.magnitude:
            if abs(value) >= 1 << (width - 1):
                raise ValueError("модуль не помещается в выбранную ширину")
            return abs(value) | ((1 << (width - 1)) if text.strip().startswith("-") else 0)
        if not -(1 << (width - 1)) <= value < 1 << width:
            raise ValueError(f"значение не помещается в {width} бит")
        return value & ((1 << width) - 1)


class AsciiFormat(ValueFormat):
    key, label = "ASCII", "ASCII"

    def format(self, bits, width, settings):
        data = bits.to_bytes((width + 7) // 8, "big")
        return "".join("\\\\" if c == 92 else chr(c) if 32 <= c <= 126 else f"\\x{c:02X}" for c in data)

    def parse(self, text, width, settings):
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            text = text[1:-1]
        data = bytearray()
        i = 0
        while i < len(text):
            if text[i:i+2] == "\\\\":
                data.append(92)
                i += 2
            elif text[i:i+2] == "\\x" and i + 4 <= len(text):
                data.append(int(text[i+2:i+4], 16))
                i += 4
            else:
                if ord(text[i]) > 127:
                    raise ValueError("ASCII поддерживает символы 0…127 и escapes \\xNN")
                data.append(ord(text[i]))
                i += 1
        bits = int.from_bytes(data, "big")
        if bits >= 1 << width or len(data) > (width + 7) // 8:
            raise ValueError("строка не помещается в шину")
        return bits


class RealFormat(ValueFormat):
    key, label = "REAL", "Real"

    def format(self, bits, width, settings):
        if settings.mode == "fixed":
            value = bits - (1 << width) if settings.signed and bits & (1 << (width - 1)) else bits
            return f"{value / (1 << settings.fractional_bits):.9g}"
        if width not in (32, 64):
            return "<Real: 32/64 bit>"
        return f"{struct.unpack('>f' if width == 32 else '>d', bits.to_bytes(width // 8, 'big'))[0]:.9g}"

    def parse(self, text, width, settings):
        number = float(text)
        if settings.mode == "fixed":
            scaled = number * (1 << settings.fractional_bits)
            if not math.isfinite(scaled) or scaled != round(scaled):
                raise ValueError("значение нельзя точно представить с заданным числом дробных бит")
            value = int(scaled)
            low, high = (-(1 << (width - 1)), 1 << (width - 1)) if settings.signed else (0, 1 << width)
            if not low <= value < high:
                raise ValueError("Real выходит за диапазон шины")
            return value & ((1 << width) - 1)
        if width not in (32, 64):
            raise ValueError("IEEE 754 требует 32 или 64 бита; используйте Real Settings → Fixed Point")
        try:
            return int.from_bytes(struct.pack('>f' if width == 32 else '>d', number), "big")
        except (OverflowError, struct.error) as exc:
            raise ValueError("Real выходит за диапазон") from exc


class FormatRegistry:
    def __init__(self):
        self.formats = {}

    def register(self, strategy: ValueFormat):
        self.formats[strategy.key] = strategy

    def effective(self, key, source_radix="HEX"):
        aliases = {"BIN": "BINARY", "OCT": "OCTAL", "DECIMAL": "UNSIGNED", "DEC": "UNSIGNED"}
        return aliases.get(source_radix, source_radix) if key == "DEFAULT" else key

    def format(self, value, width, key, settings=None, source_radix="HEX"):
        if isinstance(value, str):
            return value
        return self.formats[self.effective(key, source_radix)].format(
            value & ((1 << width) - 1), width, settings or RealSettings())

    def parse(self, text, width, key, settings=None, source_radix="HEX"):
        key = self.effective(key, source_radix)
        token = text.strip()
        if not token:
            raise ValueError("введите значение")
        if token.lower().startswith(("0x", "0b", "0o")) and key != "ASCII":
            bits = int(token, 0)
            if not 0 <= bits < 1 << width:
                raise ValueError(f"значение не помещается в {width} бит")
            return bits
        if key not in ("ASCII", "REAL") and any(c in token.lower() for c in "xz?"):
            alphabet = {"BINARY": "01xz?_", "OCTAL": "01234567xz?_", "HEX": "0123456789abcdefxz?_"}.get(key, "xz?_")
            if any(c not in alphabet for c in token.lower()):
                raise ValueError("неверное значение X/Z")
            return token.replace("_", "").upper()
        return self.formats[key].parse(token, width, settings or RealSettings())


FORMATS = FormatRegistry()
for strategy in (
    IntegerFormat("BINARY", "Binary", 2), IntegerFormat("HEX", "Hexadecimal", 16),
    IntegerFormat("OCTAL", "Octal", 8), AsciiFormat(),
    IntegerFormat("UNSIGNED", "Unsigned Decimal"), IntegerFormat("SIGNED", "Signed Decimal", signed=True),
    IntegerFormat("SIGNED_MAGNITUDE", "Signed Magnitude", magnitude=True), RealFormat(),
):
    FORMATS.register(strategy)
