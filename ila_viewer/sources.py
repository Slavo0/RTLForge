# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Data-source boundary: a future simulator can implement CaptureSource."""
from abc import ABC, abstractmethod
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from .data import load_csv
from .data import LoadCancelled
from .diskspace import CONVERSION_RESERVE, DiskSpace, DiskSpaceError, index_estimate, is_disk_full
from .progress import CONVERTING_WLF, RECONVERTING_WLF
from .files import same_file
from .tempstore import storage_or_default
from .vcd import VcdCaptureReader
from .wlf_slices import SlicedWlfConversion, find_wlfman


class CaptureSource(ABC):
    @abstractmethod
    def load(self, location, progress, cancel):
        """Return an owned Capture. UI must not depend on its origin."""


class VivadoCsvSource(CaptureSource):
    def __init__(self, pool=None, temp=None):
        self.pool = pool  # a WorkerPool; large files are split over its processes
        self.temp = temp  # a tempstore.TempStorage; None: the system temporary folder

    def load(self, location, progress=lambda p, n: None, cancel=lambda: False):
        return load_csv(location, progress, cancel, pool=self.pool, temp=self.temp)


class VcdCaptureSource(CaptureSource):
    def __init__(self, reader=None, pool=None):
        self.reader = reader or VcdCaptureReader(pool)  # a WorkerPool parses large files in parallel

    def load(self, location, progress=lambda p, n: None, cancel=lambda: False):
        return self.reader.load(location, progress, cancel)


class QuestaWlfConverter:
    """Use the vendor utility instead of guessing the proprietary WLF layout."""

    def __init__(self, executable=None, recovery_executable=None, runner=None, slices=1):
        self.executable = executable
        self.recovery_executable = recovery_executable
        self.runner = runner or self._run_command
        # Above 1: convert that many time slices in parallel (SlicedWlfConversion), falling
        # back to one wlf2vcd run whenever the slices cannot be joined safely.
        self.slices = slices

    def convert(self, source, target, cancel=lambda: False, sliced=True):
        """Write ``target``; returns the ``SeamCheck`` of a sliced conversion, else None.

        A caller that gets a check must verify the loaded capture with it and, on any
        mismatch, convert again with ``sliced=False`` (``WlfCaptureSource`` does).
        """
        executable = self.executable or os.environ.get("WLF2VCD") or shutil.which("wlf2vcd")
        if not executable:
            raise RuntimeError("Для открытия WLF нужен wlf2vcd из Questa/ModelSim. "
                               "Выберите его в Настройки → Источники либо добавьте в PATH. "
                               "Можно также открыть экспортированный VCD.")
        source, target = Path(source), Path(target)
        wlfman = find_wlfman(executable) if sliced and self.slices > 1 else None
        if wlfman:
            check = SlicedWlfConversion(executable, wlfman, self.runner, self.slices).convert(source, target, cancel)
            if check is not None:
                return check
        code, detail = self.runner([str(executable), str(source), "-o", str(target)], cancel)
        if code == 7 and "Unable to open WLF file" in detail:
            recovery = self.recovery_executable or self._recovery_tool(executable)
            if recovery:
                with tempfile.NamedTemporaryFile(prefix="wlf-recovery-", suffix=".wlf",
                                                 dir=target.parent, delete=False) as stream:
                    copy = Path(stream.name)
                try:
                    shutil.copyfile(source, copy)
                    recovered_code, recovered_detail = self.runner([str(recovery), str(copy)], cancel)
                    if recovered_code == 0:
                        code, detail = self.runner([str(executable), str(copy), "-o", str(target)], cancel)
                    else:
                        detail += f" Восстановление копии не удалось: {recovered_detail}"
                finally:
                    copy.unlink(missing_ok=True)
        if code:
            raise RuntimeError(f"wlf2vcd завершился с кодом {code}. {detail}")
        if not target.is_file() or not target.stat().st_size:
            raise RuntimeError("wlf2vcd завершился без выходного VCD-файла.")
        return None

    @staticmethod
    def _recovery_tool(converter):
        candidate = Path(converter).with_name("wlfrecover.exe" if os.name == "nt" else "wlfrecover")
        return str(candidate) if candidate.is_file() else shutil.which("wlfrecover")

    @staticmethod
    def _run_command(command, cancel):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                                           creationflags=flags)
            except OSError as exc:
                raise RuntimeError(f"Не удалось запустить {Path(command[0]).name}: {exc}") from exc
            try:
                while process.poll() is None:
                    if cancel():
                        process.kill()
                        process.wait()
                        raise LoadCancelled()
                    time.sleep(0.1)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            output.seek(0)
            return process.returncode, output.read(4000).decode("utf-8", "replace").strip()


class ConversionWatch:
    """The ``cancel`` callback a converter polls while it runs, which also watches its output.

    Every ``interval`` seconds it reports the size of the growing VCD as the named phase
    ``CONVERTING_WLF`` and checks free space; below ``CONVERSION_RESERVE`` it raises
    ``DiskSpaceError``, which stops the converter before the disk is full. Converters
    keep their ``convert(source, target, cancel)`` signature.
    """

    INTERVAL = 0.5  # seconds between reports and space checks

    def __init__(self, target, progress, cancel, disk=None, interval=None, clock=time.monotonic,
                 phase=CONVERTING_WLF):
        self.target, self.progress, self.cancel, self.phase = Path(target), progress, cancel, phase
        self.disk, self.clock = disk, clock
        self.interval = self.INTERVAL if interval is None else interval
        self.folder = self.target.parent
        self.checked = None

    def __call__(self):
        now = self.clock()
        if self.checked is None or now - self.checked >= self.interval:
            self.checked = now
            size = 0
            for path in self.folder.glob("*.vcd"):  # the target, or the slices being converted
                try:
                    size += path.stat().st_size
                except OSError:
                    pass
            self.progress(-1, size, self.phase)
            if self.disk is not None:
                free = self.disk.free(self.folder)
                if free < CONVERSION_RESERVE:
                    raise DiskSpaceError(self.folder, size + index_estimate(size) + CONVERSION_RESERVE, free)
        return self.cancel()


class WlfCaptureSource(CaptureSource):
    """Convert a WLF to a temporary VCD and load it.

    A sliced conversion returns a ``SeamCheck``; the loaded capture is checked against it,
    and on any mismatch it is discarded and the file is converted again in one wlf2vcd
    run. The capture then carries a notice for the status bar. A capture that failed the
    check is never returned.
    """

    REJECTED = "Быстрое преобразование WLF по частям отклонено ({}); файл преобразован заново одним запуском wlf2vcd."

    def __init__(self, converter=None, reader=None, pool=None, disk=None, temp=None):
        self.converter = converter or QuestaWlfConverter()
        self.disk = disk if disk is not None else DiskSpace()
        self.temp = storage_or_default(temp)
        self.reader = reader or VcdCaptureReader(pool, self.disk, self.temp)
        self.timings = {}

    def _convert(self, source, vcd, watch, **options):
        try:
            return self.converter.convert(source, vcd, watch, **options)
        except DiskSpaceError:
            raise
        except (OSError, RuntimeError) as exc:
            # wlf2vcd itself may fail on a full disk before the watch notices.
            free = self.disk.free(watch.folder)
            if is_disk_full(exc) or free < CONVERSION_RESERVE:
                written = vcd.stat().st_size if vcd.exists() else 0
                raise DiskSpaceError(watch.folder, written + index_estimate(written) + CONVERSION_RESERVE,
                                     free) from exc
            raise

    def load(self, location, progress=lambda p, n, phase=None: None, cancel=lambda: False):
        source = Path(location).resolve()
        self.timings = {}
        with self.temp.create("ila_wlf_convert_") as folder:
            vcd = Path(folder) / "converted.vcd"
            check = self._convert(source, vcd, ConversionWatch(vcd, progress, cancel, self.disk))
            if cancel():
                raise LoadCancelled()
            capture = self.reader.load(vcd, progress, cancel, source_path=source)
            if check is None:
                return capture
            try:
                began = time.perf_counter()
                mismatches = check.mismatches(capture)
                self.timings["seam_check_seconds"] = round(time.perf_counter() - began, 4)
                self.timings["seams"] = len(check.seams)
            except BaseException:
                capture.close()
                raise
            if not mismatches:
                return capture
            capture.close()
            self.timings["seam_mismatches"] = len(mismatches)
            vcd.unlink(missing_ok=True)
            self._convert(source, vcd, ConversionWatch(vcd, progress, cancel, self.disk, phase=RECONVERTING_WLF),
                          sliced=False)
            if cancel():
                raise LoadCancelled()
            capture = self.reader.load(vcd, progress, cancel, source_path=source)
            first = mismatches[0].describe()
            more = f" и ещё {len(mismatches) - 1:,}" if len(mismatches) > 1 else ""
            capture.notices.append(self.REJECTED.format(first + more))
            return capture


class WlfExportService:
    """Persist a converted VCD atomically so future viewing needs no simulator."""

    def __init__(self, converter=None, temp=None):
        self.converter = converter or QuestaWlfConverter()
        self.temp = storage_or_default(temp)

    def export(self, source, destination, cancel=lambda: False):
        destination = Path(destination).resolve()
        if same_file(source, destination):
            raise ValueError("Файл VCD не может заменить исходный WLF; выберите другое имя.")
        with self.temp.create("ila_wlf_export_", root=destination.parent) as folder:
            temporary = Path(folder) / "capture.vcd"
            self.converter.convert(Path(source).resolve(), temporary, cancel)
            if cancel():
                raise LoadCancelled()
            temporary.replace(destination)
        return destination


class AutoCaptureSource(CaptureSource):
    def __init__(self, csv=None, vcd=None, wlf=None, pool=None):
        self.readers = {".csv": csv or VivadoCsvSource(pool), ".vcd": vcd or VcdCaptureSource(pool=pool),
                        ".wlf": wlf or WlfCaptureSource(pool=pool)}

    def load(self, location, progress=lambda p, n: None, cancel=lambda: False):
        suffix = Path(location).suffix.lower()
        if suffix not in self.readers:
            raise ValueError(f"Неподдерживаемый формат {suffix or '(без расширения)'}; используйте CSV, VCD или WLF.")
        return self.readers[suffix].load(location, progress, cancel)


class SourceNavigator(ABC):
    @abstractmethod
    def available(self, signal) -> bool: ...

    @abstractmethod
    def go_to(self, signal): ...


class UnconfiguredSourceNavigator(SourceNavigator):
    """Extension point for HDL file/line mapping; never guesses file locations."""
    def available(self, signal):
        return False

    def go_to(self, signal):
        raise NotImplementedError("Связь сигналов с исходным HDL ещё не настроена")
