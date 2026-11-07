# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""CPU topology and the worker-process pool for heavy processing; Qt-free.

NumPy kernels can release the GIL; parsing and value dictionaries also do Python
work. The application uses processes (spawn on Windows); compare alternatives
with scripts.benchmark_review before changing this policy. Workers exchange paths
and value tables, whose memory cost grows with distinct-value cardinality.
"""
from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
import math
import multiprocessing
import os
import platform
import sys


@dataclass(frozen=True)
class CpuTopology:
    physical: int
    logical: int
    name: str = ""

    @property
    def signature(self):
        return f"{self.name}|{self.physical}|{self.logical}"

    def describe(self):
        name = f"{self.name} · " if self.name else ""
        return f"{name}{self.physical} ядер / {self.logical} потоков"

    def recommended_workers(self, available=None):
        """Physical cores minus one for the GUI and the merge step; no pool on 2 cores or fewer.

        SMT siblings add little to CPU-bound Python/numpy work, so logical threads
        are not counted. ``available`` is the process affinity, if smaller.
        """
        cores = self.physical
        if available is not None:
            cores = min(cores, available)
        return 1 if cores <= 2 else cores - 1


def _windows_physical_cores():
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.windll.kernel32
    relation_core = 0
    size = wintypes.DWORD(0)
    kernel.GetLogicalProcessorInformationEx(relation_core, None, ctypes.byref(size))
    if not size.value:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if not kernel.GetLogicalProcessorInformationEx(relation_core, buffer, ctypes.byref(size)):
        return None
    count = offset = 0
    while offset < size.value:
        # SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX: Relationship (DWORD), Size (DWORD), ...
        relation, record = (int.from_bytes(buffer.raw[offset + i:offset + i + 4], "little") for i in (0, 4))
        if relation == relation_core:
            count += 1
        if not record:
            break
        offset += record
    return count or None


def _windows_cpu_name():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except OSError:
        return ""


def _linux_topology():
    cores, name = set(), ""
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as stream:
            physical = core = None
            for line in stream:
                key, _, value = line.partition(":")
                key, value = key.strip(), value.strip()
                if key == "model name" and not name:
                    name = value
                elif key == "physical id":
                    physical = value
                elif key == "core id":
                    core = value
                elif not key and core is not None:
                    cores.add((physical, core))
                    physical = core = None
            if core is not None:
                cores.add((physical, core))
    except OSError:
        pass
    return len(cores) or None, name


def detect():
    """Physical cores, logical processors and the CPU name of this machine."""
    logical = os.cpu_count() or 1
    physical, name = None, ""
    try:
        if sys.platform == "win32":
            physical, name = _windows_physical_cores(), _windows_cpu_name()
        elif sys.platform.startswith("linux"):
            physical, name = _linux_topology()
    except Exception:  # detection is advisory; never break startup
        physical = None
    name = name or platform.processor()
    return CpuTopology(physical or logical, logical, " ".join(name.split()))


def available_cpus():
    if hasattr(os, "process_cpu_count"):
        return os.process_cpu_count()
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))
    return os.cpu_count()


class WorkerPool:
    """One shared, lazily created process pool; ``workers`` 1 means everything runs in-process."""

    def __init__(self, workers=1):
        self.workers = max(1, int(workers))
        self._executor = None

    def set_workers(self, workers):
        workers = max(1, int(workers))
        if workers != self.workers:
            self.shutdown()
            self.workers = workers

    def workers_for(self, size, min_chunk):
        """At most one worker per ``min_chunk`` of work, so small inputs skip process startup."""
        if self.workers <= 1 or size < 2 * min_chunk:
            return 1
        return max(1, min(self.workers, math.ceil(size / min_chunk)))

    def submit(self, function, *args):
        if self._executor is None:
            self._executor = concurrent.futures.ProcessPoolExecutor(
                max_workers=self.workers, mp_context=multiprocessing.get_context("spawn"))
        return self._executor.submit(function, *args)

    def shutdown(self):
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
