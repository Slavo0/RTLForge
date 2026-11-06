# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""CSV load time against the number of worker processes; leaves only a JSON report.

Run: python -m scripts.benchmark_parallel [workers ...]
Files: 6 M rows x 7 columns and 1 M rows x 64 columns (with X cells), generated once.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

from ila_viewer.cpu import WorkerPool, available_cpus, detect
from ila_viewer.data import load_csv


def narrow(path, rows):
    with path.open("w", newline="") as stream:
        stream.write("Sample in Buffer,Sample in Window,TRIGGER,bus[8:0],fast_bit,slow_bit,constant[31:0]\n")
        stream.write("Radix - UNSIGNED,UNSIGNED,UNSIGNED,HEX,HEX,HEX,HEX\n")
        for i in range(rows):
            stream.write(f"{i},{i % 1024},{int(i == rows // 2)},{(i // 32) % 512:03X},{i % 2},{(i // 4000) % 2},1234ABCD\n")


def wide(path, rows, columns=64):
    names = ",".join(f"probe{c}[15:0]" for c in range(columns))
    with path.open("w", newline="") as stream:
        stream.write(f"Sample in Buffer,{names}\n")
        stream.write("Radix - UNSIGNED," + ",".join(["HEX"] * columns) + "\n")
        for i in range(rows):
            cells = ("XXXX" if (i + c) % 5000 == 0 else f"{(i >> (c % 12)) & 0xFFFF:04X}" for c in range(columns))
            stream.write(f"{i}," + ",".join(cells) + "\n")


def timed(path, pool):
    start = time.perf_counter()
    capture = load_csv(path, pool=pool)
    seconds = time.perf_counter() - start
    capture.close()
    return round(seconds, 3)


def main():
    cpu = detect()
    sweep = [int(value) for value in sys.argv[1:]] or [1, 2, 4, 8, 12, 15, 16, 24, 32]
    sweep = [value for value in sweep if value <= cpu.logical]
    report = {"cpu": cpu.describe(), "recommended": cpu.recommended_workers(available_cpus()), "files": {}}
    with tempfile.TemporaryDirectory(prefix="ila_parallel_") as folder:
        for name, make, rows in (("6M_x_7", narrow, 6_000_000), ("1M_x_64", wide, 1_000_000)):
            path = Path(folder) / f"{name}.csv"
            make(path, rows)
            entry = {"bytes": path.stat().st_size, "seconds": {}}
            for workers in sweep:
                pool = WorkerPool(workers)
                try:
                    if workers > 1:
                        timed(path, pool)  # first call starts the processes
                    entry["seconds"][workers] = timed(path, pool)
                finally:
                    pool.shutdown()
                print(name, workers, entry["seconds"][workers], flush=True)
            report["files"][name] = entry
    artifacts = Path(__file__).resolve().parents[1] / "tests" / "artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "benchmark_parallel.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
