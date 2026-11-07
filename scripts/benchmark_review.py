"""Compare CSV workloads, cold/warm executors and sampled process-tree memory.

Run python -m scripts.benchmark_review [--rows 300000] [--workers 2 4].
Each trial is isolated in a subprocess. The experimental chunk threshold is
256 KiB so executor overhead remains visible on small, reproducible inputs.
Reports are advisory, not a portable automatic worker-selection rule.
"""
import argparse
import concurrent.futures
import cProfile
import ctypes
import json
import os
from pathlib import Path
import pstats
import subprocess
import sys
import tempfile
import threading
import time

from ila_viewer.cpu import WorkerPool
from ila_viewer import data


def resident_bytes(pid):
    if os.name == "nt":
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize",
                "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        handle = kernel.OpenProcess(0x410, False, pid)
        if not handle:
            return 0
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        try:
            return counters.WorkingSetSize if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb) else 0
        finally:
            kernel.CloseHandle(handle)
    try:
        return int(Path(f"/proc/{pid}/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return 0


class ThreadPool(WorkerPool):
    def submit(self, function, *args):
        if self._executor is None:
            self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.workers)
        return self._executor.submit(function, *args)


def trial(path, backend, workers):
    data.MIN_PARALLEL_CHUNK = 256 << 10
    pool = ThreadPool(workers) if backend == "threads" else WorkerPool(workers)
    stop = threading.Event()
    peak = [0]
    def sample():
        while not stop.is_set():
            executor = pool._executor
            processes = getattr(executor, "_processes", None) or {}
            try:
                pids = [os.getpid(), *list(processes)]
                peak[0] = max(peak[0], sum(resident_bytes(pid) for pid in pids))
            except RuntimeError:
                pass  # executor is still starting
            stop.wait(0.01)
    monitor = threading.Thread(target=sample)
    monitor.start()
    times = []
    rows = distinct = 0
    try:
        for _ in range(2):
            start = time.perf_counter()
            cap = data.load_csv(path, pool=pool)
            try:
                times.append(round(time.perf_counter() - start, 4))
                rows = cap.count
                distinct = sum(len(signal.values) for signal in cap.signals)
            finally:
                cap.close()
        return {"backend": backend, "workers": workers, "cold_seconds": times[0],
                "warm_seconds": times[1], "sampled_peak_tree_mib": round(peak[0] / (1 << 20), 1),
                "rows": rows, "distinct_values": distinct}
    finally:
        stop.set()
        monitor.join()
        pool.shutdown()


def make(path, workload, rows):
    columns = 64 if workload == "wide" else 6 if workload == "narrow" else 1
    with path.open("w", newline="") as stream:
        stream.write("Sample in Buffer," + ",".join(f"v{c}" for c in range(columns)) + "\n")
        stream.write("Radix - UNSIGNED," + ",".join(["HEX"] * columns) + "\n")
        for i in range(rows):
            if workload == "unique":
                cells = [f"{i:X}"]
            elif workload == "skewed":
                cells = ["0" * 1023 + "F" if i == rows // 2 else str(i % 2)]
            else:
                cells = ["XXXX" if (i + c) % 5000 == 0 else f"{(i >> (c % 12)) & 0xFFFF:04X}" for c in range(columns)]
            stream.write(f"{i}," + ",".join(cells) + "\n")


def profile(path):
    profiler = cProfile.Profile()
    cap = profiler.runcall(data.load_csv, path)
    cap.close()
    stats = pstats.Stats(profiler)
    items = sorted(stats.stats.items(), key=lambda item: item[1][3], reverse=True)[:15]
    return [{"function": f"{Path(key[0]).name}:{key[1]}:{key[2]}",
             "self_seconds": round(value[2], 5), "cumulative_seconds": round(value[3], 5)}
            for key, value in items]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=300000)
    parser.add_argument("--workers", nargs="+", type=int, default=[2, 4])
    parser.add_argument("--trial", nargs=3, metavar=("PATH", "BACKEND", "WORKERS"))
    args = parser.parse_args()
    if args.trial:
        print(json.dumps(trial(args.trial[0], args.trial[1], int(args.trial[2]))))
        return
    report = {"rows": args.rows, "experimental_min_chunk_bytes": 256 << 10,
              "memory_note": "10 ms sampled parent + pool worker RSS; shared pages may be counted twice; brief peaks may be missed.",
              "workloads": {}}
    with tempfile.TemporaryDirectory(prefix="ila_review_bench_") as folder:
        for workload in ("narrow", "wide", "unique", "skewed"):
            path = Path(folder) / f"{workload}.csv"
            make(path, workload, args.rows)
            entry = {"bytes": path.stat().st_size, "trials": []}
            configurations = [("single", 1)] + [(backend, n) for backend in ("threads", "processes") for n in args.workers]
            for backend, workers in configurations:
                result = subprocess.run([sys.executable, "-m", "scripts.benchmark_review", "--trial", str(path), backend, str(workers)],
                                        check=True, capture_output=True, text=True)
                entry["trials"].append(json.loads(result.stdout))
                print(workload, entry["trials"][-1], flush=True)
            if workload == "unique":
                entry["profile"] = profile(path)
            report["workloads"][workload] = entry
    output = Path(__file__).resolve().parents[1] / "tests" / "artifacts" / "benchmark_review.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
