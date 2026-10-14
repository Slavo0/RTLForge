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
from .vcd import VcdCaptureReader


class CaptureSource(ABC):
    @abstractmethod
    def load(self, location, progress, cancel):
        """Return an owned Capture. UI must not depend on its origin."""


class VivadoCsvSource(CaptureSource):
    def load(self, location, progress=lambda p, n: None, cancel=lambda: False):
        return load_csv(location, progress, cancel)


class VcdCaptureSource(CaptureSource):
    def __init__(self, reader=None):
        self.reader = reader or VcdCaptureReader()

    def load(self, location, progress=lambda p, n: None, cancel=lambda: False):
        return self.reader.load(location, progress, cancel)


class QuestaWlfConverter:
    """Use the vendor utility instead of guessing the proprietary WLF layout."""

    def __init__(self, executable=None, recovery_executable=None, runner=None):
        self.executable = executable
        self.recovery_executable = recovery_executable
        self.runner = runner or self._run_command

    def convert(self, source, target, cancel=lambda: False):
        executable = self.executable or os.environ.get("WLF2VCD") or shutil.which("wlf2vcd")
        if not executable:
            raise RuntimeError("Для открытия WLF нужен wlf2vcd из Questa/ModelSim. "
                               "Выберите его в Настройки → Источники либо добавьте в PATH. "
                               "Можно также открыть экспортированный VCD.")
        source, target = Path(source), Path(target)
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


class WlfCaptureSource(CaptureSource):
    def __init__(self, converter=None, reader=None):
        self.converter = converter or QuestaWlfConverter()
        self.reader = reader or VcdCaptureReader()

    def load(self, location, progress=lambda p, n: None, cancel=lambda: False):
        source = Path(location).resolve()
        with tempfile.TemporaryDirectory(prefix="ila_wlf_convert_") as folder:
            vcd = Path(folder) / "converted.vcd"
            self.converter.convert(source, vcd, cancel)
            if cancel():
                raise LoadCancelled()
            return self.reader.load(vcd, progress, cancel, source_path=source)


class WlfExportService:
    """Persist a converted VCD atomically so future viewing needs no simulator."""

    def __init__(self, converter=None):
        self.converter = converter or QuestaWlfConverter()

    def export(self, source, destination, cancel=lambda: False):
        destination = Path(destination).resolve()
        with tempfile.TemporaryDirectory(prefix="ila_wlf_export_", dir=destination.parent) as folder:
            temporary = Path(folder) / "capture.vcd"
            self.converter.convert(Path(source).resolve(), temporary, cancel)
            if cancel():
                raise LoadCancelled()
            temporary.replace(destination)
        return destination


class AutoCaptureSource(CaptureSource):
    def __init__(self, csv=None, vcd=None, wlf=None):
        self.readers = {".csv": csv or VivadoCsvSource(), ".vcd": vcd or VcdCaptureSource(),
                        ".wlf": wlf or WlfCaptureSource()}

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
