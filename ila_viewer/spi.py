# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""SPI / QSPI decoding: chip-select frames into command, address, dummy and data fields.

Lines follow the usual serial NOR naming: IO0 (MOSI, DQ0), IO1 (MISO, DQ1), IO2, IO3.
A phase written as ``command-address-data`` (``1-4-4``, ``1-1-1``, ``4-4-4``…) gives
the number of lines each phase uses. Within one transfer the highest line carries the
most significant bit: ``IO1 IO0`` for two lines, ``IO3 IO2 IO1 IO0`` for four.

The decoder reads the run-length indices of the capture and never changes samples.
Results are written to disk next to the capture indices and mapped read-only, so
long logs do not need the decoded bytes in memory. Every user choice (lines, sample
edge, frame format, command table, dummy cycles) is explicit; nothing is guessed from
the traffic.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
import re
import tempfile

import numpy as np

from .bits import BitCodec
from .data import LoadCancelled
from .files import atomic_write

# Field kinds.
CMD, ADDR, DUMMY, READ, WRITE, MOSI, MISO, DATA, RESET = range(9)
KIND_LABELS = {CMD: "CMD", ADDR: "ADDR", DUMMY: "DUMMY", READ: "RD", WRITE: "WR",
               MOSI: "MOSI", MISO: "MISO", DATA: "DATA", RESET: "RESET"}
DATA_KINDS = frozenset({READ, WRITE, MOSI, MISO, DATA})

# Field flags.
UNKNOWN = 1   # a bit was X/Z, or its line is not assigned
PARTIAL = 2   # chip select ended before the unit was complete

# Frame flags (UNKNOWN and PARTIAL keep their meaning for the frame as a whole).
UNKNOWN_COMMAND = 4
TRUNCATED = 8  # chip select was already active at the capture start, or still active at its end
RESET_FRAME = 16  # only ones on the command lines: a mode-exit / power-loss recovery sequence

FIELD_DTYPE = np.dtype([("start", "<i8"), ("end", "<i8"), ("value", "<i8"), ("frame", "<i4"),
                        ("kind", "u1"), ("flags", "u1"), ("bits", "u1"), ("lines", "u1")])
FRAME_DTYPE = np.dtype([("start", "<i8"), ("end", "<i8"), ("address", "<i8"), ("opcode", "<i2"),
                        ("cmd_lines", "u1"), ("addr_lines", "u1"), ("data_lines", "u1"),
                        ("address_bytes", "u1"), ("dummy", "u1"), ("flags", "u1"), ("dtr", "u1"),
                        ("direction", "u1"), ("first", "<i8", (2,)), ("count", "<i8", (2,)),
                        ("data_bytes", "<i8")])

DIRECTIONS = ("none", "read", "write")
PROTOCOLS = {"extended": 1, "dual": 2, "quad": 4}
LINE_NAMES = ("IO0", "IO1", "IO2", "IO3")


@dataclass(frozen=True)
class LineRef:
    """One wire: a 1-bit signal, or one physical bit of a bus (``offset`` 0 is the LSB)."""

    signal: str
    offset: int = 0
    label: str = field(default="", compare=False)  # display only; identity is signal + bit

    @property
    def text(self):
        return self.label or self.signal.rsplit("/", 1)[-1]

    def resolve(self, capture):
        signal = next((s for s in capture.signals if s.name == self.signal), None)
        if signal is None or not 0 <= self.offset < signal.width:
            return None
        return signal


def line_options(signals):
    """Every 1-bit signal and every bit of every bus, as selectable wires."""
    options = []
    for signal in signals:
        if signal.width == 1:
            options.append(LineRef(signal.name, 0, signal.short_name))
            continue
        labels = BitCodec.labels(signal)
        stem = re.sub(r"\[\d+\s*:\s*\d+\]$", "", signal.short_name)
        for index, label in enumerate(labels):
            options.append(LineRef(signal.name, signal.width - 1 - index, f"{stem}[{label}]"))
    return options


def line_levels(signal, offset):
    """Value code -> 0/1, or -1 for X/Z, for one bit of a signal."""
    lut = np.full(max(1, len(signal.values)), -1, dtype=np.int8)
    for code, value in enumerate(signal.values):
        bit = BitCodec.bit(value, signal.width, offset, signal.radix)
        if bit in (0, 1):
            lut[code] = bit
    return lut


@dataclass(frozen=True)
class BusWidths:
    """Lines per phase: ``1-4-4`` means command on 1 line, address and data on 4."""

    command: int
    address: int
    data: int

    @classmethod
    def parse(cls, text):
        match = re.fullmatch(r"\s*([0124])\s*-\s*([0124])\s*-\s*([0124])\s*", text or "")
        if not match:
            raise ValueError(f"Формат «{text}»: ожидается команда-адрес-данные, например 1-4-4 "
                             "(допустимо 0, 1, 2 или 4 линии)")
        return cls(int(match[1]), int(match[2]), int(match[3]))

    @property
    def text(self):
        return f"{self.command}-{self.address}-{self.data}"

    def scaled(self, lines):
        """Same phases with every used phase on ``lines`` lines (dual/quad protocol fallback)."""
        return BusWidths(*(lines if width else 0 for width in (self.command, self.address, self.data)))


@dataclass(frozen=True)
class CommandSpec:
    opcode: int
    name: str
    formats: tuple            # (extended, dual, quad) BusWidths text, "" when not specified
    address_bytes: int = 0
    dummy: tuple = (0, 0, 0)  # dummy clocks for extended, dual, quad protocol
    direction: str = "none"   # data phase: "read" (from device), "write" (to device), "none"
    dtr: bool = False         # address and data transfer on both clock edges
    configurable_dummy: bool = False  # follows the "dummy cycles" setting (FAST READ family)

    def widths(self, protocol):
        index = ("extended", "dual", "quad").index(protocol)
        text = self.formats[index]
        if text:
            return BusWidths.parse(text)
        base = BusWidths.parse(next((f for f in self.formats if f), "1-0-0"))
        return base.scaled(PROTOCOLS[protocol])

    def dummy_for(self, protocol):
        return int(self.dummy[("extended", "dual", "quad").index(protocol)])


def _spec(opcode, name, formats, address=0, dummy=(0, 0, 0), direction="none", dtr=False, configurable=False):
    return CommandSpec(opcode, name, tuple(formats), address, tuple(dummy), direction, dtr, configurable)


def default_commands():
    """A common serial NOR command set; every entry can be edited in the decoder dialog."""
    s = _spec
    q = ("1-0-0", "2-0-0", "4-0-0")
    rr = ("1-0-1", "2-0-2", "4-0-4")
    a = ("1-1-0", "2-2-0", "4-4-0")
    m = ("1-1-1", "2-2-2", "4-4-4")
    return (
        s(0x66, "RESET ENABLE", q), s(0x99, "RESET MEMORY", q),
        s(0x9E, "READ ID", ("1-0-1", "", ""), direction="read"),
        s(0x9F, "READ ID", ("1-0-1", "", ""), direction="read"),
        s(0xAF, "MULTIPLE I/O READ ID", rr, direction="read"),
        s(0x5A, "READ SFDP", m, 3, (8, 8, 8), "read"),
        s(0x03, "READ", ("1-1-1", "", ""), 3, direction="read"),
        s(0x0B, "FAST READ", m, 3, (8, 8, 10), "read", configurable=True),
        s(0x3B, "DUAL OUTPUT FAST READ", ("1-1-2", "2-2-2", ""), 3, (8, 8, 8), "read", configurable=True),
        s(0xBB, "DUAL I/O FAST READ", ("1-2-2", "2-2-2", ""), 3, (8, 8, 8), "read", configurable=True),
        s(0x6B, "QUAD OUTPUT FAST READ", ("1-1-4", "", "4-4-4"), 3, (8, 8, 10), "read", configurable=True),
        s(0xEB, "QUAD I/O FAST READ", ("1-4-4", "", "4-4-4"), 3, (10, 10, 10), "read", configurable=True),
        s(0x0D, "DTR FAST READ", m, 3, (6, 6, 8), "read", True, True),
        s(0x3D, "DTR DUAL OUTPUT FAST READ", ("1-1-2", "2-2-2", ""), 3, (6, 6, 6), "read", True, True),
        s(0xBD, "DTR DUAL I/O FAST READ", ("1-2-2", "2-2-2", ""), 3, (6, 6, 6), "read", True, True),
        s(0x6D, "DTR QUAD OUTPUT FAST READ", ("1-1-4", "", "4-4-4"), 3, (6, 6, 8), "read", True, True),
        s(0xED, "DTR QUAD I/O FAST READ", ("1-4-4", "", "4-4-4"), 3, (8, 8, 8), "read", True, True),
        s(0xE7, "QUAD I/O WORD READ", ("1-4-4", "", "4-4-4"), 3, (4, 4, 4), "read"),
        s(0x13, "4-BYTE READ", ("1-1-1", "", ""), 4, direction="read"),
        s(0x0C, "4-BYTE FAST READ", m, 4, (8, 8, 10), "read", configurable=True),
        s(0x6C, "4-BYTE QUAD OUTPUT FAST READ", ("1-1-4", "", "4-4-4"), 4, (8, 8, 10), "read", configurable=True),
        s(0xEC, "4-BYTE QUAD I/O FAST READ", ("1-4-4", "", "4-4-4"), 4, (10, 10, 10), "read", configurable=True),
        s(0x06, "WRITE ENABLE", q), s(0x04, "WRITE DISABLE", q),
        s(0x05, "READ STATUS REGISTER", rr, direction="read"),
        s(0x70, "READ FLAG STATUS REGISTER", rr, direction="read"),
        s(0xB5, "READ NONVOLATILE CONFIGURATION REGISTER", rr, direction="read"),
        s(0x85, "READ VOLATILE CONFIGURATION REGISTER", rr, direction="read"),
        s(0x65, "READ ENHANCED VOLATILE CONFIGURATION REGISTER", rr, direction="read"),
        s(0x96, "READ GENERAL PURPOSE READ REGISTER", rr, 0, (8, 8, 8), "read"),
        s(0x01, "WRITE STATUS REGISTER", rr, direction="write"),
        s(0xB1, "WRITE NONVOLATILE CONFIGURATION REGISTER", rr, direction="write"),
        s(0x81, "WRITE VOLATILE CONFIGURATION REGISTER", rr, direction="write"),
        s(0x61, "WRITE ENHANCED VOLATILE CONFIGURATION REGISTER", rr, direction="write"),
        s(0x50, "CLEAR FLAG STATUS REGISTER", q),
        s(0x02, "PAGE PROGRAM", m, 3, direction="write"),
        s(0xA2, "DUAL INPUT FAST PROGRAM", ("1-1-2", "2-2-2", ""), 3, direction="write"),
        s(0xD2, "EXTENDED DUAL INPUT FAST PROGRAM", ("1-2-2", "2-2-2", ""), 3, direction="write"),
        s(0x32, "QUAD INPUT FAST PROGRAM", ("1-1-4", "", "4-4-4"), 3, direction="write"),
        s(0x38, "EXTENDED QUAD INPUT FAST PROGRAM", ("1-4-4", "", "4-4-4"), 3, direction="write"),
        s(0x12, "4-BYTE PAGE PROGRAM", m, 4, direction="write"),
        s(0x34, "4-BYTE QUAD INPUT FAST PROGRAM", ("1-1-4", "", "4-4-4"), 4, direction="write"),
        s(0x3E, "4-BYTE QUAD INPUT EXTENDED FAST PROGRAM", ("1-4-4", "", "4-4-4"), 4, direction="write"),
        s(0x52, "32KB SUBSECTOR ERASE", a, 3), s(0x20, "4KB SUBSECTOR ERASE", a, 3),
        s(0xD8, "SECTOR ERASE", a, 3),
        s(0x21, "4-BYTE 4KB SUBSECTOR ERASE", a, 4), s(0x5C, "4-BYTE 32KB SUBSECTOR ERASE", a, 4),
        s(0xDC, "4-BYTE SECTOR ERASE", a, 4),
        s(0xC7, "BULK ERASE", q), s(0x60, "BULK ERASE", q),
        s(0x75, "PROGRAM/ERASE SUSPEND", q), s(0x7A, "PROGRAM/ERASE RESUME", q),
        s(0x4B, "READ OTP ARRAY", m, 3, (8, 8, 10), "read", configurable=True),
        s(0x42, "PROGRAM OTP ARRAY", m, 3, direction="write"),
        s(0x35, "ENTER QUAD I/O MODE", q), s(0xF5, "RESET QUAD I/O MODE", q),
        s(0xB7, "ENTER 4-BYTE ADDRESS MODE", q), s(0xE9, "EXIT 4-BYTE ADDRESS MODE", q),
        s(0xB9, "ENTER DEEP POWER-DOWN", q), s(0xAB, "RELEASE FROM DEEP POWER-DOWN", q),
        s(0x2D, "READ SECTOR PROTECTION", rr, direction="read"),
        s(0x2C, "PROGRAM SECTOR PROTECTION", rr, direction="write"),
        s(0xE8, "READ VOLATILE LOCK BITS", m, 3, direction="read"),
        s(0xE5, "WRITE VOLATILE LOCK BITS", m, 3, direction="write"),
        s(0xE2, "READ NONVOLATILE LOCK BITS", m, 4, direction="read"),
        s(0xE3, "WRITE NONVOLATILE LOCK BITS", a, 4),
        s(0xE4, "ERASE NONVOLATILE LOCK BITS", q),
        s(0xA7, "READ GLOBAL FREEZE BIT", ("1-0-1", "", ""), direction="read"),
        s(0xA6, "WRITE GLOBAL FREEZE BIT", q),
        s(0x27, "READ PASSWORD", ("1-0-1", "", ""), direction="read"),
        s(0x28, "WRITE PASSWORD", rr, direction="write"),
        s(0x29, "UNLOCK PASSWORD", rr, direction="write"),
        s(0x9B, "INTERFACE ACTIVATION / CRC", rr, direction="write"),
    )


@dataclass(frozen=True)
class FixedFormat:
    """The same frame layout for every chip-select frame, set by the user."""

    widths: BusWidths = BusWidths(1, 4, 4)
    address_bytes: int = 3
    dummy: int = 10
    direction: str = "read"
    dtr: str = "off"   # "off", "address_data" (command stays single rate) or "all"


@dataclass(frozen=True)
class SpiPins:
    sck: LineRef
    cs: LineRef | None = None
    cs_active_low: bool = True
    io: tuple = (None, None, None, None)

    def lines(self):
        return [self.sck, self.cs, *self.io]

    def missing(self, capture):
        return [line.text for line in self.lines() if line is not None and line.resolve(capture) is None]


@dataclass(frozen=True)
class SpiSettings:
    mode: str = "commands"         # "commands" (by opcode), "fixed" or "raw"
    sample_edge: str = "rising"    # rising: SPI modes 0 and 3; falling: modes 1 and 2
    before_edge: bool = True       # read lines just before the edge, as a flip-flop sees them
    msb_first: bool = True
    protocol: str = "extended"     # command lines: extended 1, dual 2, quad 4
    address_bytes: int = 3         # 4: three-byte-address commands use four bytes
    dummy_cycles: int | None = None  # overrides the table for configurable (FAST READ) commands
    dtr: bool = False              # double transfer rate protocol: every phase on both edges
    commands: tuple = field(default_factory=default_commands)
    fixed: FixedFormat = FixedFormat()
    raw_lines: int = 1
    # Frames with only ones on the command lines (any clock count) are the controller's
    # mode-exit / power-loss recovery sequence, shown as RESET instead of an unknown command.
    reset_frames: bool = True

    def __post_init__(self):
        if self.mode not in ("commands", "fixed", "raw"):
            raise ValueError(f"Неизвестный режим декодера: {self.mode}")
        if self.sample_edge not in ("rising", "falling"):
            raise ValueError("Фронт выборки: rising или falling")
        if self.protocol not in PROTOCOLS:
            raise ValueError(f"Неизвестный протокол: {self.protocol}")
        if self.address_bytes not in (3, 4):
            raise ValueError("Адрес команд: 3 или 4 байта")
        if self.raw_lines not in (1, 2, 4):
            raise ValueError("Только байты: 1, 2 или 4 линии данных")
        if self.fixed.address_bytes not in range(0, 5) or not 0 <= self.fixed.dummy <= 255:
            raise ValueError("Фиксированный формат: адрес 0…4 байта, dummy 0…255 тактов")
        if self.dummy_cycles is not None and not 0 <= self.dummy_cycles <= 255:
            raise ValueError("Dummy: 0…255 тактов")
        if self.fixed.direction not in DIRECTIONS or self.fixed.dtr not in ("off", "address_data", "all"):
            raise ValueError("Неверное направление или режим DTR фиксированного формата")
        opcodes = [spec.opcode for spec in self.commands]
        if len(set(opcodes)) != len(opcodes):
            raise ValueError("Коды команд в таблице повторяются")

    def command(self, opcode):
        return next((spec for spec in self.commands if spec.opcode == opcode), None)

    def describe(self):
        edge = "↑" if self.sample_edge == "rising" else "↓"
        if self.mode == "raw":
            return f"только байты, {self.raw_lines} лин., SCK {edge}"
        if self.mode == "fixed":
            f = self.fixed
            return (f"фиксированный {f.widths.text}, адрес {f.address_bytes} Б, dummy {f.dummy}"
                    + (f", DTR {f.dtr}" if f.dtr != "off" else "") + f", SCK {edge}")
        dummy = "по таблице" if self.dummy_cycles is None else str(self.dummy_cycles)
        return (f"по командам, {self.protocol}{' DTR' if self.dtr else ''}, адрес {self.address_bytes} Б, "
                f"dummy {dummy}, SCK {edge}")


@dataclass(frozen=True)
class SpiDecoderConfig:
    pins: SpiPins
    settings: SpiSettings = SpiSettings()

    def describe(self):
        return self.settings.describe()


@dataclass(frozen=True)
class _DataLane:
    lane: int
    kind: int
    lines: tuple


@dataclass(frozen=True)
class _Plan:
    """How the rest of a frame is laid out once its command (if any) is known."""

    address_lines: tuple = ()
    address_bytes: int = 0
    dummy: int = 0
    dtr_rest: bool = False
    data: tuple = ()
    data_lines: int = 0
    direction: str = "none"
    known: bool = True


def _lines(width, incoming):
    if width == 1:
        return (1,) if incoming else (0,)
    if width == 2:
        return (1, 0)
    if width == 4:
        return (3, 2, 1, 0)
    return ()


class _LineSampler:
    def __init__(self, signal, offset):
        self.signal = signal
        self.lut = line_levels(signal, offset) if signal is not None else None

    def at(self, positions):
        if self.signal is None:
            return np.full(len(positions), -1, dtype=np.int8)
        index = np.searchsorted(self.signal.starts, positions, side="right") - 1
        np.maximum(index, 0, out=index)
        return self.lut[np.asarray(self.signal.codes[index], dtype=np.int64)]


class _EdgeStream:
    """SCK edges of one frame, read in bounded chunks; STR takes sample edges only."""

    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.pos = np.empty(0, dtype=np.int64)
        self.sample = np.empty(0, dtype=bool)
        self.index = 0
        self.done = False
        self.last = None

    def _more(self):
        chunk = next(self.chunks, None)
        if chunk is None:
            self.done = True
            return False
        pos, sample = chunk
        self.pos = np.concatenate((self.pos[self.index:], pos))
        self.sample = np.concatenate((self.sample[self.index:], sample))
        self.index = 0
        return True

    def take(self, count, dtr=False):
        """Positions of the next ``count`` transfers; fewer if the frame ends first."""
        if count <= 0:
            return np.empty(0, dtype=np.int64)
        while True:
            chosen = np.flatnonzero(self.sample[self.index:])
            if dtr:
                # Double rate starts on the next sampling edge, then uses every edge.
                if len(chosen):
                    first = self.index + int(chosen[0])
                    if len(self.pos) - first >= count or self.done:
                        selected = np.arange(first, min(len(self.pos), first + count))
                        break
                elif self.done:
                    selected = np.empty(0, dtype=np.int64)
                    break
            elif len(chosen) >= count or self.done:
                selected = self.index + chosen[:count]
                break
            self._more()  # sets ``done`` at the end of the frame; the loop then stops
        if len(selected):
            self.index = int(selected[-1]) + 1
            self.last = int(self.pos[selected[-1]])
        else:
            self.index = len(self.pos)
        return self.pos[selected]

    def rest(self, dtr=False):
        """Remaining transfer positions in chunks, for the open-ended data phase."""
        started = False
        while True:
            pos, sample = self.pos[self.index:], self.sample[self.index:]
            self.index = len(self.pos)
            if dtr and not started:
                chosen = np.flatnonzero(sample)
                if len(chosen):
                    pos = pos[int(chosen[0]):]
                    started = True
                else:
                    pos = pos[:0]
            elif not dtr:
                pos = pos[sample]
            if len(pos):
                self.last = int(pos[-1])
                yield pos
            if self.done or not self._more():
                return


class _RecordWriter:
    BATCH = 65536  # records per disk write; frames produce many tiny arrays

    def __init__(self, folder, prefix, dtype):
        stream = tempfile.NamedTemporaryFile(dir=folder, prefix=prefix, suffix=".bin", delete=False)
        self.path = Path(stream.name)
        self.stream = stream
        self.dtype = dtype
        self.count = 0
        self.pending = []
        self.pending_count = 0

    def write(self, records):
        if len(records):
            self.pending.append(np.array(records, copy=True))
            self.pending_count += len(records)
            self.count += len(records)
            if self.pending_count >= self.BATCH:
                self.flush()

    def flush(self):
        if self.pending:
            np.concatenate(self.pending).tofile(self.stream)
            self.pending, self.pending_count = [], 0

    def close(self):
        if not self.stream.closed:
            self.flush()
            self.stream.close()

    def discard(self):
        self.close()
        self.path.unlink(missing_ok=True)


class _FrameLane:
    """Fields of one lane in one frame; each field ends where the next one starts."""

    def __init__(self, writer, frame):
        self.writer = writer
        self.frame = frame
        self.pending = None
        self.first = writer.count + 0
        self.data_bytes = 0

    def push(self, starts, values, kind, flags, bits, lines, ends=None):
        """Append fields; ``ends`` are where each unit's last bit ends (clipped to the next start)."""
        count = len(starts)
        if not count:
            return
        records = np.zeros(count, dtype=FIELD_DTYPE)
        records["start"] = starts
        natural = np.full(count, np.iinfo(np.int64).max, dtype=np.int64) if ends is None else             np.asarray(ends, dtype=np.int64)
        records["value"] = values
        records["frame"] = self.frame
        records["kind"] = kind
        records["flags"] = flags
        records["bits"] = bits
        records["lines"] = lines
        if kind in DATA_KINDS:
            self.data_bytes += int(np.count_nonzero((records["flags"] & PARTIAL) == 0))
        if self.pending is not None:
            self._close_pending(int(starts[0]))
        records["end"][:-1] = np.maximum(records["start"][:-1] + 1, np.minimum(natural[:-1], records["start"][1:]))
        self.writer.write(records[:-1])
        self.pending = records[-1:].copy()
        self.pending_end = int(natural[-1])

    def _close_pending(self, limit):
        self.pending["end"] = max(int(self.pending["start"][0]) + 1, min(self.pending_end, int(limit)))
        self.writer.write(self.pending)
        self.pending = None

    def finish(self, end):
        if self.pending is not None:
            self._close_pending(end)
        return self.first, self.writer.count - self.first


def _unit_ends(positions, per_unit):
    """Where each unit's last bit ends: half a transfer step after its last edge.

    ``positions`` holds ``per_unit`` transfer positions per unit (a 2-D view or a flat
    array of one unit). With SCK sampling on one edge, data changes on the other, so the
    last bit stays valid for half a clock period after its sampling edge.
    """
    rows = np.asarray(positions, dtype=np.int64).reshape(-1, per_unit)
    last = rows[:, -1]
    if per_unit > 1:
        step = (rows[:, -1] - rows[:, 0]) / (per_unit - 1)
    else:
        step = np.ones(len(rows))
    return last + np.maximum(1, np.ceil(step / 2)).astype(np.int64)


def _pack(bits, msb_first=True):
    """Rows of 0/1/-1 bits -> (values, unknown flags)."""
    width = bits.shape[1]
    weights = (1 << np.arange(width - 1, -1, -1, dtype=np.int64)) if msb_first else \
        (1 << np.arange(width, dtype=np.int64))
    values = (np.where(bits > 0, 1, 0).astype(np.int64) * weights).sum(axis=1)
    unknown = (bits < 0).any(axis=1)
    return values, unknown


class SpiDecoder:
    """Background-safe decoder: no Qt, cancellable, disk-backed output."""

    BLOCK = 65536

    def decode(self, capture, config, folder=None, progress=lambda p, n: None, cancel=lambda: False):
        pins, settings = config.pins, config.settings
        missing = pins.missing(capture)
        if missing:
            raise ValueError("Не найдены сигналы декодера: " + ", ".join(missing))
        sck_signal = pins.sck.resolve(capture)
        self.settings = settings
        self.sck = sck_signal
        self.sck_levels = line_levels(sck_signal, pins.sck.offset)
        self.samplers = [_LineSampler(line.resolve(capture) if line else None, line.offset if line else 0)
                         for line in pins.io]
        self.count = capture.count
        self.has_cs = pins.cs is not None
        folder = folder or capture.storage.name
        frames = self._frames(capture, pins, cancel)
        writers = [_RecordWriter(folder, "spi_lane0_", FIELD_DTYPE), _RecordWriter(folder, "spi_lane1_", FIELD_DTYPE)]
        frame_writer = _RecordWriter(folder, "spi_frames_", FRAME_DTYPE)
        try:
            for index, (start, end) in enumerate(frames):
                if index % 64 == 0:
                    if cancel():
                        raise LoadCancelled()
                    progress(round(100 * int(start) / max(1, capture.count)), index)
                record = self._frame(index, int(start), int(end), writers, cancel)
                frame_writer.write(record)
            for writer in (*writers, frame_writer):
                writer.close()
            progress(100, frame_writer.count)
            return SpiDecodeResult(config, frame_writer.path, frame_writer.count,
                                   [w.path for w in writers], [w.count for w in writers], capture.count)
        except BaseException:
            for writer in (*writers, frame_writer):
                writer.discard()
            raise

    def _frames(self, capture, pins, cancel=lambda: False):
        """Stream CS-active spans with state carried over transition blocks."""
        if cancel():
            raise LoadCancelled()
        if pins.cs is None:
            yield 0, capture.count
            return
        cs = pins.cs.resolve(capture)
        levels = line_levels(cs, pins.cs.offset)
        active_level = 0 if pins.cs_active_low else 1
        previous, pending = False, None
        for a in range(0, len(cs.starts), self.BLOCK):
            if cancel():
                raise LoadCancelled()
            b = min(len(cs.starts), a + self.BLOCK)
            active = levels[np.asarray(cs.codes[a:b], dtype=np.int64)] == active_level
            changes = np.flatnonzero(np.r_[active[0] != previous, active[1:] != active[:-1]])
            for offset in changes:
                if active[offset]:
                    pending = int(cs.starts[a + offset])
                else:
                    yield pending, int(cs.starts[a + offset])
                    pending = None
            previous = bool(active[-1])
        if pending is not None:
            yield pending, capture.count

    def _edge_chunks(self, start, end, cancel):
        starts, codes = self.sck.starts, self.sck.codes
        first = max(1, int(np.searchsorted(starts, start, side="left")))
        last = int(np.searchsorted(starts, end, side="left"))
        rising_edge = self.settings.sample_edge == "rising"
        for a in range(first, last, self.BLOCK):
            if cancel():
                raise LoadCancelled()
            b = min(last, a + self.BLOCK)
            states = self.sck_levels[np.asarray(codes[a - 1:b], dtype=np.int64)]
            before, after = states[:-1], states[1:]
            rising = (before == 0) & (after == 1)
            falling = (before == 1) & (after == 0)
            mask = rising | falling
            yield np.asarray(starts[a:b], dtype=np.int64)[mask], (rising if rising_edge else falling)[mask]

    def _bits(self, positions, lines):
        at = positions - 1 if self.settings.before_edge else positions
        at = np.maximum(at, 0)
        return np.stack([self.samplers[line].at(at) for line in lines], axis=1)

    def _unit(self, positions, lines, bits_wanted, msb_first=True):
        """One field from transfers: (value, flags, bit count)."""
        if not len(positions):
            return None
        stream = self._bits(positions, lines).reshape(1, -1)
        bits = stream.shape[1]
        values, unknown = _pack(stream, msb_first)
        flags = (UNKNOWN if unknown[0] else 0) | (PARTIAL if bits < bits_wanted else 0)
        return int(values[0]), flags, bits

    def _plan(self, opcode):
        s = self.settings
        if s.mode == "fixed":
            f = s.fixed
            dtr = f.dtr != "off"
            return _Plan(_lines(f.widths.address, False) if f.address_bytes else (), f.address_bytes
                         if f.widths.address else 0, f.dummy, dtr, self._data_lanes(f.widths.data, f.direction),
                         f.widths.data, f.direction)
        spec = s.command(opcode)
        width = PROTOCOLS[s.protocol]
        if spec is None:
            lanes = ((_DataLane(0, MOSI, (0,)), _DataLane(1, MISO, (1,))) if width == 1
                     else (_DataLane(0, DATA, _lines(width, False)),))
            return _Plan(data=lanes, data_lines=width, known=False)
        widths = spec.widths(s.protocol)
        address_bytes = spec.address_bytes if widths.address else 0
        if address_bytes == 3 and s.address_bytes == 4:
            address_bytes = 4
        dummy = spec.dummy_for(s.protocol)
        if spec.configurable_dummy and s.dummy_cycles is not None:
            dummy = s.dummy_cycles
        return _Plan(_lines(widths.address, False), address_bytes, dummy, s.dtr or spec.dtr,
                     self._data_lanes(widths.data, spec.direction), widths.data, spec.direction)

    @staticmethod
    def _data_lanes(width, direction):
        if not width or direction == "none":
            return ()
        return (_DataLane(0, READ if direction == "read" else WRITE, _lines(width, direction == "read")),)

    def _frame(self, index, start, end, writers, cancel):
        s = self.settings
        stream = _EdgeStream(self._edge_chunks(start, end, cancel))
        lanes = [_FrameLane(writers[0], index), _FrameLane(writers[1], index)]
        record = np.zeros(1, dtype=FRAME_DTYPE)
        record["start"], record["end"] = start, end
        record["opcode"], record["address"] = -1, -1
        flags = TRUNCATED if self.has_cs and (start == 0 or end >= self.count) else 0
        opcode = None
        command_lines = 0
        if s.mode == "commands":
            command_lines = PROTOCOLS[s.protocol]
        elif s.mode == "fixed":
            command_lines = s.fixed.widths.command
        dtr_command = (s.mode == "commands" and s.dtr) or (s.mode == "fixed" and s.fixed.dtr == "all")
        if command_lines and self._reset_frame(start, end, _lines(command_lines, False), dtr_command, cancel):
            return self._reset_record(record, flags, start, end, lanes, dtr_command, cancel)
        complete = True
        if command_lines:
            lines = _lines(command_lines, False)
            positions = stream.take(8 // command_lines, dtr_command)
            unit = self._unit(positions, lines, 8)
            if unit is not None:
                value, unit_flags, bits = unit
                lanes[0].push(positions[:1], [value], CMD, unit_flags, bits, command_lines,
                              _unit_ends(positions, len(positions)))
                flags |= unit_flags
                complete = not unit_flags & PARTIAL
                if complete and not unit_flags & UNKNOWN:
                    opcode = value
                    record["opcode"] = value
            else:
                complete = False
        record["cmd_lines"] = command_lines
        if s.mode == "raw":
            plan = _Plan(data=((_DataLane(0, MOSI, (0,)), _DataLane(1, MISO, (1,))) if s.raw_lines == 1
                               else (_DataLane(0, DATA, _lines(s.raw_lines, False)),)),
                         data_lines=s.raw_lines, direction="none")
        elif complete:
            plan = self._plan(opcode)
        else:
            plan = _Plan()
        if s.mode == "commands" and complete and opcode is None:
            plan = _Plan()  # an X/Z opcode cannot select a command layout
        if not plan.known:
            flags |= UNKNOWN_COMMAND
        if complete and plan.address_bytes and plan.address_lines:
            width = len(plan.address_lines)
            wanted = plan.address_bytes * 8
            positions = stream.take(wanted // width, plan.dtr_rest)
            unit = self._unit(positions, plan.address_lines, wanted)
            if unit is None:
                complete = False
            else:
                value, unit_flags, bits = unit
                lanes[0].push(positions[:1], [value], ADDR, unit_flags, bits, width,
                              _unit_ends(positions, len(positions)))
                flags |= unit_flags
                complete = not unit_flags & PARTIAL
                if not unit_flags:
                    record["address"] = value
        record["addr_lines"] = len(plan.address_lines)
        record["address_bytes"] = plan.address_bytes
        if complete and plan.dummy:
            positions = stream.take(plan.dummy, False)
            if len(positions):
                lanes[0].push(positions[:1], [plan.dummy], DUMMY, PARTIAL if len(positions) < plan.dummy else 0,
                              min(255, len(positions)), 0, _unit_ends(positions, len(positions)))
            complete = len(positions) == plan.dummy
        record["dummy"] = min(255, plan.dummy)
        record["dtr"] = int(plan.dtr_rest) | (2 if dtr_command else 0)
        record["data_lines"] = plan.data_lines
        record["direction"] = DIRECTIONS.index(plan.direction)
        if complete and plan.data:
            flags |= self._data(stream, plan, lanes)
        final = end
        if stream.last is not None:
            gap = self._gap(stream)
            final = min(end, stream.last + gap)
        for lane_index, lane in enumerate(lanes):
            first, count = lane.finish(final)
            record["first"][0, lane_index] = first
            record["count"][0, lane_index] = count
        record["data_bytes"] = lanes[0].data_bytes + lanes[1].data_bytes
        record["flags"] = flags
        return record

    def _reset_frame(self, start, end, lines, dtr, cancel):
        """True when every transfer of the frame reads 1 on every command line.

        Only an all-ones first byte is examined further, so ordinary frames cost one
        command read; ``FF`` listed in the command table keeps its table meaning.
        """
        s = self.settings
        if not s.reset_frames or (s.mode == "commands" and s.command(0xFF) is not None):
            return False
        stream = _EdgeStream(self._edge_chunks(start, end, cancel))
        head = stream.take(8 // len(lines), dtr)
        if not len(head) or not (self._bits(head, lines) == 1).all():
            return False
        return all((self._bits(chunk, lines) == 1).all() for chunk in stream.rest(dtr))

    def _reset_record(self, record, flags, start, end, lanes, dtr, cancel):
        stream = _EdgeStream(self._edge_chunks(start, end, cancel))
        first, count = None, 0
        for chunk in stream.rest(dtr):
            first = int(chunk[0]) if first is None else first
            count += len(chunk)
        final = min(end, stream.last + self._gap(stream))
        lanes[0].push(np.array([first]), [count], RESET, 0, min(255, count), 0, [final])
        for lane_index, lane in enumerate(lanes):
            record["first"][0, lane_index], record["count"][0, lane_index] = lane.finish(final)
        record["flags"] = flags | RESET_FRAME
        return record

    @staticmethod
    def _gap(stream):
        pos = stream.pos[:stream.index] if stream.index else stream.pos
        steps = np.diff(pos[-9:]) if len(pos) > 1 else np.empty(0)
        return max(1, int(np.median(steps))) if len(steps) else 1

    def _data(self, stream, plan, lanes):
        """Pack the open-ended data phase into bytes; a trailing short unit is PARTIAL."""
        s = self.settings
        width = plan.data_lines
        per_byte = 8 // width
        carry = np.empty(0, dtype=np.int64)
        flags = 0
        for chunk in stream.rest(plan.dtr_rest):
            positions = np.concatenate((carry, chunk))
            groups = len(positions) // per_byte
            if groups:
                used = positions[:groups * per_byte]
                for lane in plan.data:
                    bits = self._bits(used, lane.lines).reshape(groups, 8)
                    values, unknown = _pack(bits, s.msb_first)
                    lane_flags = np.where(unknown, UNKNOWN, 0)
                    flags |= UNKNOWN if unknown.any() else 0
                    lanes[lane.lane].push(used[::per_byte], values, lane.kind, lane_flags, 8, width,
                                          _unit_ends(used, per_byte))
            carry = positions[groups * per_byte:]
        if len(carry):
            for lane in plan.data:
                unit = self._unit(carry, lane.lines, 8, s.msb_first)
                value, unit_flags, bits = unit
                lanes[lane.lane].push(carry[:1], [value], lane.kind, unit_flags | PARTIAL, bits, width,
                                      _unit_ends(carry, len(carry)))
            flags |= PARTIAL
        return flags


class SpiDecodeResult:
    """Disk-backed frames and fields; ``close`` is idempotent and removes the files."""

    def __init__(self, config, frames_path, frame_count, lane_paths, lane_counts, capture_count):
        self.config = config
        self.capture_count = capture_count
        self.paths = [Path(frames_path), *map(Path, lane_paths)]
        self.frames = (np.memmap(frames_path, dtype=FRAME_DTYPE, mode="r", shape=(frame_count,))
                       if frame_count else np.empty(0, dtype=FRAME_DTYPE))
        self.lanes = [np.memmap(path, dtype=FIELD_DTYPE, mode="r", shape=(count,)) if count
                      else np.empty(0, dtype=FIELD_DTYPE) for path, count in zip(lane_paths, lane_counts)]
        for path, count in zip(self.paths, [frame_count, *lane_counts]):
            if not count:
                path.unlink(missing_ok=True)
        self.closed = False

    @property
    def settings(self):
        return self.config.settings

    @property
    def frame_count(self):
        return len(self.frames)

    @property
    def lane_count(self):
        """Lanes that carry fields; lane 1 holds MISO bytes of full-duplex frames."""
        return 1 + (len(self.lanes[1]) > 0 or (self.settings.mode == "raw" and self.settings.raw_lines == 1))

    def lane_title(self, lane):
        s = self.settings
        if s.mode == "raw" and s.raw_lines == 1:
            return "SPI MOSI" if lane == 0 else "SPI MISO"
        if lane == 1:
            return "SPI MISO"
        if s.mode == "commands":
            return "SPI/QSPI"
        widths = s.fixed.widths
        width = s.raw_lines if s.mode == "raw" else max(widths.command, widths.address, widths.data)
        return {1: "SPI", 2: "Dual SPI", 4: "QSPI"}.get(width, "SPI")

    def frame_fields(self, index, lane=0):
        frame = self.frames[index]
        first, count = int(frame["first"][lane]), int(frame["count"][lane])
        return self.lanes[lane][first:first + count]

    def frame_data(self, index, kinds=DATA_KINDS, lanes=(0,), limit=None):
        """Fields of the given kinds; with ``limit``, at most that many, read in bounded steps."""
        kind_list = list(kinds)
        parts, total = [], 0
        for lane in lanes:
            fields = self.frame_fields(index, lane)
            if limit is None:
                parts.append(fields[np.isin(fields["kind"], kind_list)])
                continue
            step = max(64, 4 * limit)
            for a in range(0, len(fields), step):
                chunk = np.asarray(fields[a:a + step])
                taken = chunk[np.isin(chunk["kind"], kind_list)][:limit - total]
                parts.append(taken)
                total += len(taken)
                if total >= limit:
                    break
            if limit is not None and total >= limit:
                break
        return np.concatenate(parts) if parts else np.empty(0, dtype=FIELD_DTYPE)

    def frame_at(self, sample):
        if not self.frame_count:
            return None
        index = int(np.searchsorted(self.frames["start"], sample, side="right")) - 1
        return index if index >= 0 and sample < int(self.frames["end"][index]) else None

    def command_name(self, opcode):
        if opcode is None or opcode < 0:
            return ""
        spec = self.settings.command(int(opcode))
        return spec.name if spec else "неизвестная команда"

    def frame_format(self, index):
        frame = self.frames[index]
        if frame["flags"] & RESET_FRAME:
            return "сброс"
        text = f"{frame['cmd_lines']}-{frame['addr_lines']}-{frame['data_lines']}"
        if frame["dtr"]:
            text += " DTR"
        return text

    def close(self):
        if self.closed:
            return
        self.closed = True
        for data in (self.frames, *self.lanes):
            if isinstance(data, np.memmap):
                data._mmap.close()
        self.frames = np.empty(0, dtype=FRAME_DTYPE)
        self.lanes = [np.empty(0, dtype=FIELD_DTYPE) for _ in self.lanes]
        for path in self.paths:
            path.unlink(missing_ok=True)


def decode_summary(result):
    """Status text after decoding; hints at the protocol when most commands are unknown."""
    frames = result.frames
    count = result.frame_count
    data = int(np.asarray(frames["data_bytes"]).sum()) if count else 0
    flags = np.asarray(frames["flags"]) if count else np.empty(0, dtype=np.uint8)
    resets = int(np.count_nonzero(flags & RESET_FRAME))
    problems = int(np.count_nonzero(flags & ~np.uint8(RESET_FRAME)))
    text = f"SPI: {count:,} транзакций, {data:,} байт данных"
    if resets:
        text += f", сбросов режима: {resets:,}"
    if problems:
        text += f", с замечаниями: {problems:,}"
    unknown = int(np.count_nonzero(flags & UNKNOWN_COMMAND))
    s = result.settings
    if s.mode == "commands" and unknown and 2 * unknown > count - resets:
        text += (f". Команд нет в таблице в {unknown:,} кадрах — проверьте протокол"
                 + (" (QPI/DPI: команда тоже на 4/2 линиях; обычный QSPI 1-4-4 — Extended)"
                    if s.protocol != "extended" else " и фронт SCK"))
    return text


def field_label(result, record, short=False):
    """Text drawn on the decoded row and shown in the Value column."""
    kind, value, flags, bits = int(record["kind"]), int(record["value"]), int(record["flags"]), int(record["bits"])
    if kind == DUMMY:
        return f"dummy {value}" if not flags & PARTIAL else f"dummy {bits}/{value}"
    if kind == RESET:
        return "RESET" if short else f"RESET {value} такт."
    if flags & UNKNOWN:
        text = "X" * max(2, (bits + 3) // 4)
    elif flags & PARTIAL:
        text = f"{value:X}/{bits}b"
    else:
        text = f"{value:0{max(1, (bits + 3) // 4)}X}"
    if kind == CMD:
        spec = None if short or flags else result.settings.command(value)
        name = spec.name if spec else ("" if short or flags or result.settings.mode != "commands"
                                       else "нет в таблице")
        return f"CMD {text}" + (f" {name}" if name else "")
    if kind == ADDR:
        return text if short else f"ADDR {text}"
    if short:
        return text
    return f"{KIND_LABELS[kind]} {text}"


class DecodedTrack:
    """Read-only view of one lane for drawing, the Value column and ←/→ navigation."""

    def __init__(self, result, lane, title):
        self.result = result
        self.lane = lane
        self.title = title

    @property
    def fields(self):
        return self.result.lanes[self.lane]

    def __len__(self):
        return len(self.fields)

    def window(self, left, right):
        """Index range of fields intersecting [left, right)."""
        fields = self.fields
        if not len(fields):
            return 0, 0
        first = int(np.searchsorted(fields["end"], left, side="right"))
        stop = int(np.searchsorted(fields["start"], right, side="left"))
        return first, max(first, stop)

    def busy(self, edges):
        """Spans between pixel ``edges`` (sample positions) that contain fields.

        Used when the row is zoomed out; the work is bounded by the viewport width.
        """
        fields = self.fields
        edges = np.asarray(edges, dtype=np.float64)
        if not len(fields) or len(edges) < 2:
            return
        indices = np.searchsorted(fields["end"], edges[:-1], side="right")
        valid = indices < len(fields)
        active = np.zeros(len(indices), dtype=bool)
        active[valid] = fields["start"][indices[valid]] < edges[1:][valid]
        steps = np.diff(np.r_[False, active, False].astype(np.int8))
        for a, b in zip(np.flatnonzero(steps == 1), np.flatnonzero(steps == -1)):
            yield float(edges[a]), float(edges[b])

    def field_at(self, sample):
        fields = self.fields
        if not len(fields):
            return None
        index = int(np.searchsorted(fields["start"], sample, side="right")) - 1
        return index if index >= 0 and sample < int(fields["end"][index]) else None

    def label_at(self, sample):
        index = self.field_at(sample)
        return field_label(self.result, self.fields[index]) if index is not None else ""

    def _frame_last(self, index):
        fields = self.fields
        return index + 1 >= len(fields) or int(fields["frame"][index + 1]) != int(fields["frame"][index])

    def search(self, query, sample, direction, cancel=lambda: False, wrap=True):
        """Start of the next/previous field where ``query`` (a SpiQuery) matches, like SignalData.search."""
        key = (query, len(self.fields))
        if getattr(self, "_search_key", None) != key:
            self._search_hits = np.asarray(self.fields["start"])[query.field_matches(self.fields, cancel)]
            self._search_key = key
        starts = self._search_hits
        if not len(starts):
            return None
        if direction > 0:
            index = int(np.searchsorted(starts, sample, side="right"))
            if index < len(starts):
                return int(starts[index]), False
            return (int(starts[0]), True) if wrap else None
        index = int(np.searchsorted(starts, sample, side="left")) - 1
        if index >= 0:
            return int(starts[index]), False
        return (int(starts[-1]), True) if wrap else None

    def transition(self, sample, direction):
        """Next/previous field boundary, like a signal change.

        Steps go from byte start to byte start, so Shift+→ extends B one whole byte at a
        time even though boxes end where their last bit ends; the end of the last field of
        a transaction is a stop as well.
        """
        fields = self.fields
        if not len(fields):
            return None
        starts, ends = fields["start"], fields["end"]
        candidates = []
        if direction > 0:
            i = int(np.searchsorted(starts, sample, side="right"))
            if i < len(starts):
                candidates.append(int(starts[i]))
            j = int(np.searchsorted(ends, sample, side="right"))
            if j < len(ends) and self._frame_last(j):
                candidates.append(int(ends[j]))
            return min(candidates) if candidates else None
        i = int(np.searchsorted(starts, sample, side="left")) - 1
        if i >= 0:
            candidates.append(int(starts[i]))
        j = int(np.searchsorted(ends, sample, side="left")) - 1
        if j >= 0 and self._frame_last(j):
            candidates.append(int(ends[j]))
        return max(candidates) if candidates else None


_HEX = re.compile(r"(?:0x)?([0-9a-f_]+)", re.IGNORECASE)


@dataclass(frozen=True)
class SpiQuery:
    """What to find in decoded SPI fields.

    * ``9F`` or ``9F EF 40`` - consecutive bytes of one transaction, in any field
      (command, address bytes, data); ``001000`` is the same as ``00 10 00``;
    * ``data 01 02`` - only data bytes;
    * ``cmd 9F`` - command opcode; ``@1000`` or ``addr 1000`` - address,
      ``@1000-1FFF`` a range of addresses.

    Values are hexadecimal, like the decoded labels. X/Z and partial units never match.
    """

    kind: str                    # "bytes", "data", "cmd", "addr"
    values: tuple = ()           # bytes, or (low, high) for cmd/addr

    PREFIXES = (("cmd", "cmd"), ("addr", "addr"), ("@", "addr"), ("data", "data"))

    @classmethod
    def parse(cls, text):
        text = text.strip()
        kind, rest = "bytes", text
        for prefix, name in cls.PREFIXES:
            if text.casefold().startswith(prefix):
                kind, rest = name, text[len(prefix):].strip(" :=")
                break
        if not rest:
            raise ValueError("Введите значение в HEX: 9F, @1000, cmd 03 или 01 02 03")
        if kind in ("cmd", "addr"):
            parts = re.split(r"\s*(?:-|\.\.|…)\s*", rest)
            if len(parts) > 2:
                raise ValueError(f"Диапазон задаётся как @1000-1FFF, а не «{rest}»")
            low, high = (cls._number(part) for part in (parts * 2)[:2])
            return cls(kind, (min(low, high), max(low, high)))
        data = []
        for token in re.split(r"[\s,]+", rest):
            digits = cls._digits(token)
            digits = digits.rjust(len(digits) + len(digits) % 2, "0")
            data.extend(int(digits[i:i + 2], 16) for i in range(0, len(digits), 2))
        if len(data) > 256:
            raise ValueError("Не больше 256 байт в поиске")
        return cls(kind, tuple(data))

    @staticmethod
    def _digits(token):
        match = _HEX.fullmatch(token)
        if not match or not match[1].strip("_"):
            raise ValueError(f"«{token}» — не HEX-значение")
        return match[1].replace("_", "")

    @classmethod
    def _number(cls, token):
        return int(cls._digits(token), 16)

    def describe(self):
        if self.kind in ("cmd", "addr"):
            low, high = self.values
            text = f"{low:X}" if low == high else f"{low:X}–{high:X}"
            return ("команда " if self.kind == "cmd" else "адрес ") + text
        return ("данные " if self.kind == "data" else "") + " ".join(f"{b:02X}" for b in self.values)

    def lanes(self, result):
        return (0,) if self.kind in ("cmd", "addr") else tuple(range(result.lane_count))

    def field_matches(self, fields, cancel=lambda: False, chunk=1 << 20):
        """Indices of fields where a match starts, ascending and unique."""
        total = len(fields)
        found = []
        if self.kind in ("cmd", "addr"):
            low, high = self.values
            wanted = CMD if self.kind == "cmd" else ADDR
            for a in range(0, total, chunk):
                if cancel():
                    raise LoadCancelled()
                part = np.asarray(fields[a:a + chunk])
                good = (part["kind"] == wanted) & ((part["flags"] & (UNKNOWN | PARTIAL)) == 0)
                good &= (part["value"] >= low) & (part["value"] <= high)
                found.append(np.flatnonzero(good) + a)
            return np.concatenate(found) if found else np.empty(0, dtype=np.int64)
        sequence = np.asarray(self.values, dtype=np.int64)
        length = len(sequence)
        for a in range(0, total, chunk):
            if cancel():
                raise LoadCancelled()
            last = a + chunk >= total
            part = np.asarray(fields[a:a + chunk + length])  # overlap: every field holds >= 1 byte
            sizes = np.maximum(1, (part["bits"].astype(np.int64) + 7) // 8)
            sizes[part["kind"] == DUMMY] = 0
            if self.kind == "data":
                sizes[~np.isin(part["kind"], list(DATA_KINDS))] = 0
            owner = np.repeat(np.arange(len(part)), sizes)
            if len(owner) < length:
                continue
            first_byte = np.cumsum(sizes) - sizes
            position = np.arange(len(owner)) - first_byte[owner]
            shift = 8 * (sizes[owner] - 1 - position)
            values = (part["value"][owner].astype(np.int64) >> shift) & 0xFF
            values[(part["flags"][owner] & (UNKNOWN | PARTIAL)) != 0] = -1
            frames = part["frame"][owner]
            starts = len(owner) - length + 1
            hit = values[:starts] == sequence[0]
            for j in range(1, length):
                hit &= (values[j:j + starts] == sequence[j]) & (frames[j:j + starts] == frames[:starts])
            owners = owner[np.flatnonzero(hit)]
            if not last:
                owners = owners[owners < chunk]  # matches starting in the overlap belong to the next chunk
            found.append(np.unique(owners) + a)
        return np.concatenate(found) if found else np.empty(0, dtype=np.int64)

    def frames(self, result, cancel=lambda: False):
        """Frame indices that contain at least one match."""
        hits = [np.asarray(result.lanes[lane]["frame"])[self.field_matches(result.lanes[lane], cancel)]
                for lane in self.lanes(result) if lane < len(result.lanes)]
        return np.unique(np.concatenate(hits)).astype(np.int64) if hits else np.empty(0, dtype=np.int64)


def bubble_label(result, record, short=False):
    """Compact text for a field drawn as a filled box: ``0x45``."""
    kind, value, flags, bits = int(record["kind"]), int(record["value"]), int(record["flags"]), int(record["bits"])
    if kind == DUMMY:
        return f"dummy {value}" if not flags & PARTIAL else f"dummy {bits}/{value}"
    if kind == RESET:
        return "сброс" if short else f"сброс режима · {value} такт."
    digits = max(1, (bits + 3) // 4)
    if flags & UNKNOWN:
        text = "0x" + "X" * max(2, digits)
    elif flags & PARTIAL:
        text = f"0x{value:X} ({bits} бит)" if not short else f"{value:X}/{bits}b"
    else:
        text = f"0x{value:0{digits}X}"
    if kind == CMD and not flags and result.settings.mode == "commands":
        spec = result.settings.command(value)
        if short:
            return text if spec else text + " ?"
        return f"{spec.name} ({text})" if spec else f"{text} нет в таблице"
    if kind == ADDR:
        return ("@" if short else "адрес ") + text
    return text


class FieldOverlay:
    """Fields of one decoder lane drawn on top of the wire they came from.

    Only single-line fields of the given kinds are shown; the samples are not touched.
    """

    kind = "fields"

    def __init__(self, track, kinds):
        self.track = track
        self.kinds = np.array(sorted(kinds), dtype=np.uint8)

    @property
    def result(self):
        return self.track.result

    def _mask(self, fields):
        return np.isin(fields["kind"], self.kinds) & (fields["lines"] == 1)

    def window(self, left, right):
        return self.track.window(left, right)

    def visible(self, first, stop):
        fields = np.asarray(self.track.fields[first:stop])
        return fields[self._mask(fields)]

    def busy(self, edges):
        """Pixel spans with fields of this overlay; one probe per pixel, bounded by width."""
        fields = self.track.fields
        edges = np.asarray(edges, dtype=np.float64)
        if not len(fields) or len(edges) < 2:
            return
        indices = np.searchsorted(fields["end"], edges[:-1], side="right")
        valid = indices < len(fields)
        active = np.zeros(len(indices), dtype=bool)
        probe = np.asarray(fields[indices[valid]])
        active[valid] = (probe["start"] < edges[1:][valid]) & self._mask(probe)
        steps = np.diff(np.r_[False, active, False].astype(np.int8))
        for a, b in zip(np.flatnonzero(steps == 1), np.flatnonzero(steps == -1)):
            yield float(edges[a]), float(edges[b])


class ClockEdgeMarks:
    """Arrows on SCK at the edges the decoder samples, inside chip-select frames only."""

    kind = "edges"

    def __init__(self, result, sck, signal):
        self.result = result
        self.rising = result.settings.sample_edge == "rising"
        self.signal = signal
        self.levels = line_levels(signal, sck.offset)

    def visible(self, left, right, limit):
        """Sample-edge positions in [left, right), or None when there are more than ``limit``."""
        starts = self.signal.starts
        first = max(1, int(np.searchsorted(starts, left, side="left")))
        last = int(np.searchsorted(starts, right, side="left"))
        if last <= first:
            return np.empty(0, dtype=np.int64)
        if last - first > 2 * limit:
            return None
        states = self.levels[np.asarray(self.signal.codes[first - 1:last], dtype=np.int64)]
        before, after = states[:-1], states[1:]
        mask = (before == 0) & (after == 1) if self.rising else (before == 1) & (after == 0)
        positions = np.asarray(starts[first:last], dtype=np.int64)[mask]
        frames = self.result.frames
        if self.result.config.pins.cs is not None and len(positions):
            if not len(frames):
                return np.empty(0, dtype=np.int64)
            index = np.searchsorted(frames["start"], positions, side="right") - 1
            inside = index >= 0
            inside[inside] = positions[inside] < np.asarray(frames["end"])[index[inside]]
            positions = positions[inside]
        return positions if len(positions) <= limit else None


def overlays(result, capture, tracks):
    """(source signal name, annotation) pairs for 1-bit wires: bytes on MOSI/MISO, arrows on SCK.

    ``tracks`` are the DecodedTrack objects of the result's lanes. Lines that are bits
    of a bus are skipped: annotations attach to whole 1-bit signals.
    """
    pins = result.config.pins
    items = []

    def wire(line):
        signal = line.resolve(capture) if line is not None else None
        return signal if signal is not None and signal.width == 1 else None

    sck = wire(pins.sck)
    if sck is not None:
        items.append((sck.name, ClockEdgeMarks(result, pins.sck, sck)))
    io0, io1 = wire(pins.io[0]), wire(pins.io[1])
    if io0 is not None and tracks:
        items.append((io0.name, FieldOverlay(tracks[0], {CMD, ADDR, WRITE, MOSI})))
    if io1 is not None and tracks:
        items.append((io1.name, FieldOverlay(tracks[0], {READ})))
        if len(tracks) > 1:
            items.append((io1.name, FieldOverlay(tracks[1], {MISO})))
    return items


CONTENTS = {
    "data": "Данные (RD/WR/MOSI)",
    "read": "Только чтение (RD)",
    "write": "Только запись (WR)",
    "miso": "MISO (вторая линия полного дуплекса)",
    "all": "Все поля (CMD, ADDR, данные)",
}


@dataclass(frozen=True)
class SpiExportRequest:
    result: SpiDecodeResult
    frames: tuple | None = None   # frame indices, or None for the sample range
    start: int = 0                # inclusive
    end: int | None = None        # exclusive; None means the capture end
    content: str = "data"
    radix: str = "HEX"            # HEX, DEC, BIN
    separator: str = " "
    prefix: bool = False          # 0x / 0b before each value
    layout: str = "stream"        # "stream" or "frames" (one line per transaction)
    header: bool = True           # in frame layout: "#n CMD @ADDR:" before the bytes

    def lane_and_kinds(self):
        if self.content == "miso":
            return 1, frozenset({MISO})
        if self.content == "read":
            return 0, frozenset({READ})
        if self.content == "write":
            return 0, frozenset({WRITE})
        if self.content == "all":
            return 0, frozenset({CMD, ADDR, READ, WRITE, MOSI, DATA})
        return 0, frozenset({READ, WRITE, MOSI, DATA})


class SpiExportService:
    """Builds clipboard text or a binary file from decoded fields; cancellable."""

    MAX_CHARACTERS = 32_000_000
    CHUNK = 262144

    def _chunks(self, request, cancel):
        """(frame index, fields) in time order, filtered by content."""
        lane, kinds = request.lane_and_kinds()
        result = request.result
        kind_list = list(kinds)
        if request.frames is not None:
            for offset, index in enumerate(request.frames):
                if offset % 256 == 0 and cancel():
                    raise LoadCancelled()
                fields = result.frame_fields(int(index), lane)
                yield int(index), fields[np.isin(fields["kind"], kind_list)]
            return
        fields = result.lanes[lane]
        end = request.end if request.end is not None else result.capture_count
        first = int(np.searchsorted(fields["start"], request.start, side="left"))
        stop = int(np.searchsorted(fields["start"], end, side="left"))
        for a in range(first, stop, self.CHUNK):
            if cancel():
                raise LoadCancelled()
            chunk = np.asarray(fields[a:min(stop, a + self.CHUNK)])
            chunk = chunk[np.isin(chunk["kind"], kind_list)]
            if not len(chunk):
                continue
            # Split by frame so the frame layout can start a new line per transaction.
            frames = chunk["frame"]
            cuts = np.flatnonzero(np.diff(frames)) + 1
            for part in np.split(chunk, cuts):
                yield int(part["frame"][0]), part

    def _byte_table(self, request):
        prefix = {"HEX": "0x", "BIN": "0b"}.get(request.radix, "") if request.prefix else ""
        if request.radix == "DEC":
            return [str(value) for value in range(256)], "X"
        if request.radix == "BIN":
            return [prefix + format(value, "08b") for value in range(256)], prefix + "XXXXXXXX"
        return [prefix + format(value, "02X") for value in range(256)], prefix + "XX"

    def _texts(self, request, fields, table, unknown):
        simple = (fields["kind"] != CMD) & (fields["kind"] != ADDR) & (fields["flags"] == 0) & (fields["bits"] == 8)
        if simple.all():
            return [table[v] for v in fields["value"].astype(np.int64)]
        texts = []
        for record, plain in zip(fields, simple):
            if plain:
                texts.append(table[int(record["value"])])
                continue
            bits, value, flags = int(record["bits"]), int(record["value"]), int(record["flags"])
            if flags & UNKNOWN:
                texts.append(unknown)
            elif request.radix == "DEC":
                texts.append(str(value))
            elif request.radix == "BIN":
                texts.append(("0b" if request.prefix else "") + format(value, f"0{bits}b"))
            else:
                texts.append(("0x" if request.prefix else "") + format(value, f"0{max(1, (bits + 3) // 4)}X"))
        return texts

    def frame_header(self, result, index):
        frame = result.frames[index]
        parts = [f"#{index + 1}"]
        if int(frame["opcode"]) >= 0:
            parts.append(f"{int(frame['opcode']):02X}")
        if int(frame["address"]) >= 0:
            parts.append(f"@{int(frame['address']):0{2 * max(1, int(frame['address_bytes']))}X}")
        return " ".join(parts) + ":"

    def build(self, request, progress=lambda p, n: None, cancel=lambda: False):
        from .copying import CopyResult
        table, unknown = self._byte_table(request)
        parts = []
        size = 0
        count = 0
        current = None
        line_open = False
        for frame, fields in self._chunks(request, cancel):
            if not len(fields):
                continue
            texts = self._texts(request, fields, table, unknown)
            if request.layout == "frames":
                if frame != current:
                    if line_open:
                        parts.append("\n")
                        size += 1
                    if request.header:
                        header = self.frame_header(request.result, frame) + " "
                        parts.append(header)
                        size += len(header)
                    line_open = True
                    joined = request.separator.join(texts)
                else:
                    joined = request.separator + request.separator.join(texts)
                current = frame
            else:
                joined = (request.separator if count else "") + request.separator.join(texts)
            size += len(joined)
            if size > self.MAX_CHARACTERS:
                raise ValueError("Результат превышает 32 млн символов. Уменьшите диапазон или сохраните .bin.")
            parts.append(joined)
            count += len(texts)
            progress(0, count)
        if cancel():
            raise LoadCancelled()
        progress(100, count)
        return CopyResult("".join(parts), count)

    def write_binary(self, request, path, progress=lambda p, n: None, cancel=lambda: False):
        """Complete data bytes in order; returns the number of bytes written.

        An unknown (X/Z) bit has no binary representation, so the export stops at the
        first such byte and the destination keeps its previous content.
        """
        written = 0
        with atomic_write(Path(path), "wb") as stream:
            for frame, fields in self._chunks(replace(request, content="data" if request.content == "all"
                                                      else request.content), cancel):
                complete = fields[(fields["bits"] == 8) & ((fields["flags"] & PARTIAL) == 0)]
                unknown = np.flatnonzero(complete["flags"] & UNKNOWN)
                if len(unknown):
                    first = int(unknown[0])
                    raise ValueError(
                        f"Транзакция #{frame + 1}: байт {written + first} файла (отсчёт "
                        f"{int(complete['start'][first])}) содержит неизвестные биты X/Z. "
                        "Двоичный файл не записан; сохраните текст или сузьте диапазон.")
                stream.write(complete["value"].astype(np.uint8).tobytes())
                written += len(complete)
                progress(0, written)
            if cancel():
                raise LoadCancelled()
        progress(100, written)
        return written


def guess_pins(signals):
    """Prefill for the dialog only; the user confirms or changes every line."""
    options = line_options(signals)

    def find(patterns, exclude=()):
        for pattern in patterns:
            for option in options:
                name = option.text.casefold()
                if any(option == other for other in exclude):
                    continue
                if re.search(pattern, name):
                    return option
        return None

    sck = find([r"(^|_)s?sclk($|_)", r"(^|_)sck($|_)", r"spi.*cl(oc)?k", r"(^|_)clk($|_)"])
    cs = find([r"(^|_)(cs|ss|ce|sel)(_?n|_b|#)?($|_)", r"cs_?n", r"ss_?n", r"(^|_)n?cs"], (sck,))
    io = []
    for index, extra in enumerate(([r"mosi", r"(^|_)si($|_)", r"(^|_)sdi($|_)"],
                                   [r"miso", r"(^|_)so($|_)", r"(^|_)sdo($|_)"],
                                   [r"(^|_)wp(_?n|#)?$"], [r"(^|_)hold(_?n|#)?$", r"(^|_)reset(_?n|#)?$"])):
        patterns = [rf"(^|_)(dq|io|sio|data|d)_?\[{index}\]$", rf"(^|_)(dq|io|sio)_?{index}$", *extra]
        io.append(find(patterns, (sck, cs, *io)))
    if not any(io):
        buses = data_buses(signals)
        if buses:
            io = list(bus_lines(buses[0]))
    return SpiPins(sck, cs, True, tuple(io)) if sck else None


DATA_BUS_NAME = re.compile(r"(^|_)(dq|io|sio|qspi|spi|data|dat|d)(_?(in|out|i|o|io))?($|_)")


def data_buses(signals):
    """2- and 4-bit buses that can carry IO0…IO3, likely data-line names first.

    Only a prefill for the dialog: with no 1-bit IO wires, a bus such as ``dq_in[3:0]``
    gives IO0…IO3 from its bits; the user still sees and can change every line.
    """
    def stem(signal):
        return re.sub(r"\[\d+\s*:\s*\d+\]$", "", signal.short_name).casefold()
    buses = [signal for signal in signals if signal.width in (2, 4)]
    named = [signal for signal in buses if DATA_BUS_NAME.search(stem(signal))]
    return named or (buses if len(buses) == 1 else [])


def bus_lines(signal):
    """IO0…IO3 from the physical bits of a 2/4-bit bus (bit offset 0 is IO0)."""
    labels = dict(zip(range(signal.width - 1, -1, -1), BitCodec.labels(signal)))
    stem = re.sub(r"\[\d+\s*:\s*\d+\]$", "", signal.short_name)
    lines = [LineRef(signal.name, offset, f"{stem}[{labels[offset]}]") for offset in range(signal.width)]
    return tuple(lines + [None] * (4 - len(lines)))


def rebind(config, capture):
    """The same configuration for a refreshed capture, or None if a line disappeared."""
    if config is None or config.pins.missing(capture):
        return None
    return config


def replace_settings(config, **changes):
    return replace(config, settings=replace(config.settings, **changes))


# Remembered dialog choices ------------------------------------------------------
# The decoder config of a capture is capture state. Only the last choices made in the
# dialog are kept as a general preference, to prefill the dialog next time; lines are
# kept by signal name and reused only when every one of them exists in the capture.

def _line_dict(line):
    return None if line is None else {"signal": line.signal, "offset": line.offset}


def _line_from(data, options):
    if data is None:
        return None
    key = LineRef(str(data["signal"]), int(data["offset"]))
    match = next((option for option in options if option == key), None)
    if match is None:
        raise KeyError(key.signal)
    return match


def config_to_dict(config):
    s, pins = config.settings, config.pins
    settings = {"mode": s.mode, "sample_edge": s.sample_edge, "before_edge": s.before_edge,
                "msb_first": s.msb_first, "protocol": s.protocol, "address_bytes": s.address_bytes,
                "dummy_cycles": s.dummy_cycles, "dtr": s.dtr, "raw_lines": s.raw_lines,
                "reset_frames": s.reset_frames,
                "fixed": {"widths": s.fixed.widths.text, "address_bytes": s.fixed.address_bytes,
                          "dummy": s.fixed.dummy, "direction": s.fixed.direction, "dtr": s.fixed.dtr}}
    if s.commands != default_commands():
        settings["commands"] = [[c.opcode, c.name, list(c.formats), c.address_bytes, list(c.dummy),
                                 c.direction, c.dtr, c.configurable_dummy] for c in s.commands]
    return {"settings": settings,
            "pins": {"sck": _line_dict(pins.sck), "cs": _line_dict(pins.cs), "cs_active_low": pins.cs_active_low,
                     "io": [_line_dict(line) for line in pins.io]}}


def settings_from_dict(data):
    """SpiSettings from remembered choices; defaults for anything missing or invalid."""
    try:
        data = dict(data["settings"])
        fixed = dict(data.pop("fixed"))
        commands = data.pop("commands", None)
        fixed = FixedFormat(BusWidths.parse(fixed["widths"]), int(fixed["address_bytes"]), int(fixed["dummy"]),
                            str(fixed["direction"]), str(fixed["dtr"]))
        known = {"mode", "sample_edge", "before_edge", "msb_first", "protocol", "address_bytes",
                 "dummy_cycles", "dtr", "raw_lines", "reset_frames"}
        kwargs = {key: value for key, value in data.items() if key in known}
        if commands is not None:
            kwargs["commands"] = tuple(CommandSpec(int(c[0]), str(c[1]), tuple(map(str, c[2])), int(c[3]),
                                                   tuple(map(int, c[4])), str(c[5]), bool(c[6]), bool(c[7]))
                                       for c in commands)
        return SpiSettings(fixed=fixed, **kwargs)
    except (KeyError, TypeError, ValueError, IndexError):
        return SpiSettings()


def pins_from_dict(data, signals):
    """Remembered lines when all of them exist among ``signals``, else None."""
    try:
        pins = data["pins"]
        options = line_options(signals)
        sck = _line_from(pins["sck"], options)
        if sck is None:
            return None
        io = tuple(_line_from(line, options) for line in pins["io"])
        if len(io) != 4:
            return None
        return SpiPins(sck, _line_from(pins["cs"], options), bool(pins["cs_active_low"]), io)
    except (KeyError, TypeError, ValueError):
        return None
