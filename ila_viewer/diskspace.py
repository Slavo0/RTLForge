# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Free disk space for temporary capture files; Qt-free.

A WLF load needs the temporary VCD and then the index files, up to about three times
the VCD while a parallel parse places its ranges (3.9 GB + 11.9 GB for the 465 MB
vsim.wlf). Loaders check the space they can estimate before they start, and turn a
full disk met later into ``DiskSpaceError``; their cleanup paths remove every
temporary file, so no half-loaded capture is left.
"""
from __future__ import annotations

import errno
from pathlib import Path
import shutil
import tempfile

# Index bytes per VCD byte measured on wlf2vcd output: 1.72 (real vsim.wlf) and 2.58
# (synthetic). The early check asks for the smaller factor so it does not refuse a file
# that would fit; the exact checks and the full-disk translation cover the rest.
INDEX_PER_VCD_BYTE = 1.5
# Free space a running conversion must leave; below it the converter is stopped.
CONVERSION_RESERVE = 256 << 20
_WINDOWS_DISK_FULL = (39, 112)  # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL


def format_bytes(size):
    """Russian size text with one decimal: 512 МБ, 1,2 ГБ."""
    size = max(0, int(size))
    if size >= 1 << 30:
        return f"{size / (1 << 30):.1f} ГБ".replace(".", ",")
    return f"{max(1, round(size / (1 << 20)))} МБ"


class DiskSpace:
    """Free bytes of the volume holding ``folder``; tests inject a fake probe."""

    def free(self, folder) -> int:
        return shutil.disk_usage(folder).free


class DiskSpaceError(OSError):
    """Not enough space for temporary files; the message names the folder and the need."""

    def __init__(self, folder, needed=None, free=None):
        self.folder, self.needed, self.free = Path(folder), needed, free
        parts = [f"Недостаточно места на диске для временных файлов в папке {self.folder}."]
        if needed:
            parts.append(f"Нужно не меньше {format_bytes(needed)}"
                         + (f", свободно {format_bytes(free)}." if free is not None else "."))
        parts.append("Освободите место или укажите другую папку для временных файлов (переменная TEMP).")
        super().__init__(errno.ENOSPC, " ".join(parts))

    def __str__(self):
        return self.strerror


def temporary_folder():
    return Path(tempfile.gettempdir())


def is_disk_full(exc):
    return isinstance(exc, OSError) and (exc.errno == errno.ENOSPC
                                         or getattr(exc, "winerror", None) in _WINDOWS_DISK_FULL)


def require(probe, folder, needed):
    """Raise ``DiskSpaceError`` when the volume of ``folder`` has less than ``needed`` bytes free."""
    if probe is None or needed <= 0:
        return
    free = probe.free(folder)
    if free < needed:
        raise DiskSpaceError(folder, needed, free)


def index_estimate(vcd_bytes):
    """Index bytes a VCD of this size needs at least (see ``INDEX_PER_VCD_BYTE``)."""
    return int(vcd_bytes * INDEX_PER_VCD_BYTE)
