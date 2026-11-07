# Performance evidence and acceleration decisions

## Reproduce

Use the project's virtual environment:

```powershell
.venv/Scripts/python.exe -m scripts.benchmark_waveform
.venv/Scripts/python.exe -m scripts.benchmark_review --rows 300000 --workers 2 4
```

`benchmark_waveform` exercises one million CSV samples, viewport rendering,
copying, highlights, bit expansion and 200,000 VCD events. `benchmark_review`
compares the same CSV pipeline in one process, a thread pool, and a spawned
process pool. It runs each configuration in an isolated subprocess, measures
first and second loads with the same executor, and profiles the unique-counter
case. JSON reports go to ignored `tests/artifacts/benchmark*.json` files.

The review experiment deliberately lowers the parallel chunk threshold to
256 KiB. It does not change the application's 16 MiB threshold. "Cold" refers
to executor startup, not an emptied operating-system file cache. Memory is the
largest 10 ms sample of parent plus pool-worker resident memory: brief peaks may
be missed and shared pages may be counted more than once. Generation is outside
the timed subprocess. These are single-run synthetic comparisons on one machine.

## Results on 2026-11-07

Each workload contains 300,000 rows. Narrow has six signal columns; wide has 64
and intermittent X values. Unique has one counter with 300,000 distinct values;
skewed has one 1024-byte cell among single-byte cells.

| Workload | Backend | First / second load (s) | Sampled peak tree (MiB) |
|---|---|---:|---:|
| Narrow | Single | 0.234 / 0.229 | 112.4 |
| Narrow | 4 threads | 0.390 / 0.377 | 148.8 |
| Narrow | 4 processes | 0.527 / 0.280 | 301.6 |
| Wide | Single | 1.460 / 1.429 | 434.3 |
| Wide | 4 threads | 2.715 / 2.707 | 679.4 |
| Wide | 4 processes | 2.144 / 2.148 | 849.4 |
| Unique | Single | 0.119 / 0.115 | 128.3 |
| Unique | 4 threads | 0.146 / 0.139 | 108.2 |
| Unique | 4 processes | 0.356 / 0.106 | 248.1 |
| Skewed | Single | 0.138 / 0.133 | 68.2 |
| Skewed | 4 threads | 0.090 / 0.081 | 56.0 |
| Skewed | 4 processes | 0.323 / 0.072 | 196.0 |

The waveform benchmark loaded one million CSV rows in 0.394 s, copied them in
0.294 s, built simple/advanced highlights in approximately 0.04/0.05 s, and loaded
200,000 VCD events in approximately 0.19 s. Timings fluctuate with load and caches;
use the generated report for the exact current values.

## Decisions and limits

Keep the existing process backend and user-selectable worker count. These results
do not support a universal switch to threads or a rule based only on core count.
The wide example was faster and used less memory in one process; users can choose
one worker for similar captures. A future automatic policy needs representative
real captures, repetitions, memory availability, column count, and distinct-value
cardinality. The current "Auto" preference remains physical cores minus one,
subject to input-size gating; it is not memory-adaptive.

The unique-counter profile took 0.111 s: cell splitting consumed 0.031 s,
numeric value interning 0.021 s, decimal conversion 0.019 s, and gather 0.018 s
(cumulative times overlap). There is no dominant Python loop here that justifies
a native rewrite. Candidate native experiments remain a streaming CSV
tokenizer/encoder or VCD tokenizer behind the existing source interfaces, only
if representative profiles show a worthwhile benefit. Preserve Python/NumPy
references, wide integers, X/Z, error locations, cancellation and Windows builds.

CSV padded cells and gather scratch arrays are budgeted; skewed columns use compact
byte objects. Merge files and CS discovery are streamed in bounded chunks. These
fixes do **not** cap total capture RAM: distinct-value dictionaries, global lookup
tables and process transfer buffers still grow with cardinality. Typed or
disk-backed value dictionaries need a separate design and measurement effort.
