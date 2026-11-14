# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Streaming VCD importer for simulator captures, with disk-backed transitions."""

from array import array
import concurrent.futures
from pathlib import Path
import os
import re
import tempfile
import time

import numpy as np

from . import diskspace, vcd_chunks
from .csv_chunks import ValueTable
from .data import Capture, LoadCancelled, SignalData, _Writer, _run
from .tempstore import storage_or_default


def abandon(*holders):
    """Close the files of writers or timelines on an error path, never raising.

    Closing flushes buffered data; on a full disk that flush fails again, and the
    error must not stop the cleanup that removes the capture folder.
    """
    for holder in holders:
        for name in ("sf", "cf"):
            stream = getattr(holder, name, None)
            if stream is not None and not stream.closed:
                try:
                    stream.close()  # the file is closed even when its final flush fails
                except OSError:
                    pass
        if isinstance(holder, RawTimeline):
            holder.closed = True
            holder.release()


class RawTimeline:
    """Change rows of one split-bus member bit, kept as the raw VCD text of each change.

    A bus value is the concatenation of its members' raw texts, so members are recorded
    as text (``"1"`` and ``"01"`` are different texts with the same value). Only a
    repeat of the same text is dropped, exactly as ``_Writer`` drops it. The member's own
    value runs and the bus are built from this timeline after the parse.
    """

    SPILL = 8192  # entries kept in memory (96 KB, like a _Writer buffer) before moving to disk

    def __init__(self, folder, index):
        self.start_path = folder / f"{index}.raw_starts"
        self.code_path = folder / f"{index}.raw_codes"
        self.sf = self.cf = None  # opened on the first spill; most bits change rarely
        self.closed = False
        self.on_disk = False
        self.starts = array("q")
        self.codes = array("I")
        self.texts = []
        self.ids = {}
        self.last_raw = None

    def append(self, row, raw):
        if raw == self.last_raw:
            return
        self.last_raw = raw
        code = self.ids.get(raw)
        if code is None:
            code = self.ids[raw] = len(self.texts)
            self.texts.append(raw)
        self.starts.append(row)
        self.codes.append(code)
        if len(self.starts) >= self.SPILL:
            self.spill()

    @classmethod
    def placed(cls, folder, index, texts):
        """A closed timeline whose files a parallel parse wrote (``vcd_chunks.place_signals``)."""
        timeline = cls(folder, index)
        timeline.texts = texts
        timeline.ids = None
        timeline.closed = timeline.on_disk = True
        return timeline

    def spill(self):
        if self.sf is None:
            self.sf = self.start_path.open("wb")
            self.cf = self.code_path.open("wb")
            self.on_disk = True
        self.starts.tofile(self.sf)
        self.codes.tofile(self.cf)
        self.starts = array("q")
        self.codes = array("I")

    def close(self):
        """Finish writing; a timeline that ever spilled ends up entirely on disk."""
        if self.closed:
            return
        self.closed = True
        if self.sf is not None:
            self.spill()
            self.sf.close()
            self.cf.close()

    def open(self):
        """Prepare a closed timeline for reading; returns its start rows for binary search.

        A spilled timeline is searched through a memory map, which touches only the
        pages a bisection visits; blocks are read with ``read`` into private memory, so
        the pages of a whole timeline never stay resident.
        """
        if not self.on_disk:
            self.memory = (np.frombuffer(self.starts, dtype=np.int64) if self.starts else np.zeros(0, np.int64),
                           np.frombuffer(self.codes, dtype=np.uint32) if self.codes else np.zeros(0, np.uint32))
            self.index = self.memory[0]
        else:
            self.memory = None
            self.index = np.memmap(self.start_path, dtype=np.int64, mode="r")
        return self.index

    def read(self, begin, end):
        """(starts, codes) of entries ``[begin, end)``."""
        if self.memory is not None:
            return self.memory[0][begin:end], self.memory[1][begin:end]
        # fromfile allocates ``count`` entries up front, so never ask past the end.
        count = max(0, min(end, len(self.index)) - begin)
        return (np.fromfile(self.start_path, dtype=np.int64, count=count, offset=begin * 8),
                np.fromfile(self.code_path, dtype=np.uint32, count=count, offset=begin * 4))

    def release(self):
        index = getattr(self, "index", None)
        if isinstance(index, np.memmap):
            index._mmap.close()
        self.index = self.memory = None


class SplitBusAssembler:
    """Rebuild contiguous scalar VCD declarations as a presentation-ready bus.

    The parse only records each member bit's raw changes (``RawTimeline``). ``build``
    then writes the members' value runs and each bus in bounded row blocks. A bus changes
    exactly at the union of its members' change rows, and its raw text there is the
    members' texts joined MSB first, as the per-timestamp assembly produced it
    (``tests/reference.py`` keeps that version as the oracle).
    """

    BIT_NAME = re.compile(r"^(.*)\[(\d+)\]$")
    BLOCK_CELLS = 1 << 21  # union rows x member bits per block: tens of MB of arrays at most

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
        self.member_indices = sorted({index for members in self.members.values() for index in members})

    @classmethod
    def one_bus(cls, parent, members, block_cells=None):
        """An assembler for one already-planned bus (``build_bus`` in a worker process)."""
        assembler = cls.__new__(cls)
        assembler.members = {parent: list(members)}
        assembler.children = {}
        assembler.member_indices = sorted(members)
        if block_cells:
            assembler.BLOCK_CELLS = block_cells
        return assembler

    def timelines(self, folder):
        """{member signal index: RawTimeline} for the parse loop."""
        return {index: RawTimeline(folder, index) for index in self.member_indices}

    def build(self, writers, timelines, cancel=lambda: False):
        """Write member value runs and buses from the members' timelines; cancellable."""
        try:
            for timeline in timelines.values():
                timeline.close()
                timeline.open()
            for index, timeline in timelines.items():
                self._member(writers[index], timeline, cancel)
            for parent, members in self.members.items():
                self._bus(writers[parent], [timelines[i] for i in members], cancel)
        finally:
            for timeline in timelines.values():
                timeline.release()
            for timeline in timelines.values():
                timeline.start_path.unlink(missing_ok=True)
                timeline.code_path.unlink(missing_ok=True)

    @staticmethod
    def _write(writer, starts, codes):
        writer.flush()
        np.ascontiguousarray(starts, dtype=np.int64).tofile(writer.sf)
        np.ascontiguousarray(codes, dtype=np.uint32).tofile(writer.cf)

    @staticmethod
    def _changes(values, previous):
        """Rows whose value code differs from the row before (``previous`` before the block)."""
        keep = np.empty(len(values), dtype=bool)
        keep[0] = values[0] != previous
        np.not_equal(values[1:], values[:-1], out=keep[1:])
        return keep

    def _member(self, writer, timeline, cancel):
        # Texts are learned in order of first appearance, as _Writer learns them.
        texts = timeline.texts
        table = np.fromiter((writer._learn(text) for text in texts), dtype=np.int64, count=len(texts))
        previous = -1
        step = self.BLOCK_CELLS
        for first in range(0, len(timeline.index), step):
            if cancel():
                raise LoadCancelled()
            starts, codes = timeline.read(first, first + step)
            values = table[codes]
            keep = self._changes(values, previous)
            self._write(writer, starts[keep], values[keep])
            previous = int(values[-1])

    def _bus(self, writer, members, cancel):
        width = len(members)
        single = all(len(text) == 1 for member in members for text in member.texts)
        alphabets = [np.frombuffer("".join(member.texts).encode("latin-1", "replace"), dtype=np.uint8)
                     for member in members] if single else None
        last_row = max(int(member.index[-1]) for member in members)
        rows = max(1, self.BLOCK_CELLS // width)
        cache = {}
        previous = -1
        for low in range(0, last_row + 1, rows):
            if cancel():
                raise LoadCancelled()
            blocks = []
            for member in members:
                begin = int(np.searchsorted(member.index, low, "left"))
                end = int(np.searchsorted(member.index, low + rows, "left"))
                # One entry before the block gives the member's state at the block start.
                before = max(0, begin - 1)
                starts, codes = member.read(before, end)
                blocks.append((starts, codes, begin - before))
            union = np.unique(np.concatenate([starts[skip:] for starts, _, skip in blocks]))
            if not len(union):
                continue
            # Every timeline starts at row 0, so each union row has a state in every member.
            states = [codes[np.searchsorted(starts, union, "right") - 1] for starts, codes, _ in blocks]
            if single:
                matrix = np.empty((len(union), width), dtype=np.uint8)
                for column, (alphabet, state) in enumerate(zip(alphabets, states)):
                    matrix[:, column] = alphabet[state]
                values = self._value_codes(writer, matrix, cache)
            else:
                # A member wrote a multi-character text (``b01`` for one bit): join per row.
                values = np.empty(len(union), dtype=np.int64)
                columns = [[member.texts[code] for code in state.tolist()] for member, state in zip(members, states)]
                for row, parts in enumerate(zip(*columns)):
                    text = "".join(parts)
                    code = cache.get(text)
                    if code is None:
                        code = cache[text] = writer._learn(text)
                    values[row] = code
            keep = self._changes(values, previous)
            self._write(writer, union[keep], values[keep])
            previous = int(values[-1])

    @staticmethod
    def _value_codes(writer, matrix, cache):
        """Value code of each row of joined one-character texts (rows x bits, ASCII).

        Rows of only 0/1 are binary numbers, computed with numpy; the others (X, Z, ?)
        go through ``_Writer._learn`` like any text. New values are registered in order
        of first appearance, as appending the joined texts one by one would.
        """
        sig = writer.signal
        rows, width = matrix.shape
        binary = (((matrix == 48) | (matrix == 49)).all(axis=1) if width <= 63
                  else np.zeros(rows, dtype=bool))
        groups = []
        numeric = np.flatnonzero(binary)
        if len(numeric):
            bits = matrix[numeric]
            number = np.zeros(len(numeric), dtype=np.uint64)
            for column in range(width):
                number = (number << np.uint64(1)) | (bits[:, column] == 49)
            distinct, first, inverse = np.unique(number, return_index=True, return_inverse=True)
            groups.append((numeric, distinct.tolist(), numeric[first], inverse.reshape(-1), True))
        other = np.flatnonzero(~binary)
        if len(other):
            keys = np.ascontiguousarray(matrix[other]).view(np.dtype((np.void, width))).ravel()
            distinct, first, inverse = np.unique(keys, return_index=True, return_inverse=True)
            groups.append((other, [key.tobytes().decode("latin-1") for key in distinct],
                           other[first], inverse.reshape(-1), False))
        tables = [np.empty(len(group[1]), dtype=np.int64) for group in groups]
        # Learn every distinct value in order of its first row across both groups.
        firsts = np.concatenate([group[2] for group in groups])
        owners = np.concatenate([np.full(len(group[1]), number) for number, group in enumerate(groups)])
        slots = np.concatenate([np.arange(len(group[1])) for group in groups])
        order = np.argsort(firsts, kind="stable")
        ids, values = sig.value_ids, sig.values
        for owner, slot in zip(owners[order].tolist(), slots[order].tolist()):
            _, distinct, _, _, is_number = groups[owner]
            key = distinct[slot]
            if is_number:
                # What _learn does for a binary text that fits the bus's explicit width.
                code = ids.get(key)
                if code is None:
                    code = ids[key] = len(values)
                    values.append(key)
            else:
                code = cache.get(key)
                if code is None:
                    code = cache[key] = writer._learn(key)
            tables[owner][slot] = code
        result = np.empty(rows, dtype=np.int64)
        for (where, _, _, inverse, _), table in zip(groups, tables):
            result[where] = table[inverse]
        return result


def build_bus(folder, parent, members, cancel_path=None, block_cells=None):
    """Worker entry point: one split bus and its member bits from timelines on disk.

    ``parent`` and each of ``members`` are (signal index, width) plus, for members, the
    timeline's texts. Writes the final index files of all of them with the task 5 code
    (``SplitBusAssembler.build``) and returns {signal index: values}.
    """
    folder = Path(folder)
    parent_index, parent_width = parent
    signals = {parent_index: SignalData("", "BINARY", parent_width, True)}
    timelines, writers = {}, {}
    for index, width, texts in members:
        signals[index] = SignalData("", "BINARY", width, True)
        timelines[index] = RawTimeline.placed(folder, index, texts)
    try:
        for index, signal in signals.items():
            writers[index] = _Writer(folder, index, signal)
        assembler = SplitBusAssembler.one_bus(parent_index, [index for index, _, _ in members], block_cells)
        assembler.build(writers, timelines, lambda: bool(cancel_path) and os.path.exists(cancel_path))
        for writer in writers.values():
            writer.close()
    except BaseException:
        abandon(*writers.values())
        raise
    finally:
        for timeline in timelines.values():
            timeline.release()
    return {index: signal.values for index, signal in signals.items()}


def map_files(files, threads=16):
    """Read-only memory maps of freshly written (path, dtype) files, opened from several threads.

    The first open of a just-written file waits (about 3 ms here, most likely the
    on-access scan); the waits overlap across threads, 1.4 s -> 0.13 s for 488 files.
    On an error every map already opened is closed again.
    """
    with concurrent.futures.ThreadPoolExecutor(max(1, min(threads, len(files)))) as executor:
        futures = [executor.submit(np.memmap, path, dtype=dtype, mode="r") for path, dtype in files]
    errors = [future.exception() for future in futures if future.exception() is not None]
    if errors:
        for future in futures:
            if future.exception() is None:
                future.result()._mmap.close()
        raise errors[0]
    return [future.result() for future in futures]


MIN_PARALLEL_CHUNK = 16 << 20  # bytes of VCD per worker; smaller files stay on one process
RANGES_PER_WORKER = 4  # several ranges per worker balance uneven ranges and give finer progress


class ParallelVcdLoad:
    """Parse the VCD body in byte ranges on a ``WorkerPool`` and merge them in order.

    The result equals the single-process parse array for array. ``load`` returns None
    when the file cannot be split safely (a ``$comment`` or other directive in the
    body, a repeated timestamp at a range seam, a non-ASCII header); the caller then
    parses on one process. Errors raise the single-process message (see
    ``vcd_chunks.RangeParser``).
    """

    def __init__(self, pool, disk=None, temp=None):
        self.pool = pool
        self.disk = disk  # a DiskSpace probe; None skips the space check before placement
        self.temp = storage_or_default(temp)
        self.timings = {}  # seconds per phase of the last load, for benchmarks

    def _phase(self, name, began):
        now = time.perf_counter()
        self.timings[name] = round(now - began, 3)
        return now

    def load(self, path, progress, cancel, source_path=None):
        began = time.perf_counter()
        size = path.stat().st_size
        workers = self.pool.workers_for(size, MIN_PARALLEL_CHUNK)
        if workers <= 1:
            return None
        body = vcd_chunks.header_end(path)
        if body is None:
            return None
        storage = self.temp.create("ila_vcd_")
        folder = Path(storage.name)
        timelines, mapped = {}, []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                signals, by_id, timescale = VcdCaptureReader._header(VcdCaptureReader._tokens(stream, cancel))
            if not signals:
                storage.cleanup()
                return None
            declared = len(signals)
            buses = SplitBusAssembler(signals)
            members = set(buses.member_indices)
            widths = [signal.width for signal in signals[:declared]]
            undumped = [(index, "x" * width) for index, width in enumerate(widths)]
            starts = vcd_chunks.range_starts(path, body, size, workers * RANGES_PER_WORKER)
            ranges = list(zip(starts, [*starts[1:], size]))
            if len(ranges) < 2:
                storage.cleanup()
                return None
            flag = folder / "cancel.flag"
            parts = [str(folder / f"part{k}.bin") for k in range(len(ranges))]
            futures = [self.pool.submit(vcd_chunks.parse_range, str(path), a, b,
                                        (widths, by_id, members, None if k else undumped), parts[k], str(flag),
                                        vcd_chunks.SEGMENT)
                       for k, (a, b) in enumerate(ranges)]

            def parsed(done, total):
                rows = sum(f.result().get("rows", 0) for f in futures if f.done() and not f.cancelled()
                           and f.exception() is None)
                progress(min(90, 90 * done // total), rows)

            began = self._phase("split", began)
            results = _run(futures, flag, cancel, parsed)
            began = self._phase("parse", began)
            self.timings["ranges"] = len(ranges)
            spent = sorted(result.get("seconds", 0) for result in results)
            self.timings["range_seconds"] = [round(spent[0], 2), round(spent[len(spent) // 2], 2),
                                             round(spent[-1], 2), round(sum(spent), 1)]
            self.timings["range_cpu_seconds"] = round(sum(result.get("cpu_seconds", 0) for result in results), 1)
            offsets = self._check(results)
            if offsets is None:
                storage.cleanup()
                return None
            count = offsets[-1]
            jobs = self._plan(signals, declared, members, results, offsets, folder, timelines, cancel)
            groups = self._balance(jobs, self.pool.workers)
            # Placement writes every run once more while the part files still exist, and the
            # buses then write member and bus runs: at most twice the member timelines.
            members_bytes = sum(weight for (path, _, _), weight in jobs if path.suffix == ".raw_starts") * 12
            diskspace.require(self.disk, folder.parent,
                              sum(weight for _, weight in jobs) * 12 + 2 * members_bytes)
            began = self._phase("plan", began)
            _run([self.pool.submit(vcd_chunks.place_signals, parts, group, str(flag)) for group in groups],
                 flag, cancel)
            for part in parts:
                Path(part).unlink(missing_ok=True)
            progress(95, count)
            began = self._phase("place", began)
            # Buses are independent: each one, with its member bits, is built in a worker.
            built = _run([self.pool.submit(build_bus, str(folder), (parent, signals[parent].width),
                                           [(i, signals[i].width, timelines[i].texts) for i in bits], str(flag),
                                           SplitBusAssembler.BLOCK_CELLS)
                          for parent, bits in buses.members.items()], flag, cancel)
            for values in built:
                for index, table in values.items():
                    signals[index].values = table
                    signals[index].value_ids = {value: code for code, value in enumerate(table)}
            began = self._phase("buses", began)
            files = [(folder / f"{index}{suffix}", dtype) for index in range(len(signals))
                     for suffix, dtype in ((".starts", np.int64), (".codes", np.uint32))]
            mapped.extend(map_files([*files, (folder / "timestamps.i64", np.int64)]))
            times = mapped[-1]
            for index, signal in enumerate(signals):
                signal.starts, signal.codes = mapped[2 * index], mapped[2 * index + 1]
            progress(100, count)
            capture = Capture(Path(source_path or path).resolve(), signals, count, {}, storage,
                              times=times, timescale=timescale, split_buses=buses.children)
            self._phase("finish", began)
            return capture
        except BaseException:
            for data in mapped:
                data._mmap.close()
            storage.cleanup()
            raise

    @staticmethod
    def _check(results):
        """Row offset of every range plus the total; None when the split was unsafe.

        Ranges are checked in file order, so the first problem is the one the
        single-process parse meets first. A range's own error raised before its first
        timestamp ended precedes the seam check; the seam check precedes later errors.
        """
        offsets = [0]
        last = None
        for k, result in enumerate(results):
            if result.get("unsafe"):
                return None
            if "error" in result and result["early"]:
                raise ValueError(result["error"])
            if k:
                first = result.get("first_time")
                if first is None or first == last:
                    return None  # equal timestamps at a seam continue one row
                if first < last:
                    raise ValueError("В VCD временные отметки идут не по возрастанию.")
            if "error" in result:
                raise ValueError(result["error"])
            if not result["rows"]:
                return None  # only possible for the first range; it owns the all-X first row
            last = result["last_time"]
            offsets.append(offsets[-1] + result["rows"])
        return offsets

    @staticmethod
    def _plan(signals, declared, members, results, offsets, folder, timelines, cancel):
        """Global value tables and the placement entries of every signal and of the timestamps.

        Local codes map to global ones in first-appearance order; a range whose first run
        repeats the value (or, for a bus member, the text) the signal had at the end of the
        previous ranges drops that run, as one writer would have.
        """
        jobs = [((folder / "timestamps.i64", None,
                  [(k, result["segments"].get(vcd_chunks.TIMES, []), None, 0, 0)
                   for k, result in enumerate(results)]), offsets[-1])]
        for index in range(declared):
            table = ValueTable()
            last = None
            entries, runs = [], 0
            for k, result in enumerate(results):
                if cancel():
                    raise LoadCancelled()
                count, local, first, final = result["columns"][index]
                if not count:
                    continue
                lut = table.lut_for(("list", local), cancel)
                drop = int(last is not None and int(lut[first]) == last)
                last = int(lut[final])
                entries.append((k, result["segments"][index], lut, offsets[k], drop))
                runs += count - drop
            if index in members:
                timelines[index] = RawTimeline.placed(folder, index, table.values)
                paths = (timelines[index].start_path, timelines[index].code_path)
            else:
                signals[index].values, signals[index].value_ids = table.values, table.ids
                paths = (folder / f"{index}.starts", folder / f"{index}.codes")
            jobs.append(((*paths, entries), runs))
        return jobs

    @staticmethod
    def _balance(jobs, workers):
        """Split placement jobs over ``workers`` groups of similar size, largest first."""
        groups = [[] for _ in range(max(1, min(workers, len(jobs))))]
        loads = [0] * len(groups)
        for job, weight in sorted(jobs, key=lambda item: -item[1]):
            lightest = loads.index(min(loads))
            groups[lightest].append(job)
            loads[lightest] += weight + 1
        return [group for group in groups if group]


class VcdCaptureReader:
    """Map each distinct VCD timestamp to one event row without expanding idle time.

    With a ``WorkerPool`` a large file is parsed in parallel (``ParallelVcdLoad``);
    files that cannot be split safely, and small ones, are parsed on this process.
    """

    def __init__(self, pool=None, disk=None, temp=None):
        self.pool = pool
        self.disk = disk if disk is not None else diskspace.DiskSpace()
        self.temp = storage_or_default(temp)  # creates the index folders (tempstore.TempStorage)
        self.timings = {}

    def load(self, location, progress=lambda percent, rows: None,
             cancel=lambda: False, source_path=None):
        """Load a VCD; refuses early, or fails cleanly, when the index files do not fit on disk."""
        path = Path(location).resolve()
        folder = self.temp.root
        needed = diskspace.index_estimate(path.stat().st_size)
        diskspace.require(self.disk, folder, needed)
        try:
            return self._load(path, progress, cancel, source_path)
        except diskspace.DiskSpaceError:
            raise
        except OSError as exc:
            # Every temporary file is already removed by the cleanup paths below.
            if diskspace.is_disk_full(exc):
                raise diskspace.DiskSpaceError(folder, needed, self.disk.free(folder)) from exc
            raise

    def _load(self, location, progress, cancel, source_path):
        path = Path(location).resolve()
        if self.pool is not None:
            parallel = ParallelVcdLoad(self.pool, self.disk, self.temp)
            self.timings = parallel.timings
            capture = parallel.load(path, progress, cancel, source_path)
            if capture is not None:
                return capture
        storage = self.temp.create("ila_vcd_")
        writers = []
        timelines = {}
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
                timelines = buses.timelines(folder)
                # Member bits record raw texts; buses are built from them after the parse.
                sinks = [timelines.get(index, writer) for index, writer in enumerate(writers)]
                undumped = [(index, "x" * signal.width) for index, signal in enumerate(signals)
                            if index not in buses.members]
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
                        if count:
                            for index, value in changes.items():
                                sinks[index].append(count, value)
                        else:
                            # An undumped signal is unknown in every bit, like a later explicit bx.
                            for index, unknown in undumped:
                                sinks[index].append(0, changes.get(index, unknown))
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
                    # One target per identifier is the common case; padding needs width > 1.
                    single = {symbol: (indices[0], widths[indices[0]])
                              for symbol, indices in by_id.items() if len(indices) == 1}
                    shared = {symbol: indices for symbol, indices in by_id.items() if len(indices) > 1}
                    target = single.get
                    lookup = shared.get
                    scalar = {"0": "0", "1": "1", "x": "x", "X": "x", "z": "z", "Z": "z"}.get
                    for token in tokens:
                        first = token[0]
                        if first == "b" or first == "B":
                            # Vector form: most changes in simulator dumps, 1-bit regs included.
                            symbol = next(tokens, None)
                            if symbol is None:
                                raise ValueError(f"Незавершённое изменение VCD: {token[:80]}")
                            value = token[1:]
                            if not value.isdecimal():
                                value = value.lower()  # only X/Z letters change
                        elif (value := scalar(first)) is not None:
                            symbol = token[1:]
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
                        elif first in "rRsS":
                            if next(tokens, None) is None:
                                raise ValueError(f"Незавершённое изменение VCD: {token[:80]}")
                            continue  # real/string declarations are deliberately unsupported
                        else:
                            raise ValueError(f"Некорректное изменение VCD: {token[:80]}")
                        if not symbol or not value:
                            raise ValueError(f"Некорректное изменение VCD: {token[:80]}")
                        started = True
                        plan = target(symbol)
                        if plan is not None:
                            index, width = plan
                            if width > 1 and len(value) < width and value[0] in "xz":
                                changes[index] = value.rjust(width, value[0])
                            else:
                                changes[index] = value
                            continue
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
                buses.build(writers, timelines, cancel)
                for writer in writers:
                    writer.close()
                mapped.extend(map_files([*((path, dtype) for writer in writers for path, dtype in
                                           ((writer.start_path, np.int64), (writer.code_path, np.uint32))),
                                         (time_path, np.int64)]))
                times = mapped[-1]
                for index, writer in enumerate(writers):
                    writer.signal.starts, writer.signal.codes = mapped[2 * index], mapped[2 * index + 1]
                progress(100, count)
                return Capture(Path(source_path or path).resolve(), signals, count, {}, storage,
                               times=times, timescale=timescale, split_buses=buses.children)
        except BaseException:
            abandon(*writers, *timelines.values())
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
