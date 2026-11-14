# WLF load time and repaint lag: task list

This file is the work queue for the performance work described in
`docs/WLF_LOAD_AND_LAG_ANALYSIS.md` (Russian; the measurements there are the baseline).
One AI worker takes the tasks in order, does one at a time, and writes the outcome into
the task's **Result** section in this file.

## Current state (work stopped by the owner)

Nothing is committed. Details and all measurements are in each task's Result.

| Task | Status | Main result |
|---|---|---|
| 1 Benchmark | DONE | `scripts/benchmark_wlf.py`; real VCD kept at `%TEMP%\ila_bench\converted.vcd`; baseline `wlf2vcd` 83.4 s + parse 262.6 s |
| 2 Integer search keys | DONE | `data.search_sorted`; FHD whole-capture repaint 223 → 39.8 ms |
| 3 One viewport transform | DONE | `viewport.py`; FHD 1 % 112 → 55 ms, 2000 ts 125 → 57 ms |
| 4 Dense-row drawing | DONE | `RowPainter`, `pixel_runs`; real FHD cells 5–27 ms, except 2000 ts at 30.6 ms (target 30); synthetic 139.8 → 54.1 ms; golden frames carry `environment.json` |
| 5 Bus assembly after parse | DONE | equal to the old loader on the full real file; parse 226.8 → 192.7 s; peak memory 176.5 MB vs 96 MB before, accepted as a known limit |
| 6 Parallel VCD parse | DONE | `vcd_chunks.py`, `ParallelVcdLoad`; real file 192.7 → 25.0 s on 15 workers, equal to one process; parent peak 97.9 MB |
| 7 WLF conversion progress | DONE | sliced parallel conversion 83.4 → 12.4 s; vsim.wlf total 346 → 43.9–44.5 s; conversion progress; full-disk checks; every seam verified on every load (0.045 s), fallback to one run; preference `wlf_sliced`; `--verify-sliced`: 0 differences |
| 8 Docs and final check | DONE | final run 48.5 s total, FHD whole repaint 7.0 ms; docs, AGENTS rules, `--check-scaling` (ratio 1.06, fails at 12.99 with float keys) |
| 9 Index cache | BLOCKED | owner's decision |
| 10 Temporary-file cleanup | DONE | `tempstore.TempStorage` with an owner lock; startup sweep in the background, 46 folders / 413 MB removed in 0.37 s; start-up unchanged |

Open points:
- Task 6: parent memory grows with the number of distinct values of a signal (its in-memory
  value table), not with capture length as such; an existing property of the design.
- Task 7: the speed-up curve varies between runs with machine state (224–307 CPU seconds
  for the same work); document it as ranges. Sliced conversion rests on Questa 2024.1
  behaviour measured on vsim.wlf and falls back to one wlf2vcd run when a check fails.
- The full suite ran on the final tree: 206 of 206 passed.
- About 12.5 GB of folders that my crashed measurement script left in `%TEMP%` today go with the first sweep after 24 h (task 8 Result).

## Background

The application is a PySide6 waveform viewer (`ila_viewer/`). It opens Vivado ILA CSV,
VCD and Questa WLF captures. A WLF is converted to a temporary VCD by the vendor tool
`wlf2vcd`, then parsed by `VcdCaptureReader` (`ila_viewer/vcd.py`) into disk-backed
arrays: per signal `starts` (int64 row index of each value change) and `codes` (uint32
index into `signal.values`), plus `capture.times` (int64 timestamp of each row).

The problem file is `vsim.wlf` in the repository root: 465 MB, which becomes a 3.92 GB
VCD with 24.4 million timestamps, 237 signals and about 514 million value changes.

Two things are slow:

| Problem | Measured | Where the time goes |
|---|---|---|
| Opening the file | about 5 min 10 s | 76 s in `wlf2vcd`, 232 s in the single-threaded Python VCD parser |
| Every repaint after loading | 140–250 ms for the whole capture, up to 413 ms at 1 % zoom (FHD) | about 77 % in `numpy.searchsorted` called with float keys on int64 arrays |

The same repaint on the 1-million-row `vsim.vcd` in the repository root takes 4–30 ms, so the cost
grows with capture length, which it must not.

## Rules for every task

- Read `AGENTS.md` first. Its architecture rules are binding: object-oriented design,
  composition and dependency injection, Qt-free worker modules, disk-backed indices,
  viewport-bounded rendering, cancellation of expensive work, and vectorized paths that
  equal a scalar reference in `tests/reference.py`.
- Loaded data must be identical before and after an optimization. For the parser this
  means the same signal list and order, the same `values`, `starts`, `codes`, `times`,
  `count`, `timescale` and `split_buses`. For rendering it means the same pixels.
  Prove it with a test, not by inspection.
- User-facing strings in the code are Russian. Keep new ones Russian. Code comments and
  this file stay English.
- Tests inject an isolated `PreferencesStore`, clipboard and `CpuTopology`, as the
  existing tests do. Run the suite after every task:
  `python -m unittest -v tests.test_waveform tests.test_extensions tests.test_review tests.test_remediation`
- The working tree already holds uncommitted changes that belong to the owner. Leave
  them alone. Make no commits, pushes or branch switches; list the files you changed in
  the Result section instead.
- Add no new third-party dependencies and no compiled extensions. A Rust or C reader was
  considered and deferred by the owner (`HANDOFF.md`, item 7).
- Never write a result you did not measure. If a target is missed, say so, give the
  number you got and what limits it.
- Use the project interpreter in `.venv`. `wlf2vcd` (Questa 2024.1) is installed on this
  machine; find it through the app's source settings, the `WLF2VCD` environment variable
  or `PATH`.

## How to record a result

When a task is finished, change its `Status` line to `DONE` (or `BLOCKED` / `PARTIAL`
with the reason) and fill in its **Result** section with:

1. What you changed, as a list of files with a line on each.
2. Before and after numbers from the benchmark of task 1, with the command used.
3. The tests you added and the outcome of the full suite (count passed, count failed).
4. Anything you found that differs from this file's description, and anything left open.

Keep it short and factual. If you discover work that belongs to a later task, add a note
under that task instead of doing it early.

---

## Task 1. Reproducible benchmark for load and repaint

Status: DONE
Depends on: nothing

### Why

The numbers in the analysis came from throwaway scripts that are gone. Every later task
needs a before/after measurement taken the same way, and it must not require the 3.92 GB
file each time.

### What to build

A module `scripts/benchmark_wlf.py`, run as `python -m scripts.benchmark_wlf`, in the
style of the existing `scripts/benchmark_waveform.py` (JSON printed to stdout).

1. A synthetic VCD generator that imitates the real workload at a chosen size. Parameters:
   number of timestamps (default 2 million), timestep (10), number of signals (237, of
   which 191 are 1-bit), 7 clock copies that toggle at every timestamp, a few split
   buses declared bit by bit (`name[7]` … `name[0]` as separate 1-bit `$var`s) so
   `SplitBusAssembler` is exercised, and some X/Z values. Use a fixed random seed. Write
   in the same textual layout `wlf2vcd` produces; convert a small slice of the real file
   and look at it before deciding the layout.
2. A load benchmark: time `VcdCaptureReader().load(path)` and report seconds, timestamps
   per second and megabytes per second. With `--file PATH` it measures a real `.vcd` or
   `.wlf` instead, reporting the `wlf2vcd` phase and the parse phase separately.
3. A repaint benchmark that reproduces the table in the analysis: a real `MainWindow`
   under the offscreen Qt platform with the capture installed, `repaint()` of the
   waveform viewport, best of 5, at 800×500 and 1920×1080, for four zoom levels: the
   whole capture, 1 % of it, 2000 timestamps, 100 timestamps. Also time one marker
   click, one zoom step and one scroll step.
4. A `--profile` switch that prints the top 25 functions by own time (cProfile) for the
   chosen phase.

### Expected result

- The script exists and runs with defaults in under about two minutes.
- The Result section holds the baseline table for the synthetic capture and, measured
  once, for the real `vsim.wlf` (load phases and repaint table). The real-file repaint
  numbers should be close to the analysis: about 248 ms for the whole capture at FHD.
- If the synthetic capture does not show the same repaint cost pattern as the real one
  (time growing with capture length), adjust the generator until it does and say what
  you changed.

### Result

**Files**

- `scripts/benchmark_wlf.py` (new): `SyntheticVcd` generator, load benchmark (`--file` for
  a real `.vcd`/`.wlf`, conversion and parse timed separately with `time.perf_counter`),
  repaint benchmark on a real `MainWindow` (offscreen, isolated `PreferencesStore`,
  `CpuTopology(2, 2)`), `--profile load|repaint` (top 25 by own time, to stderr),
  `--size`/`--zoom` to restrict the table, `--skip-repaint`. JSON on stdout.
- `tests/frames.py` and `tests/fixtures/frames/*.png` (new, made for task 2 but rendered
  here from the unmodified renderer): deterministic frames of an irregular-time VCD and a
  CSV at five zoom levels, used as golden images for tasks 2-4.

**Converted real VCD (reused by tasks 5, 6, 8; do not reconvert):**
`C:\Users\Vyacheslav\AppData\Local\Temp\ila_bench\converted.vcd` (3 920 255 733 bytes,
from `wlf2vcd -o converted.vcd vsim.wlf`, Questa 2024.1). Benchmark JSON files of this
task are in the same folder (`baseline_synth_2m.json`, `baseline_real_wlf.json`).

**Real `wlf2vcd` layout** (header, plus the first 64 MB of the body = 422 624 timestamps):

- `wlf2vcd -help` lists only `-help`, `-quiet` and `-o <outfile>`.
- Header: `$date`, `$version` (`QuestaSim Version 2024.1`), `$timescale` with `1ns` on its
  own line, `$scope module …`/`$upscope`, `$var`, `$enddefinitions $end`. No `$dumpvars`
  and no other directive in the body.
- 237 `$var`s, 237 distinct identifiers (no aliases): `wire 1` ×123, `reg 1` ×68,
  `integer 32` ×5, `reg` vectors ×41 (widths 2×2, 4×2, 5×3, 8×7, 9×13, 11×3, 15×3, 16×5,
  40×1, 48×2). Vector names carry the range after a space (`rx_data [8:0]`); 113 bit-wise
  members are declared as `wire 1 … name [n]` and become 7 split buses (244 signals).
- The seven `clk` copies: three `integer 32` (`b0 !`/`b1 !`), two `wire 1` (`0>`), two
  `reg 1` (`b0 8"`). All toggle at every timestamp.
- Change form follows the var type, not the width: `wire 1` uses the scalar form `0>`;
  every `reg` and `integer` change, including 1-bit regs, uses the vector form
  `b<bits> <id>` with leading zeros dropped (`b0`, `b11111110`, `bx`, `bx0x`). Of the change
  lines, 71 % are vector form, 29 % scalar form. X/Z are rare (1 320 of 8.85 M lines).
- One change per line, lines end in CRLF. Timestamps are `#t` on their own line, step
  always 10 (1ns units). 21.9 changes per timestamp. Identifiers start with `#` for
  three signals (`#`, `#!`, `#"`), so `b0 #` lines occur. The same identifier appears
  twice within one timestamp 45 times in this slice.

**Generator** follows this layout: CRLF, the var types and widths above, scalar form for
`wire 1`, vector form for `reg`/`integer`, three `integer 32` + two `wire 1` + two `reg 1`
clocks, 7 bit-wise buses with 113 members, 22.3 changes per timestamp, X/Z at about
3·10⁻⁴ of changes. It does not repeat an identifier within one timestamp. Default run
(2 M timestamps, 308 MB): 12 s generation, 53 s wall time in total.

**Baseline, synthetic, 2 M timestamps** (`python -m scripts.benchmark_wlf`):
parse 32.4 s, 61 732 timestamps/s, 9.5 MB/s, 51.8 M runs, peak working set 523 MB
(includes the generator's arrays in the same process).

| Window (viewport) / rows | Whole | 1 % | 2000 ts | 100 ts | Click @1 % | Zoom step | Scroll step |
|---|---|---|---|---|---|---|---|
| 800×500 (770×271) / 6 | 21.5 ms | 13.8 ms | 14.4 ms | 7.0 ms | 13.8 ms | 42.4 ms | 29.8 ms |
| 1920×1080 (1890×851) / 18 | 96.2 ms | 84.8 ms | 342.8 ms | 20.0 ms | 86.5 ms | 133.1 ms | 135.0 ms |

**Baseline, real `vsim.wlf`** (`python -m scripts.benchmark_wlf --file vsim.wlf --wlf2vcd
C:/Programms/questasim64_2024.1/win64/wlf2vcd.exe`): `wlf2vcd` 83.4 s, parse 262.6 s
(92 915 timestamps/s, 14.2 MB/s), total 346.0 s; 24 397 216 timestamps, 244 signals,
528.6 M runs, parent peak working set 96 MB.

| Window (viewport) / rows | Whole | 1 % | 2000 ts | 100 ts | Click @1 % | Zoom step | Scroll step |
|---|---|---|---|---|---|---|---|
| 800×500 (770×271) / 6 | 116.7 ms | 60.9 ms | 57.2 ms | 4.9 ms | 67.3 ms | 170.2 ms | 174.3 ms |
| 1920×1080 (1890×851) / 18 | 223.0 ms | 204.4 ms | 120.1 ms | 9.8 ms | 200.9 ms | 368.8 ms | 392.0 ms |

Zoom windows are centred on the middle of the capture. Interaction timings run at the
1 % zoom and include the repaint. "Rows" is the range `WaveformRenderer.paint` walks,
including partly visible rows.

**Differences and open points**

- The whole-capture FHD repaint is 223 ms against 248 ms in the analysis. That is close.
  The analysis counted 7 and 19 rows; `paint` walks 6 and 18 in these windows, because
  the toolbars and the status bar take the rest of the height.
- The 2000-timestamp cell differs (real 120 ms, analysis 334 ms, synthetic 343 ms). It
  depends on how many segments the visible rows have at the chosen position. The
  synthetic file has random activity on its first rows, so it has more segments there.
- The cost grows with capture length in the synthetic file as in the real one. The
  whole-capture repaint is 9.4 ms at 500 k timestamps and 21.5 ms at 2 M (800×500). At
  FHD it is 60.3 ms and 96.2 ms. No generator change was needed for this.
- The synthetic file parses slower per timestamp than the real one (62 k/s vs 93 k/s)
  with similar changes per timestamp. Its vectors take 64 random values each, so the
  writers learn more distinct values. Compare parse speed-ups within one source only.
- `wlf2vcd` took 83.4 s here, not 76 s, and the parse took 262.6 s, not 232 s.
- The load profile shows a fixed cost in opening the per-signal index files: 1.6 s for
  979 `open` calls on a 20 000-timestamp file. See the note under task 6.
- Not run: the full suite, since this task changes no application code.

---

## Task 2. Integer search keys in every per-frame `searchsorted`

Status: DONE
Depends on: task 1

### Why

`sig.starts` and `capture.times` are int64 memmaps. The view position `v.left`,
`v.left + v.span` and the pixel edges are float64. When the key type differs from the
array type, numpy converts the **whole array** to float64 on every call: about 195 MB
copied for a 24-million-transition clock, per row, per frame. Measured on 24 million
int64 values: one float key 22.7 ms, one int key 0.003 ms.

### Where

Confirmed in the per-frame path:

- `ila_viewer/rendering.py`, `WaveformRenderer.draw_signal`: `sig.run_index(v.left)`,
  `np.searchsorted(sig.starts, v.left + v.span, side="left")`, and
  `np.searchsorted(sig.starts, edges, side="right")` with float `edges`.
- `ila_viewer/data.py`, `SignalData.run_index`, typed `int | float` and called with floats.

Check these too. Each takes view coordinates or pixel edges and may have the same problem;
look at the dtype of the searched array before changing anything:

- `ila_viewer/highlights.py`: `visible` (called every frame with `v.left`, and with float
  `edges`), `index_at`, `step`.
- `ila_viewer/spi.py`: track `window` and `busy`, the overlay `busy`, `ClockEdgeMarks.visible`,
  `_LineSampler.at`.
- `ila_viewer/sampling.py`, `ila_viewer/measurements.py`, `ila_viewer/period_dialog.py`,
  `ila_viewer/copying.py`, `ila_viewer/condition_vectors.py`.

### How

The array holds integers, so for any real `x`:

- `searchsorted(a, x, "right") == searchsorted(a, floor(x), "right")`
- `searchsorted(a, x, "left") == searchsorted(a, ceil(x), "left")`

Put this in one place, for example a small helper next to `SignalData` that takes the
array, the key or key array and the side, converts the key to the array's dtype with
`floor` or `ceil`, clamps it to the int64 range, and calls `searchsorted`. Use it at
every site above instead of repeating the conversion. `Capture.sample_at_time` already
does this by hand and has a comment explaining why; make it use the helper.

### Expected result

- A test that compares the helper with plain float `searchsorted` for both sides on random
  integer arrays and keys that are integral, fractional, negative, beyond the last
  element and exactly equal to elements.
- A test that fails if a float key reaches `searchsorted` on an int64 array in the render
  path (for example, render one frame with `numpy.searchsorted` wrapped to record key
  dtypes).
- Rendered output unchanged: render a frame of a fixture capture to a `QImage` before and
  after and compare the bytes, at a zoomed-out and a zoomed-in level.
- Benchmark: the whole-capture FHD repaint on the real file drops from about 248 ms to
  about 50 ms or less. Report all cells of the table.

### Result

**Files**

- `ila_viewer/data.py`: new `search_sorted(array, keys, side)` next to `SignalData`.
  It takes a scalar or an array of keys. Integer arrays get `floor`/`ceil` integer keys.
  Keys beyond the dtype range, ±inf and NaN (sorted last, as in numpy) are answered
  without a search. Strided arrays are bisected in place. A scalar key returns a Python
  int; an array of keys returns int64 of the keys' shape. `SignalData.run_index`,
  `transition`, `search`, `Capture.sample_at_time` (the manual floor/clamp is replaced),
  `samples_at_offsets` and `samples_at_times` use it.
- `ila_viewer/rendering.py`: both `draw_signal` searches.
- `ila_viewer/highlights.py`: `visible`, `index_at`, `step`.
- `ila_viewer/spi.py` (all 20 calls, including track `window`/`busy`/`field_at`/
  `transition`/`search`, the overlay `busy`, `ClockEdgeMarks.visible`, `_LineSampler.at`,
  frame lookup, edge chunks, export), `ila_viewer/sampling.py` (6),
  `ila_viewer/measurements.py` (2), `ila_viewer/period_dialog.py` (1),
  `ila_viewer/copying.py` (3), `ila_viewer/condition_vectors.py` (1).
  `csv_chunks.py` is left as it is: its searches are on worker-local integer arrays with
  integer keys.
- `tests/test_remediation.py`: `SortedSearchTests` and `FrameIdentityTests` (below).
- `tests/frames.py`, `tests/fixtures/frames/*.png`: the golden frames (see task 1).

**Difference from the description:** the float key was not the only whole-array copy.
Highlight intervals are a `(count, 2)` memmap, and SPI fields are a structured array.
Searching `data[:, 1]` or `fields["end"]` passes a strided view, and numpy copies it to
contiguous memory on every call, even with integer keys. So the helper also bisects
strided views in place, reading O(keys · log n) elements. When there are so many keys
that this reads more than the array (keys · log₂n > n, as in exports), it copies once
instead. Microbenchmark, 24 M int64: 1921 float keys 0.07 ms (the analysis measured
21.8 ms for plain `searchsorted`); one float key 8 µs. On a strided 1 M-row column,
1921 keys: 0.44 ms bisected against 1.0 ms for numpy's copy.

**Benchmark, real capture** (after: `python -m scripts.benchmark_wlf --file
%TEMP%\ila_bench\converted.vcd`; before: task 1 on `vsim.wlf`, the same capture):

| Window / rows | Whole | 1 % | 2000 ts | 100 ts | Click @1 % | Zoom step | Scroll step |
|---|---|---|---|---|---|---|---|
| 800×500 / 6, before | 116.7 | 60.9 | 57.2 | 4.9 | 67.3 | 170.2 | 174.3 |
| 800×500 / 6, after | **2.8** | **4.5** | **1.8** | 4.7 | **4.9** | **4.9** | **5.1** |
| 1920×1080 / 18, before | 223.0 | 204.4 | 120.1 | 9.8 | 200.9 | 368.8 | 392.0 |
| 1920×1080 / 18, after | **39.8** | **112.4** | 125.3 | 10.3 | **113.8** | **101.8** | **119.8** |

(ms, best of 5.) The target holds: the whole-capture FHD repaint went from 223 ms to
39.8 ms. The parse is unchanged: 269.8 s here against 262.6 s in task 1 (run-to-run noise).

**Benchmark, synthetic 2 M** (`python -m scripts.benchmark_wlf`), after (before in task 1):
800×500 whole 5.1 (21.5), 1 % 5.6 (13.8), 2000 ts 6.3 (14.4), 100 ts 7.4 (7.0), click
6.1 (13.8), zoom 19.2 (42.4), scroll 5.8 (29.8); FHD whole 48.5 (96.2), 1 % 63.2 (84.8),
2000 ts 350.4 (342.8), 100 ts 21.2 (20.0), click 63.3 (86.5), zoom 60.9 (133.1),
scroll 66.1 (135.0).

**What remains** (cProfile, synthetic 1 % at FHD, 3 frames, 0.65 s): `numpy.searchsorted`
is gone from the top. Left are the per-pixel dense loop in `draw_signal` (0.17 s own
time) with 139 795 `plot_width` property calls (0.16 s), memmap `__getitem__` per
element, the `samples_at_offsets` generator (0.07 s), and `sample_x`/`_time_parts`
(0.12 s). These are tasks 3 and 4. The 2000-timestamp cell is unchanged, as expected;
it is task 3's `sample_x` cost.

**Tests:** `SortedSearchTests` compares the helper with plain float `searchsorted` for
both sides. It uses arrays of 0, 1, 2, 17 and 1000 elements with duplicates, and keys
that are integral, fractional, negative, beyond the last element and equal to elements,
as scalars and arrays, on contiguous, 2-D-column and structured-field views. It also
covers bisecting a strided 300 k column with `ascontiguousarray` patched to fail, and
±inf, NaN, ±2⁷⁰ and the int64 limits. `FrameIdentityTests` renders 10 frames (an
irregular-time VCD with highlights and an A/B band, and a CSV, each at whole, dense,
mid, detail and fractional zoom). Both are compared byte for byte with the golden PNGs
rendered by the unmodified renderer. `numpy.searchsorted` is wrapped during rendering,
and the test fails if an integer array receives a non-integer key or a strided array of
more than one element is searched. Full suite: 167 tests, 167 passed, 0 failed. That run
came before the strided-bisection test was added. The suite after task 3, which includes
that test, passed 171 of 171.

---

## Task 3. Compute the time-axis geometry once per frame

Status: DONE
Depends on: task 2

### Why

For captures with timestamps (VCD, WLF) the x coordinate is proportional to real time.
`WaveformView.sample_x` (`ila_viewer/view.py`) recomputes the frame origin and both
viewport edges on every call, which is three `Capture.time_offset_at` calls and several
scalar reads from the `times` memmap. In the detailed drawing mode `draw_signal` calls
`sample_x` twice per segment: 50 586 calls in three frames, about 160 ms per frame at the
2000-timestamp zoom. Separately, `WaveformRenderer._time_edges` is recomputed for every
dense row although it is the same for the whole frame (19 times per FHD frame), and
`Capture.samples_at_offsets` builds its query array with a Python generator, one
iteration per pixel.

### How

Introduce one object that holds the frame's mapping between sample positions and x, for
example `ViewportTransform` in its own module or in `timebase.py`. It is built once per
paint from the capture, `left`, `span`, `plot_left` and `plot_width`, and offers:

- `x(sample)` for one position and `xs(samples)` for a numpy array, vectorized;
- `edges`, the sample positions of the pixel boundaries, computed lazily once;
- the inverse used by `x_sample`.

It has two implementations behind the same interface: uniform (CSV captures, plain
linear mapping) and time-proportional (captures with `times`). The view asks a factory
for the right one; no `if capture.times is not None` in the renderer.

Then:

- `WaveformRenderer.paint` builds the transform once and passes it to `draw_signal`,
  `draw_track`, `draw_overlays`, `draw_busy`, `draw_boxes`, the highlight loop, bookmarks
  and cursors. `pixel_edges()` returns the cached edges.
- In the detailed mode of `draw_signal`, read `sig.starts[first:last + 1]` and
  `sig.codes[first:last]` once as numpy arrays, clip the boundaries to the viewport and
  convert them all to x in one `xs` call. Keep the per-segment Qt drawing.
- `WaveformView.sample_x` and `x_sample` stay as public methods for input handling and
  delegate to a transform built on demand, cached and invalidated when `left`, `span`,
  the widths or the capture change.
- Replace the generator in `Capture.samples_at_offsets` with a vectorized expression.
  Keep the existing precision rule: subtract the integer origin before going to float.

### Expected result

- A test that `xs` equals a list of `x` calls, and that both equal the current
  `sample_x` on a VCD fixture with irregular timestamps and on a CSV fixture, including
  fractional positions and the last row.
- A test for the vectorized `samples_at_offsets` against the present implementation, kept
  as the scalar reference in `tests/reference.py`.
- Pixel-identical frames before and after, as in task 2.
- Benchmark: the 2000-timestamp and 1 % zoom levels at FHD drop well below 100 ms. Report
  the whole table and the number of `time_offset_at` calls per frame before and after.

### Result

**Files**

- `ila_viewer/viewport.py` (new, Qt-free): abstract `ViewportTransform` (`x`, `xs`,
  `sample`, lazily cached `edges`) with `UniformTransform` (CSV) and `TimeTransform`
  (captures with `times`: integer origin, both edge offsets and the scale computed once).
  The factory `viewport_transform()` holds the only `times is not None` check.
  `TimeTransform.xs` repeats the scalar `time_offset_at` arithmetic step for step: trunc
  and clamp, int64 base minus origin, then float fraction times int width. So
  `xs(a) == [x(s) for s in a]` exactly, and vectorized drawing puts lines on the same
  pixels.
- `ila_viewer/view.py`: `transform()` builds a transform on demand and caches it. The
  capture and its `times` are compared by identity; `count`, `left`, `span`,
  `plot_left` and `plot_width` by value. `set_capture` drops the cached transform.
  `sample_x`/`x_sample` delegate to it.
- `ila_viewer/rendering.py`: `paint` takes the transform once and passes it to
  `draw_signal`, `draw_track`, `draw_overlays`, `draw_busy`, `draw_boxes`, the highlight
  loop, `draw_bookmarks`, `draw_cursors` and `cursor_badges`. `pixel_edges()` returns the
  cached edges, and dense rows no longer recompute them per row. The detailed mode reads
  `starts[first:last + 1]` and `codes[first:last]` once and maps all boundaries with two
  `xs` calls. Clock-edge arrows and highlight bands are mapped with `xs` as well.
  `_time_edges(v)` remains as a one-line delegate (an existing test calls it), and
  `cursor_badges` falls back to `view.sample_x` when it gets no transform (an existing
  test calls it with a fake view).
- `ila_viewer/data.py`: `Capture.samples_at_offsets` has no Python loop any more. The
  queries are `origin + floor(t)` in int64 with the same clamp to [0, 2⁶³−1]. Offsets
  beyond ±2⁶² ticks fall back to the exact per-element rule.
- `tests/reference.py`: `reference_samples_at_offsets` (the generator version),
  `reference_sample_x` and `reference_x_sample` (the old view methods).
- `tests/test_remediation.py`: `ViewportTransformTests`, and a `time_offset_at` call
  counter in `FrameIdentityTests`.

**Differences from the description:** the highlight loop called `x` per band edge, which
was 584 of the 739 `time_offset_at` calls in the golden-frame run. It now maps the bands
with `xs`. Dense rows read `plot_width`/`plot_left` once from the transform instead of the
view property (part of task 4's note; it falls out of passing the transform). The
per-pixel dense loop itself is untouched and remains task 4.

**`time_offset_at` calls per repaint** (FHD, synthetic 500 k timestamps; "before" is a
scratch copy with the task 2 renderer and the old view mapping):

| | Whole | 1 % | 2000 ts | 100 ts |
|---|---|---|---|---|
| before | 104 | 151 318 | 144 968 | 7 532 |
| after | 24 | 24 | 24 | 24 |

**Benchmark, real capture** (`python -m scripts.benchmark_wlf --file
%TEMP%\ila_bench\converted.vcd`), ms, best of 5, with task 2 values in parentheses:

| Window / rows | Whole | 1 % | 2000 ts | 100 ts | Click @1 % | Zoom step | Scroll step |
|---|---|---|---|---|---|---|---|
| 800×500 / 6 | 1.6 (2.8) | 3.0 (4.5) | 1.3 (1.8) | 2.9 (4.7) | 3.4 (4.9) | 3.4 (4.9) | 3.4 (5.1) |
| 1920×1080 / 18 | 20.5 (39.8) | **55.0** (112.4) | **56.9** (125.3) | 6.6 (10.3) | 56.0 (113.8) | 51.4 (101.8) | 60.8 (119.8) |

The target holds on the real file: the 1 % and 2000-timestamp FHD cells are 55 and 57 ms.
Against the task 1 baseline the whole table is now 1.3–3.4 ms at 800×500 and
6.6–60.8 ms at FHD, down from 4.9–392 ms.

**Benchmark, synthetic 2 M** (`python -m scripts.benchmark_wlf`): 800×500 whole 2.7,
1 % 4.6, 2000 ts 4.4, 100 ts 4.0, click 3.5, zoom 10.1, scroll 3.4. FHD whole 23.6,
1 % 30.6, **2000 ts 139.8** (task 2: 350.4), 100 ts 10.1, click 31.4, zoom 29.8,
scroll 31.7. The synthetic 2000-timestamp FHD cell misses the 100 ms target. Its first
18 rows have random activity, which gives about 40 000 visible segments per frame. The
profile (3 frames, 1.16 s) shows that the limit is per-segment work in `draw_segment`
(0.38 s own time, 120 250 calls): `signal_color` walks the tree on every call (0.20 s),
and `QColor`/`QPen` objects and `line()` calls are created per segment (0.24 s). Qt's own
`drawPolygon`/`drawLine` take 0.15 s. Creating the color and pen once per row is part
of task 4.

**Tests:** `ViewportTransformTests` checks the following on an irregular-time VCD and a
CSV at four zoom windows and two plot widths. `x`, `xs` and `sample` equal the old
scalar `sample_x`/`x_sample` (`tests/reference.py`) exactly. The positions include
fractions, the last row, beyond the last row, negatives and integers. `edges` is
computed once. The view cache is reused, and replaced when `left`, `plot_width` or the
capture changes. Vectorized `samples_at_offsets` equals the generator version for random
offsets, exact timestamps, ±10³⁰, 2-D shapes and the default origin.
`FrameIdentityTests` still matches all 10 golden frames byte for byte, and it now
requires fewer than 200 `time_offset_at` calls for all 10 frames (739 before the
band fix). Full suite: 171 tests, 171 passed, 0 failed.

---

## Task 4. Dense-row drawing without per-pixel Python work

Status: DONE — accepted by the owner. The real-file FHD 2000-timestamp cell (30.6 ms against
30 ms) is within measurement noise of the target; no further work on it.
Depends on: task 3

### Why

When a row has more transitions than twice the plot width, `draw_signal` walks the pixels
in a Python `while` loop and reads `indices[i]` and `sig.codes[...]` from the memmap one
element at a time, about two thousand iterations per row. `v.signal_color(sig)` and
`QColor` objects are also created again for each segment, and `signal_color` walks the
tree for an inherited color each time.

### How

- Fetch the codes for all pixel edges in one indexing operation, compute the `busy` mask
  and the run boundaries with numpy (`diff`, `flatnonzero`), and loop only over the
  resulting runs, which are far fewer than pixels.
- Resolve the row's color, its translucent fill color and its pen once per row and pass
  them down to `draw_segment`.
- Keep the visual rule exactly as it is: a pixel that contains a transition is shaded as
  busy; consecutive quiet pixels with the same value form one segment.
- Move the run computation into a Qt-free function so it can be tested against a scalar
  reference in `tests/reference.py` (the current loop, copied there).

Note from task 1: in the repaint profile, `WaveformView.plot_width` (a property that asks
Qt for the viewport width) was called 12 200 times in 3 small frames, from the dense
loop's `while run_start < v.plot_width` conditions. Read it once per row.

### Expected result

- A test of the run computation against the scalar reference on random data, including a
  row that is entirely busy, entirely quiet, and one with X values.
- Pixel-identical frames before and after.
- Benchmark target on the real file: every cell of the repaint table at or under 30 ms at
  FHD and at or under 16 ms at 800×500. If a cell misses, profile it and report what
  remains.

### Result

**Files**

- `ila_viewer/viewport.py`: `pixel_runs(indices, codes)`, Qt-free. It turns the run
  indices at the pixel edges into run arrays `(starts, ends, busy, codes)` with `diff`-style
  comparisons and `flatnonzero`. The rule is unchanged: a pixel whose edges fall in
  different runs is busy, busy neighbours merge, and quiet neighbours merge while the code
  is the same.
- `ila_viewer/rendering.py`: new `RowPainter`, built once per signal row. It resolves the
  row color (`signal_color` walks the tree once per row instead of once per segment), the
  pens, the 65/100-alpha fills, the unknown color, `fill_high`/`fill_bus`, the radix and
  the font metrics, and caches a formatted label per code. It skips `setPen`/`setBrush`
  when the pen or brush is already set. Straight lines in one pen are collected and drawn
  with one `drawLines`; the batch is flushed before every fill, shape or text, so
  overlapping translucent fills keep the old painting order. Three drawing paths:
  - `levels`: 1-bit rows in the detailed mode. Level and transition lines are computed
    with numpy and created with `map(QLineF, ...)`. A run with an X/Z value is drawn by
    `segment` at its place in the sequence. Rows with `fill_high` keep the per-run path,
    because their translucent fills must interleave with the lines.
  - `shapes`: bus rows in the detailed mode. The vertices are computed with numpy, and
    unknown-ness is taken only for the codes on screen. Before this, a counter bus with
    millions of distinct values cost 182 000 `isinstance` calls per two frames. When no
    run is unknown, the pen and brush are set once per row.
  - `segment` / `busy`: dense rows, one call per run from `pixel_runs` instead of per pixel.
  `draw_segment` remains as a thin wrapper for single calls.
- `tests/reference.py`: `reference_pixel_runs` (the old per-pixel loop, copied) and
  `reference_draw_segment` (the old `draw_segment`, copied: a color, a pen and a draw
  call per run).
- `tests/test_remediation.py`: `PixelRunsTests`.
- `tests/frames.py`, `tests/fixtures/frames/`: environment record and styled frames (see
  below).

**Golden frames: environment check (owner's request).**
`tests/fixtures/frames/environment.json` records where the frames were made:

- platform (`Windows 11 AMD64`), QPA (`offscreen`), Qt `6.11.2`, PySide `6.11.2`;
- the font actually resolved for the waveform viewport: `Consolas`, 9.75 pt, 13 px.

`FrameIdentityTests.setUpClass` renders the frames, then compares this environment with
the recorded one. On any difference, `test_frames_equal_the_golden_images` is skipped with
a message that names each differing field ("qt: recorded '6.9.0', here '6.11.2'"), and
says how to regenerate. This was checked by editing the file temporarily. In a matching
environment the test stays strict: it compares the bytes and needs all 20 frames. The
search-dtype and `time_offset_at` guards in the same class run in any environment,
because they do not depend on rasterized text. `python -m tests.frames` writes the PNGs
and `environment.json`. `--missing` adds only the frames that do not exist yet, and
refuses to run in a different environment.

**Styled golden frames.** The 10 original frames all use default preferences, so they never
exercised `fill_high`, `fill_bus`, `separate_signal_styles`, a user row color or the
light theme. Before any task 4 change, 10 `-styled` frames were rendered with all of these
on, by the task 3 renderer, which the default frames had proven unchanged. The `count`
row and the dense CSV rows use the new paths there too.

**Pixel identity.**
- All 20 golden frames are byte-equal after the change.
- `test_row_painter_paints_like_the_old_per_run_drawing` draws 48 random rows into two
  `QImage`s, one with `RowPainter` and one with the copied old `draw_segment`, and
  compares the images. The rows cover:
  - 1-bit rows and buses;
  - X/Z runs between known ones;
  - runs narrower than a pixel, and wide runs with labels;
  - `fill_high` and `fill_bus` on and off.
- The test is not vacuous: shifting the high level by one pixel in `levels` makes it fail.
- `pixel_runs` equals `reference_pixel_runs` on 60 random rows (uniform and irregular
  edges), on entirely busy, entirely quiet and X-split rows, and on zero width.

**Benchmark, real capture** (`python -m scripts.benchmark_wlf --file
%TEMP%\ila_bench\converted.vcd`), ms, best of 5, with task 3 values in parentheses:

| Window / rows | Whole | 1 % | 2000 ts | 100 ts | Click @1 % | Zoom step | Scroll step |
|---|---|---|---|---|---|---|---|
| 800×500 / 6 | 1.0 (1.6) | 1.7 (3.0) | 1.1 (1.3) | 2.2 (2.9) | 2.1 (3.4) | 2.0 (3.4) | 2.0 (3.4) |
| 1920×1080 / 18 | 5.1 (20.5) | **26.7** (55.0) | **30.6** (56.9) | 6.0 (6.6) | 26.7 (56.0) | 23.0 (51.4) | 26.9 (60.8) |

Every 800×500 cell is at or under 2.2 ms (target 16). At FHD every cell is at or under
30 ms except 2000 timestamps, at 30.6 ms. An intermediate run, with `RowPainter` but
before the `levels`/`shapes` paths, gave 28.6 / 33.1 ms for 1 % / 2000.

**What remains in the 2000-timestamp FHD cell** (real file, cProfile, 5 frames each of
2000 and 1 %, 0.377 s before the last change):
- bus rows are about 5 000 notched shapes per frame, most narrower than a pixel;
- Qt's own `drawPolygon` takes about 10 ms per frame;
- building the `QPolygonF` and its six `QPointF` takes most of the rest.

Two ways to get under 30 ms were not taken:
- `drawConvexPolygon` would be correct for this convex shape, but it measured slower
  (44 vs 25 ms per 5 000 shapes) and was dropped.
- Merging sub-pixel shapes would change pixels, which the rules forbid.

**Benchmark, synthetic 2 M** (`python -m scripts.benchmark_wlf`), with task 3 values in
parentheses:
- 800×500: whole 1.1 (2.7), 1 % 1.4 (4.6), 2000 ts 1.8 (4.4), 100 ts 2.8 (4.0),
  click 1.7, zoom 5.3, scroll 1.6.
- FHD: whole 4.7 (23.6), 1 % 8.3 (30.6), **2000 ts 54.1** (139.8), 100 ts 6.3 (10.1),
  click 8.9 (31.4), zoom 7.8 (29.8), scroll 8.7 (31.7).

The 2000-timestamp FHD cell is now under the 100 ms target. In its profile, the
per-segment `draw_segment`/`signal_color` work (0.93 s per 5 frames before) is gone. What
remains is the bus shapes loop and Qt's drawing.

**Tests:** full suite: 175 tests, 175 passed, 0 failed. Four tests are new
(`PixelRunsTests`). `FrameIdentityTests` now checks 20 frames, and allows up to 400
`time_offset_at` calls, the old per-frame limit for twice the frames (318 measured).

---

## Task 5. Take split-bus assembly out of the VCD parse loop

Status: DONE — accepted by the owner. Parse is equal to the old code and 15 % faster on the
real file. Peak memory of 176.5 MB (96 MB before) is accepted as a known limit; the memory
work is not continued as a separate effort (one `BLOCK_CELLS` measurement is part of task 6).
Depends on: task 1

### Why

`SplitBusAssembler.append` (`ila_viewer/vcd.py`) runs for every timestamp inside the
parser. It routes each change to its writer, lowercases every value (61 million calls)
and rebuilds each affected bus string with `"".join`. With nested calls it accounts for
63.8 s of a 120 s profile. It also makes the parse depend on state carried from all
earlier rows, which blocks the parallel parse of task 6.

### How

1. Parse only the signals declared in the VCD. In the token loop, write each change
   straight to its `_Writer`; drop the per-timestamp `changes` dict where a signal changes
   once per timestamp, but keep the rule that the last change within one timestamp wins
   and that the first row gets an all-X value for signals that were never dumped.
2. Build each split bus after the scalar signals are complete, from the member bits'
   finished `starts` and `codes`. The bus changes exactly at the union of its members'
   change rows. For each union row take every member's current value (integer-key
   `searchsorted` from task 2, or a merge), combine them into the bus value, and write
   the bus's `starts`, `codes`, `values` and `value_ids`. Do it in bounded blocks of rows
   so memory stays flat, check `cancel()` between blocks, and write to disk through the
   same file layout the writers use.
3. Keep the result identical to today's: bus name, bit order (including the MSB-first
   reordering of out-of-order declarations), X and Z handling, value strings for unknown
   bits, the order of first appearance in `values`, and `capture.split_buses`.
4. Keep the current `SplitBusAssembler.append` path as the scalar reference in
   `tests/reference.py` and compare against it.
5. While in the token loop, apply the cheap savings the profile shows: call `lower()` only
   for `X`/`Z`, bind hot methods to local names, avoid the generator-in-generator for the
   common tokens. Measure each; keep only those that help.

Note from task 1 (real `wlf2vcd` layout): vector-form changes are the common case in this
file. Every `reg` and `integer` change, including 1-bit regs, is written as `b<bits> <id>`
(`b0 !`, `bx $`); only `wire 1` signals use the scalar form `0>`. In the first 64 MB of the
real VCD, 71 % of the change lines are vector form. So the token loop's hot path is the
`b` branch with its `next(tokens)` call, and the savings in step 5 must be measured on
that branch first. Lines end in CRLF. The split-bus bits (113 `[n]` members) are all
`wire 1`, so they take the scalar form.

Baseline for tasks 5–8 (owner's decision): the "before" load numbers are the task 1
measurements, `wlf2vcd` 83.4 s and parse 262.6 s, not the figures quoted from the
analysis; stated targets such as "30 s or less" stand. Note from task 4: three later
benchmark runs of the unchanged parser on the same `converted.vcd` measured 226.5,
227.6 and 226.8 s, so run-to-run spread (disk cache, other load) is large; compare
before/after on the same day and state both.

### Expected result

- A test that loads several VCDs (`vsim.vcd` from the repository root, the synthetic one from task 1 at small
  size, and hand-written edge cases: bits declared out of order, a bus member never
  dumped, X/Z bits, two changes of one bit in one timestamp, a repeated timestamp) with
  the old and the new code and compares every array and value list.
- Benchmark: single-process parse time of the synthetic capture before and after. The
  profile suggests roughly a quarter to a third less time; report what you get.
- Cancellation during bus assembly releases all files (the existing cancellation tests
  show the pattern).

### Result

Stopped by the owner before the last memory step. The code below matches the numbers given
here (the last unmeasured experiment, `BLOCK_CELLS = 1 << 19`, was reverted).

**Files**

- `ila_viewer/vcd.py`:
  - New `RawTimeline`: the change rows of one split-bus member bit, recorded as the raw
    VCD text. Only a repeat of the same text is dropped, as `_Writer` does. Up to 8 192
    entries stay in memory; beyond that the timeline spills to disk. Its file is opened
    on the first spill, because most bits change rarely and file opens cost about 1 ms
    each here.
  - `SplitBusAssembler` no longer runs in the parse loop. `build()` runs after the parse,
    in row blocks of `BLOCK_CELLS = 1 << 21` (union rows × bits), and checks `cancel()`
    between blocks.
  - First, each member's value runs are written. The raw texts are learned in order of
    first appearance, and runs with equal codes are merged.
  - Then each bus is built. Its rows are the union of its members' change rows, and each
    member's state at those rows is found with `searchsorted`. When every text is one
    character, the joined texts form a `uint8` matrix:
    - rows of only 0/1 become integers with numpy shifts;
    - X/Z/? rows go through `_Writer._learn`;
    - new values are registered in order of first appearance across both kinds of row;
    - code-change rows are kept.
  - A member that wrote a multi-character text (`b01` for one bit) uses an exact
    per-row join instead.
  - Block data is read with `np.fromfile` at an offset; the memory map is used only for
    binary search, so whole timelines do not stay resident. The timeline files are deleted
    in `finally`, and the loader's error path closes them too.
  - Token loop: for a capture with no split buses, nothing changes outside the loop.
    Undumped signals still get an all-X value on row 0. The last change within a
    timestamp still wins (the `changes` dict is kept, see below).
- `tests/reference.py`: `ReferenceSplitBusAssembler`, plus `reference_load_vcd`, the
  complete old `VcdCaptureReader.load` copied verbatim.
- `tests/vcd_compare.py` (new): `capture_differences()` compares count, timescale, times
  and every signal's name, radix, width, `explicit_width`, `values`, `value_ids`, `starts`
  and `codes` (dtype included), plus `split_buses` and their order.
- `tests/test_remediation.py`: `VcdBusAssemblyTests`.

**Token-loop savings (step 5), each measured on the synthetic 2 M file, 3 runs each:**

| Step | Load, s | Kept |
|---|---|---|
| post-parse buses only | 26.06 / 26.04 / 26.07 | — |
| A: per-identifier plan (index, width); no width test for 1-bit | 25.44 / 25.58 / 25.46 | yes |
| C: `b` branch first; `lower()` only if the value is not all digits | 24.84 / 24.74 / 24.70 | yes |
| B: one dict lookup for scalar `0 1 x X z Z` instead of `in` + `lower()` | 24.05 / 24.14 / 24.23 | yes |
| D: write straight to the writers, no per-timestamp dict | 23.14 / 23.21 / 23.26 | **no** |

D was measured *without* the last-change-wins rule, so it is an upper bound (about 0.95 s,
4 %). A correct version needs an undo in the writer for a second change of a signal in one
timestamp. It was not built. For task 6: chunk workers could instead keep the dict only
for the rare duplicate.

**Equality.**
- Real `converted.vcd`: every field of all 244 signals and of the 7 split buses equals
  the old loader's output (`capture_differences` returned nothing). In that run the new
  loader took 194.1 s and the old one 236.7 s.
- `VcdBusAssemblyTests` compare the new loader with `reference_load_vcd` on:
  - `vsim.vcd` from the repository root;
  - the synthetic capture at 3 000 timestamps;
  - both again with small blocks and spills, so that block seams and disk timelines are
    exercised;
  - hand-written cases: bits declared out of order, a member that is never dumped, X/Z
    bits including upper case, two changes of one bit in one timestamp, a repeated
    timestamp, a multi-character member text, and a bus whose members never change.
- Cancellation inside `build` raises `LoadCancelled`, and the temporary folder is
  removed, which on Windows means no file was left open. This holds with timelines both
  on disk and in memory.

**Benchmark** (parse only, `--skip-repaint`). The old parser was measured on the same day.

| Capture | Before | After |
|---|---|---|
| real `converted.vcd` | 226.8 s (226.5–227.6 in task 4 runs; task 1 baseline 262.6 s) | **192.7 s** (−15 %; −27 % against the task 1 baseline) |
| synthetic 2 M | 26.72 s (27.88–27.92 earlier) | **23.23–23.36 s** (−13 %) |

In the synthetic run, bus building after the parse takes 5.2 s. Most of it goes to the
40-bit `status` bus, which has 1.74 million distinct values that every implementation
must register.

**Memory: not finished.**
- Peak working set on the real file:
  - 96 MB before;
  - 1 008 MB with the first version, which memory-mapped whole timelines;
  - 213.8 MB after reading blocks with `fromfile`;
  - 177.6 MB with 2 M-cell blocks;
  - 176.5 MB with the 8 192-entry spill.
- On the synthetic file the new path peaks lower than the old one (349 vs 522 MB), because
  the old loader held the same bus values plus its own state.
- The remaining excess on the real file is most likely the per-block arrays: about 25 MB
  of read blocks, plus union and sort copies, for a 9-bit bus at 233 k rows per block.
- Next step: measure `BLOCK_CELLS = 1 << 19`. The synthetic build was 4.1 s at 2 M cells
  against 4.8 s at 16 M, so smaller blocks have not cost time so far. If that is not
  enough, read the members of a block in a merge instead of concatenating them.

**Tests.** `python -m unittest -v tests.test_waveform tests.test_extensions tests.test_review
tests.test_remediation`: 178 tests, 178 passed, 0 failed. That run used the earlier
constants (`BLOCK_CELLS = 1 << 24`, `SPILL = 1 << 16`). After the change to `1 << 21` and
8 192, `VcdBusAssemblyTests` passed again, and the real-file comparison above was run with
the final constants. The full suite has not been rerun since those two constant changes.

---

## Task 6. Parse the VCD body in parallel on the shared WorkerPool

Status: DONE — real file 192.7 s → 25.0 s with the default 15 workers, equal to the
single-process load in every field.
Depends on: task 5

### Why

Tokenizing alone runs at 36 MB/s in Python, so no single-process loop reads 3.92 GB in
under about 108 s. The machine has 32 logical cores, and the CSV loader already splits a
file over `WorkerPool` processes and merges the pieces (`_load_parallel`, `_plan_column`,
`_run` in `ila_viewer/data.py`; `parse_range`, `place_range`, `ValueTable` in
`ila_viewer/csv_chunks.py`). The VCD reader should work the same way.

### How

Follow the CSV design and reuse its merge pieces where they fit.

1. Inject the pool: `VcdCaptureReader` (or `VcdCaptureSource`) receives the `WorkerPool`
   the way `VivadoCsvSource` does, and `AutoCaptureSource` and the `WlfCaptureSource`
   built in `ila_viewer/window.py` pass it on. Without a pool, or for a small file
   (use `pool.workers_for` with a minimum chunk size), the single-process path of task 5
   runs unchanged.
2. Put the worker code in a new Qt-free module, for example `ila_viewer/vcd_chunks.py`.
   Read the file as bytes; VCD is ASCII.
3. Parse the header in the parent. Split the body into byte ranges that each start at a
   timestamp token. A timestamp is a whitespace-delimited token that begins with `#` and
   whose preceding token is not a vector, real or string value (`b`, `B`, `r`, `R`, `s`,
   `S` prefix), because `#` is also a legal identifier character and may follow such a
   value. Write this boundary search carefully and test it.
4. Each worker parses its range with row numbers local to the range and writes, into the
   capture folder, its timestamps and each signal's local runs. It returns only small
   data: row count, first and last timestamp, and per signal the value table, the run
   count and the first and last value. A signal that does not change in a range has no
   runs there.
5. The parent merges in order. Row offsets are the running sum of row counts. A range's
   first run for a signal is dropped when its value equals that signal's last value so
   far, exactly as `_plan_column` does for CSV. Local value codes are remapped to global
   codes in first-appearance order so `values` matches the single-process result. The
   placement into the final files runs in the workers.
6. Split buses are assembled afterwards by the code from task 5, which is why that task
   comes first.
7. Cases that would make a split unsafe must fall back to the single-process path rather
   than produce a wrong capture. Detect them before or during the merge: a `$comment` or
   other directive in the body besides `$dumpvars`, `$dumpall`, `$dumpon`, `$dumpoff` and
   `$end` (a fast byte search of the body is enough); a range whose first timestamp
   equals the previous range's last one (the reader merges equal consecutive
   timestamps); timestamps that do not increase across a boundary (must raise the
   existing error).
8. Cancellation uses the flag-file pattern of `_run`. Progress goes from completed ranges.
   Errors in a worker reach the user with the same Russian messages as today.
9. AGENTS.md asks for numpy first, processes second. Before writing the worker loop,
   spend a bounded effort on a numpy kernel for the common layout (one change per line:
   classify lines by first byte, handle scalar changes in bulk). Measure it against the
   tuned Python loop on one range and keep whichever is faster; record both numbers. The
   scalar reference stays in `tests/reference.py` either way.

Note from task 1 (real `wlf2vcd` layout): vector-form changes (`b<bits> <id>`) are the
common case here, 71 % of change lines, because every `reg`/`integer` change uses that
form, including 1-bit regs and three of the seven clock copies. Identifiers starting with
`#` exist (`#`, `#!`, `#"` in this file), so a line such as `b0 #` is common, not rare.
Design and test the range-boundary rule ('#' as an identifier after a vector value) for
that form first; the test is mandatory. Design and measure the numpy kernel for
vector-form lines first too. The "one value character + identifier" line is the minority
(about 29 %). Lines end in CRLF (`\r\n`), one change per line. Timestamps are `#t` on
their own line. Within one timestamp the same identifier occurs twice now and then
(45 times in the first 64 MB), so the last-change-wins rule must hold inside a range.

Note from task 1 (load profile): opening files is not free on this machine. A
20 000-timestamp synthetic load spent 1.6 s in 979 `open` calls (two index files per
signal plus the timestamps). A design that writes per-signal files for every range
multiplies that by the number of ranges. Prefer one intermediate file per range, or
measure the open cost first.

### Expected result

- Tests, with an injected `CpuTopology` and a small pool, that the parallel result equals
  the single-process result array for array on: `vsim.vcd`, the synthetic capture, a
  file whose range boundaries are forced to fall right after a vector value with a `#`
  identifier, and files that trigger each fallback. Force small ranges in tests so
  several boundaries occur in a small file.
- A cancellation test: cancel in mid-parse, no files or processes left behind.
- Benchmark on the real converted VCD: parse time from 232 s to 30 s or less, with the
  worker count used. Report the speed-up curve for 1, 4, 8, 15 and the default number of
  workers, and peak resident memory of the parent.
- The disk footprint in `%TEMP%` during load is reported (the temporary VCD plus
  intermediate and final index files) and intermediate files are removed after the merge.

### Result

**Files**

- `ila_viewer/vcd_chunks.py` (new, Qt-free worker module):
  - `header_end`: the byte offset where the body starts, found by the same directive/`$end`
    walk as `_header`, so `$enddefinitions` inside a header `$comment` does not count.
    A non-ASCII header returns None, and the file is then parsed on one process.
  - `range_starts` / `boundary_in`: a range starts at a token that is `#` plus 1–18
    digits, whose preceding token does not start with `b B r R s S`. A candidate is
    skipped when a non-ASCII byte (possibly Unicode whitespace) is near it, or when the
    window ends inside its token. Because the start token is always a valid timestamp,
    the first token of a range can never raise.
  - `RangeParser` / `parse_range`: the token loop of `VcdCaptureReader.load` with local
    rows. Changes go straight to the sinks. A per-signal `seen` row sends a second change
    within one timestamp to a dict, and at commit `replace` pops the change written first.
    If that change introduced a new value or text, the value is forgotten again, so a
    value that only appears replaced never enters the tables. Sinks write a segment before
    they append, so the change being replaced is always still in memory.
    - `_ValueSink` reuses `_Writer._learn`; `_TextSink` records split-bus member texts as
      `RawTimeline` does.
    - One file per range (`part{k}.bin`); the segment index comes back with the result.
    - A directive other than `$dumpvars/$dumpall/$dumpon/$dumpoff/$end` stops the range
      as unsafe. The task asked for a byte search of the body; this check runs inside the
      workers instead, so it adds no serial pass.
    - A range that raises `ValueError` is parsed again in exact mode: every change goes
      through the dict in insertion order, as in the old loop. The error returned is the
      one the single-process loop would raise, or none when the failing value was replaced
      within its timestamp. `early` records whether the error came before the range's
      first commit.
  - `place_signals`: copies segments into the final files and remaps codes and rows. One
    job covers a group of signals across all ranges, so each job opens every part file
    once and writes each target file sequentially.
- `ila_viewer/vcd.py`:
  - `ParallelVcdLoad`:
    - `_check` goes through the ranges in file order. An early error is raised before the
      seam check, and the seam check comes before a later error, which is the order the
      single-process loop meets them. Equal timestamps at a seam, an unsafe range, or an
      empty first range return None, and the file is parsed on one process.
    - `_plan` builds global first-appearance tables with `ValueTable` and drops a range's
      first run when it repeats the previous value or text.
    - `_balance` splits placement jobs across workers by run count.
    - `_map` opens the memory maps from 16 threads.
    - `timings` records each phase for the benchmark.
  - `build_bus` (worker) with `SplitBusAssembler.one_bus` builds one split bus and its
    member bits with the unchanged task 5 code. This goes beyond the task text: built in
    sequence, the buses took 11.7 s of 34 s, and they are independent.
  - `RawTimeline.placed` and the `on_disk` flag let the worker read timelines from the
    files that placement wrote.
  - `VcdCaptureReader(pool=None)`. Without a pool, and for files under
    `2 × MIN_PARALLEL_CHUNK` (16 MB), the task 5 path runs unchanged.
  - `MIN_PARALLEL_CHUNK = 16 MB`, `RANGES_PER_WORKER = 4`.
- `ila_viewer/sources.py`: `VcdCaptureSource`, `WlfCaptureSource` and `AutoCaptureSource`
  pass the pool on. `ila_viewer/window.py` gives the WLF source the window's `WorkerPool`.
- `scripts/benchmark_wlf.py`: new options `--workers` (1 = one process, 0 = physical cores
  − 1), `--disk` (peak size of the `ila_vcd_*` folders in %TEMP%) and `--block-cells`.
  The report includes `phase_seconds`.
- `tests/test_remediation.py`: `VcdParallelParseTests`, described under Tests below.

**Numbers on the real file** (`converted.vcd`, 3 738.6 MB, 24 397 216 timestamps, 244
signals). Command: `python -m scripts.benchmark_wlf --file %TEMP%\ila_bench\converted.vcd
--skip-repaint --workers N [--disk]`.

| Workers | Load, s | Parse phase | Place | Buses | Parent peak, MB |
|---|---|---|---|---|---|
| 1 (single process, measured today) | 194.3 | — | — | — | 176.5 (task 5) |
| 4 (16 ranges) | 69.43 | 58.19 | 3.71 | 7.29 | 97.4 |
| 8 (32 ranges) | 50.74 | 41.03 | 2.55 | 6.88 | 97.3 |
| **15, default (60 ranges)** | **24.99** | 16.66 | 3.20 | 4.79 | **97.9** |
| 31 (124 ranges) | 24.72 | 14.58 | 3.35 | 6.25 | 111.7 |

- The target of 30 s or less is met: 24.99 s in the benchmark and 24.3 s in the
  comparison run.
- Before the buses were built in workers and the memory maps were opened from threads,
  the same 15-worker load took 34.06 s: 11.67 s for buses, and about 1.4 s for opening
  488 memory maps one by one.
- The curve is not linear from 4 to 15 workers. Correction from task 7: the 4- and
  8-worker figures do not reproduce. Repeated runs vary with machine state (73–81 s at
  4 workers, 36–48 s at 8, 25–31 s at 15), and range size does not explain it; see the
  task 7 Result.
- The time that remains is the parse phase, at about 15 MB/s per worker; one range on its
  own runs at 19 MB/s.
- Disk in %TEMP% during the load: the 3 738.6 MB VCD plus up to 11 940 MB in the capture
  folder (part files and final indices both exist during placement). After the load
  6 424.7 MB of final indices remain. The part files and member timelines are deleted;
  the tests assert this.

**Equality.** On the full real file the parallel load (15 workers, 24.3 s) and the
single-process load (194.3 s) have no difference in `capture_differences`: all 244
signals, the 7 split buses, times, count and timescale.

**Synthetic capture** (`SyntheticVcd`, 15 workers):

| Timestamps | Load, s | Parent peak, MB | `status[39:0]` table, MB (distinct values) |
|---|---|---|---|
| 500 k | 2.38 | 117.4 | 37 (434 405) |
| 2 M | 5.82 (5.38 in an earlier run; single process 23.23–23.36) | 303.0 | 148 (1 737 694) |
| 8 M | 17.95 | 1 002.1 | 595 (6 954 107) |

- Parent peak memory grows here, but with the number of distinct values of a signal (its
  in-memory value table), not with capture length as such. This is an existing property
  of the design (owner's decision: not fixed now).
- What grows is the value table of the 40-bit `status` bus, which has a new value at
  almost every change. Any loader keeps that table resident as `values` and
  `value_ids`, and the single-process path holds it too (349 MB at 2 M in task 5).
- The rest of the excess (67, 142 and 387 MB) most likely comes from the moment the list
  arrives from the bus worker: the unpickled list and the new `value_ids` dict exist
  together.
- The real file's tables are small, which is why its parent peak (97.9 MB at 24.4 M
  timestamps) is lower than at 500 k synthetic.
- Not done, as a possible follow-up: make the worker return the values in a compact
  form.

**`SplitBusAssembler.BLOCK_CELLS = 1 << 19`, measured once on the real file:**
- Parallel, 15 workers: 46.04 s, with buses at 20.32 s against 4.79 s, and a parent
  peak of 96.4 MB. In this run the parse phase was also slower (22.7 s), so part of the
  difference may be machine load.
- Single process: 208.42 s and 273.7 MB, against 192.7 s and 176.5 MB with `1 << 21`.
- Both time and memory rose, so the change is not kept; `BLOCK_CELLS` stays `1 << 21`.

**Numpy kernel (step 9), bounded try.** I prototyped the bulk steps for one 62.3 MB range
of the real file (408 208 rows, 8.5 M changes): lines, classes, timestamps, identifier
keys to signals, binary values to integers, per-signal grouping with last-change-wins
and deduplication.
- The bulk steps alone took 1.71 s; the complete Python `RangeParser` takes 3.25 s on
  the same range.
- Still missing: X/Z texts, padding, the lowercasing rules, value tables, member texts,
  output, and the per-line fallback for irregular layouts. These would narrow the gap.
- The gain would be at most about 2× on a phase of 16.7 s, at the cost of a second
  implementation that must match the token loop exactly.
- I kept the Python loop; the prototype is not in the tree. The single-process loop is
  still the scalar reference, together with `reference_load_vcd`.

**Tests.** `VcdParallelParseTests`, 8 tests, using `WorkerPool(3)` and small ranges forced
from the parent. The parallel result is compared with both
`VcdCaptureReader().load` and `reference_load_vcd`.
- `vsim.vcd` and the 3 000-timestamp synthetic capture, with 20 or more ranges.
- The boundary rule:
  - `#` after `b`, `B`, `r` and `S` values, on the same line and on its own line;
  - `b0 # #5`, scalar `1#`, `#a` and `#1x`;
  - U+00A0 near a candidate, 19 digits, and a token cut at the window edge.
- A file with identifiers `#`, `#!` and `#"` in vector form, split into more than 30
  ranges; every range start is a `#digits` line.
- Last change wins:
  - duplicates in the all-X first row, inside a range, in the last row before a seam
    and in the first row after it;
  - three changes in one timestamp, duplicates of a member bit and of a shared
    identifier;
  - a value that only ever appears replaced (asserted absent from `values`);
  - with one and two entries per segment, so a spill comes right before a replaced
    change.
  - Two mutations were checked by hand and are caught: forgetting the replaced value,
    and writing every change directly.
- Fallbacks:
  - a `$comment` in the body;
  - a range starting at the repeated timestamp that ended the previous one (and the same
    file split elsewhere, which is not a fallback);
  - `$enddefinitions` inside a header comment.
- The same error message as one process for:
  - decreasing timestamps across a seam;
  - a token error in a range's first row (it comes before the seam check);
  - a width error there (it comes after the seam check);
  - a width error later in a range;
  - an unfinished change;
  - an error in the first range;
  - and no error when the bad value is replaced in its timestamp.
- Cancellation right after the first range is submitted, and right after the first bus
  is submitted: `LoadCancelled`, and the capture folder is removed, so no worker kept a
  file open. The shared pool still loads afterwards. Its processes are persistent by
  design, so "no processes left behind" means no work is left running for the cancelled
  load.
- The sources pass the pool on.
- Full suite:
  `python -m unittest -v tests.test_waveform tests.test_extensions tests.test_review tests.test_remediation`:
  186 tests, 186 passed, 0 failed.

**Open points.**
- Opening a just-written file costs about 3 ms here (most likely the on-access scan); 16
  threads cut that from 1.4 s to 0.13 s for 488 files. The single-process path (and CSV)
  still opens its memory maps one by one. That path was left unchanged, as the task
  asks.
- The parse phase is the largest remaining part, at 16.7 s of 25 s. The numpy kernel is
  the known option, at about 2× for that phase at most, by the prototype.
- Notes for tasks 7 and 8 were added under those tasks.

---

## Task 7. WLF conversion: visible progress, then overlap with parsing

Status: DONE — vsim.wlf opens in 43.9–44.5 s (346 s at task 1): conversion in parallel time
slices (12.4 s), progress during conversion, clean failure on a full disk.
Depends on: task 6

### Why

`wlf2vcd` takes 76 s on this file and the progress display stands still the whole time,
because `WlfCaptureSource.load` (`ila_viewer/sources.py`) calls the converter without a
progress callback and parsing starts only after it exits. Once task 6 lands, conversion
is the largest remaining part of the load.

### How

Part A, required:

- Give `QuestaWlfConverter.convert` a progress callback. While the process runs, poll the
  size of the growing output file and report it. The final size is unknown, so show the
  phase and the amount converted (for example "Преобразование WLF: 1.2 ГБ") rather than a
  made-up percentage. Look at how `MainWindow.load_progress` displays `(percent, rows)`
  and extend that path cleanly so a source can report a named phase.
- Cancel must still kill the converter promptly.

Part B, investigate first and implement only what the evidence supports:

- Run `wlf2vcd -help` and read the Questa documentation installed with the tool. Record
  the options that exist. Do not assume any option; AGENTS.md forbids guessing about WLF.
- Question 1: does `wlf2vcd` write its output strictly sequentially, so that the file can
  be parsed while it grows? Test by reading the partial file during conversion and
  comparing with the same byte range of the finished file.
- Question 2: can it convert a time window or a subset of signals, so that several
  converter processes could run in parallel on slices of the capture? If such options
  exist, measure whether N processes are actually faster and whether the slices join
  without loss at the boundaries.
- If question 1 holds, feed completed byte ranges of the growing file to the task 6
  workers as they appear, so parsing overlaps conversion and the total approaches the
  conversion time alone. If question 2 holds and helps, propose the design in the Result
  section and implement it only if it stays simple; otherwise leave it as a documented
  option.
- Whatever is implemented stays behind `CaptureSource` / the converter class, with the
  runner injectable as it is now so tests need no Questa installation.

Note from task 6: the parallel reader needs only the header (`vcd_chunks.header_end`) and
the bytes around each range start (`vcd_chunks.range_starts` reads a 1 MB window per
boundary), so ranges of a growing file can be handed out as soon as the bytes after their
end exist. Today `ParallelVcdLoad.load` takes the file size once at the start and submits
all ranges together; an overlapping design would submit ranges as the file grows and keep
the merge in file order. The last range must not be parsed before the converter exits.

### Expected result

- Progress moves during conversion; a test with a fake runner that grows a file shows the
  callbacks arriving.
- The Result section answers questions 1 and 2 with the evidence (commands, outputs,
  measurements).
- Benchmark: total time to open `vsim.wlf`, before (about 310 s) and after. With
  sequential conversion and overlapping parse the expected total is roughly 80–90 s; if
  parallel conversion works, less.

### Result

**Answers to the investigation questions** (Questa 2024.1, `vsim.wlf`):

- **Options.** `wlf2vcd -help` lists only `-help`, `-quiet` and `-o <outfile>`. The
  installed reference (`docs/htmldocs/questa_sim_ref`, wlf2vcd page) shows the same
  syntax, so `wlf2vcd` has no time or signal option.
- **`wlfman`.** The same folder holds `wlfman`. `wlfman filter` takes `-begin <time>`,
  `-end <time>`, `-f <object_list_file>`, `-s <symbol>`, `-r <object>`, and
  `-collapsedelta` (default) / `-collapsetime` / `-nocollapse`. `wlfman info` prints
  `Start Time: 0+0` and `End Time: 243972150+61` for this file. The documentation does
  not say what a slice holds at `-begin`, so that was measured (below).
- **Question 1: is the output sequential? Yes.** Script:
  `growing.py wlf2vcd vsim.wlf out.vcd`, which samples the output every 0.5 s.
  - The conversion took 76.7 s, gave 3 920 255 733 bytes, and the file grew at a steady
    48.7 MB/s.
  - The size never shrank.
  - After the converter exited, 150 samples were compared with the finished file: the
    first 64 KB and the 1 MB below each sampled size. None of them had changed.
  - The header is complete in the first sample (0.5 s).
- **Question 2: can slices be converted in parallel and joined? Yes, for this capture.**
  - A single slice `-begin 100000000 -end 110000000` loads to the same 1 000 001
    timestamps as the full conversion, with equal values of all 244 signals at every row.
    So the end is inclusive.
  - A slice VCD has the same header except `$date`, which holds the conversion time.
  - It starts with a row at `-begin` that lists every signal: 237 lines when `-begin`
    falls between events, more when there are events at that time.
  - A slice that ends between events has no row at its end; its last row is the last
    event.
  - Joining slice 0 whole plus every later slice from its second timestamp gave exactly
    the full capture with 15 slices, both with edges at event times and with edges moved
    5 ns between events. `capture_differences` was empty and the count was 24 397 216.
  - 15 slices took 3.0 s to filter and 5.9 s to convert in parallel (8.9 s), against
    76.7 s for one `wlf2vcd` run.

**What was built**

- **Sliced conversion (`ila_viewer/wlf_slices.py`, new).** `SlicedWlfConversion`:
  - reads the time range with `wlfman info`;
  - runs N × (`wlfman filter` + `wlf2vcd`) in threads through the injected runner;
  - checks on every conversion: headers equal without `$date`, each later slice's first
    timestamp equal to its `-begin`, and a first row no longer than 4 MB;
  - joins the slices into the same single target file (slice 0 is renamed, the others
    are appended from their second timestamp, and each slice is deleted once appended).
  - Anything unexpected returns False, and `QuestaWlfConverter` runs the old single
    conversion, recovery included. Unexpected means `wlfman` missing, `info` unreadable,
    a slice failing, a header or start mismatch.
  - `QuestaWlfConverter(slices=…)`: the window passes the worker count (the general
    preference `worker_count`) and updates it when preferences change; WLF export uses
    it too.
  - Overlapping the parse with a sequential conversion (question 1) was not built. With
    sliced conversion at 12.5 s it could save at most that, while a growing-file reader
    would add complexity. The note under task 6 describes how it would work.
  - One run in three first fell back, because slices finishing in different seconds have
    different `$date` lines. Headers are now compared without `$date` (the reader ignores
    it), and five runs in a row took the sliced path at 12.3–14.9 s.
- **Progress (part A).**
  - `ila_viewer/progress.py` (new): `Phase` and `CONVERTING_WLF`. A source calls
    `progress(-1, amount, phase)` for a step whose total is unknown.
  - `BackgroundTask.report()` routes that to a new `phase` signal; the existing
    `progress(int, int)` signal and its callers stay unchanged.
  - `MainWindow.load_phase` shows a busy bar and «Преобразование WLF: 1,2 ГБ».
  - `ConversionWatch` (`sources.py`) is the `cancel` callback the converter polls. Every
    0.5 s it reports the size of the VCD files being written and checks free space. So
    converters keep `convert(source, target, cancel)`, and cancel still kills the process
    at the next poll.
  - This differs from the task text, which put a progress callback on `convert`: with the
    watch, existing converters and test fakes need no change.
- **Disk space (owner's item 3).** `ila_viewer/diskspace.py` (new) holds `DiskSpace` (the
  injectable probe), `DiskSpaceError` and `format_bytes`. The message names the folder and
  the need, for example «Недостаточно места на диске для временных файлов в папке …
  Нужно не меньше 5,6 ГБ, свободно 1,2 ГБ.»
  - Before a VCD load, `VcdCaptureReader` asks for at least 1.5 × the VCD size. Measured
    index-to-VCD ratios are 1.72 (real) and 2.58 (synthetic); the lower factor is used so
    the check never refuses a file that would fit.
  - Before placement the parallel load checks the exact need: every run once more, plus
    twice the member timelines for the buses. It refuses before writing them.
  - During conversion, `ConversionWatch` stops `wlf2vcd` when less than 256 MB would
    remain. A `wlf2vcd` failure while the disk is nearly full gives the same message.
  - Any `ENOSPC` / Windows error 112 or 39 met later becomes `DiskSpaceError`.
  - Cleanup removes every temporary file, so no capture is returned. A bug was found and
    fixed: on a full disk the error path's `writer.close()` flushed again and raised, so
    the folder was not removed. `vcd.abandon()` now closes files without letting that
    flush stop the cleanup.
- **Threaded memory-map opening in the single-process VCD path (owner's item 2):**
  `vcd.map_files`, shared by both paths.
- **Benchmark.** `--ranges-per-worker`; conversion uses `slices = workers`. The reader
  records `range_seconds` and `range_cpu_seconds`.

**Numbers.** `python -m scripts.benchmark_wlf --file vsim.wlf --skip-repaint --workers 0 --disk`:

| | Before (task 1) | After, two runs |
|---|---|---|
| WLF conversion | 83.4 s | 12.39 s / 12.51 s |
| Parse | 262.6 s | 31.47 s / 32.03 s |
| **Total** | **346.0 s** | **43.86 s / 44.54 s** |
| Parent peak memory | 96 MB | 102.7 MB |

- The expected result was 80–90 s, or less if parallel conversion works; it does.
- The end-to-end WLF load through `WlfCaptureSource` with 15 slices (44.5 s) equals the
  capture of the single conversion: 0 differences, 24 397 216 timestamps.
- The parse phase is slower right after a conversion (22.0–22.7 s) than on a VCD written
  earlier (16.7–17.8 s). This was not investigated; flushing the freshly written 3.9 GB
  is a plausible cause.
- **Disk**, measured with `--disk`. The capture folder peaks at 11 763–12 244 MB. The
  sliced conversion needs about the VCD plus one slice and its WLF slice; the slices are
  deleted as they are joined. After the load only 6 424.7 MB of indices remain, plus the
  3 738.5 MB VCD until it is removed.

**Speed-up curve (owner's item 1).** Real `converted.vcd`, already in the OS cache.

| Workers | Ranges | Load, s (runs) | Parse phase, s | Place, s | Buses, s |
|---|---|---|---|---|---|
| 4 | 16 | 78.84, 80.79, 72.91 | 68.9, 70.6, 63.0 | 3.6–3.7 | 6.1–6.3 |
| 4 | 32 / 64 | 85.39 / 83.79 | 75.4 / 73.0 | 3.7 / 3.9 | 6.0 / 6.6 |
| 8 | 32 | 36.30, 36.86, 47.98 | 28.5, 28.5, 38.8 | 1.9 | 5.0–5.7 |
| 8 | 64 / 128 | 38.18 / 38.59 | 28.6 / 28.8 | 2.5 / 4.5 | 6.8 / 4.9 |
| 15 | 60 | 26.46, 25.52, 31.20 | 17.5, 17.8, 21.9 | 2.2–2.3 | 5.1–6.4 |
| 15 | 30 / 120 | 29.24 / 30.85 | 18.8 / 20.0 | 4.0 / 5.5 | 5.0 / 5.0 |

- The number of ranges depends on the worker count (`RANGES_PER_WORKER = 4` per worker),
  but range size does not explain the curve. At 4 workers, 16, 32 and 64 ranges take
  the same time. At 15 workers, 4 ranges per worker is best: 2 per worker unbalances the
  ranges and 8 per worker slows placement. The setting stays 4.
- In every run the CPU seconds of the ranges equal their wall seconds (247.4 vs 247.9 s
  at 4 workers), so workers never wait for I/O or each other.
- What varies is how many CPU seconds the same work takes: 224–307 s in total between
  runs with the same settings. Per-core speed changes with machine state, and boost
  clock and other load are the plausible causes.
- So the curve is not a property of the code. The earlier 69.4 s / 50.7 s for 4 / 8
  workers (task 6) are within this spread. In the documentation the curve should be
  given as ranges of repeated runs: about 73–81 s at 4 workers, 36–48 s at 8, and 25–31 s
  at 15.

**Tests.** 9 new, and the full suite has 195 tests, 195 passed, 0 failed.
- `DiskSpaceTests`:
  - early refusal with no temporary folder created;
  - `ENOSPC` and Windows error 112 while writing: the folder is removed and the cause is
    kept, while other `OSError`s stay as they are;
  - the conversion phase reports growing sizes;
  - conversion stopped below the reserve: converter killed, partial VCD removed, and a
    `wlf2vcd` failure on a nearly full disk;
  - the window shows the phase text with a busy bar.
- `VcdParallelParseTests.test_full_disk_before_placement_refuses_cleanly`.
- `SlicedWlfConversionTests`, with fake tools that behave as measured, including a
  per-slice `$date`:
  - 2, 3, 4, 7 and 13 slices with edges at events, between events and in an idle gap
    join to the one-run capture;
  - one slice uses no `wlfman`;
  - fallbacks for a failing `info`, a missing time range, a failing slice, a different
    header and a shifted start;
  - cancel leaves no slice file.
  - A mutation that keeps each slice's first row is caught.

**Left open**
- Sliced conversion rests on Questa 2024.1 behaviour measured on one capture. The checks
  cover what the output can show; a capture whose events at a slice edge behave
  differently would only be caught by a full comparison like the one above. (The owner's
  addition below now checks the full state at every seam on every load.)
- The CSV loader does not have the disk-space checks; the request was for WLF/VCD.

**Addition required by the owner: every seam is verified on every load**

What the first row holds, measured on `vsim.wlf` (script
`seam_measure.py`: full capture from `converted.vcd`, slices of 50 000 ticks cut with
`wlfman filter` + `wlf2vcd` at three event times and three times between events):

| `-begin` | Row lines | Signals listed | Equal to the capture at the last row ≤ begin | Equal to the row before |
|---|---|---|---|---|
| event time (30 000 000, 120 000 000, 210 000 000) | 243 | 237 of 237 | all 237 (first listing and last-wins alike) | 230 (7 signals change at begin) |
| between events (39 999 991, 99 999 991, 199 999 981) | 237 | 237 of 237 | all 237 | — |

- At an event time the row lists every signal with its state *after* the events at
  `begin`, then repeats the 6 lines of signals that change there, with the same values.
- Between events it lists the state of the last event before `begin`.
- So in both cases the row states the value at the last capture row with time ≤ `begin`,
  and the check compares exactly that.

What was built
- `ila_viewer/wlf_slices.py`:
  - `SlicedWlfConversion.convert` now returns a `SeamCheck` (or None to fall back). It
    holds the slices' common header (without `$date`) and one `Seam(begin, row bytes)` per
    later slice. The rows are cut from the 4 MB window `_offsets` already reads, so
    nothing extra is read.
  - `SeamCheck.mismatches(capture)` parses each row as the reader does: vectors are
    lower-cased, and X/Z are extended to the width; the last change of an identifier
    wins. It compares every declared signal, aliases included, at
    `search_sorted(times, begin, "right") - 1`. Values are compared after `parse_value`,
    with zero / X / Z extension on both sides, so `b0011` in the row equals `b11` in the
    events. A signal missing from the row, an unreadable row or a different signal list
    is a mismatch.
- `ila_viewer/sources.py`:
  - `QuestaWlfConverter.convert(..., sliced=True)` returns the check from a sliced
    conversion, else None. `sliced=False` forces the single run.
  - `WlfCaptureSource.load` verifies the loaded capture with the check. On any mismatch
    it closes that capture, deletes the VCD and converts again with `sliced=False`. The
    phase shown is `RECONVERTING_WLF` («Быстрое преобразование отклонено, повторное
    преобразование WLF: …»). The returned capture carries the notice «Быстрое
    преобразование WLF по частям отклонено (стык 2 из 3 (время 501), сигнал top/a: …);
    файл преобразован заново одним запуском wlf2vcd.» in the new field
    `Capture.notices`.
  - A capture that failed the check is never returned. `WlfCaptureSource.timings`
    records `seam_check_seconds`, `seams` and `seam_mismatches`.
- `ila_viewer/window.py`: `loaded` puts `capture.notices` in front of the usual status
  message. `wlf_slices(preferences)` gives the worker count, or 1 when slicing is off.
- WLF → VCD export now always uses one `wlf2vcd` run. The exported file is not loaded
  by the export, so a sliced join could not be checked there. This differs from task 7,
  where export used the slices. Export takes about 76 s again instead of about 12 s.
- Preference `wlf_sliced` (default on) in `preferences.py`, with the checkbox
  «Преобразовывать WLF по частям параллельно» under the wlf2vcd path in Settings →
  Sources (`settings_dialog.py`). Its tooltip says what is checked and what happens on a
  mismatch.
- `ila_viewer/progress.py`: `RECONVERTING_WLF`. `ConversionWatch` takes the phase.
- `scripts/benchmark_wlf.py`:
  - `--verify-sliced PATH.wlf` loads the file through `WlfCaptureSource` sliced and in
    one run and prints `capture_differences`.
  - The normal WLF path now times the seam check separately and adds it to the total.

Real file, `python -m scripts.benchmark_wlf --verify-sliced vsim.wlf` (15 workers):

| | Sliced, with the check | One wlf2vcd run |
|---|---|---|
| Load through `WlfCaptureSource` | 43.05 s | 118.63 s |
| Seam check | 0.0425 s for 14 seams × 237 signals | — |
| Notices | none | none |
| `capture_differences` | 0 (24 397 216 timestamps, 244 signals) | |

The check costs 42.5 ms, 0.1 % of the load.

Tests (`SlicedWlfConversionTests`). The fake `wlf2vcd` now writes first-row vectors at
full width, so the check has to normalize.
- `test_seam_rows_state_the_capture_at_each_begin`: 2, 4, 7 and 13 slices load through
  `WlfCaptureSource` with no fallback and no notice, and equal the one-run capture.
- `test_a_seam_mismatch_reloads_through_one_wlf2vcd_run`: each of these causes exactly
  one single run, the reconversion phase and a notice naming «стык 2 из 3», and gives
  the one-run capture:
  - a slice with the event at 400 of `!` removed;
  - a slice with the event of the vector `"` changed;
  - a slice whose first row lacks `#`.
- `test_a_rejected_capture_is_closed_and_never_returned`.
- `test_sliced_conversion_preference`: isolated `PreferencesStore`, the checkbox, the
  converter's slice count and saving.
- Full suite: 199 tests, 199 passed, 0 failed.

Limit: the check proves the state at every seam. An event lost inside a slice and
overwritten before the next seam, or one in the last slice, does not show at any seam.
The fake-runner tests therefore corrupt the last change before a seam.
---

## Task 8. Documentation and final verification

Status: DONE — vsim.wlf 346.0 → 48.5 s in the final run (43.9–44.5 s in task 7); FHD whole-capture repaint 223 → 7.0 ms; documents updated; `--check-scaling` added; 206/206 tests pass.
Depends on: tasks 2, 3, 4, 5, 6, 7, 10

### What to do

- Run the full test suite and the benchmark of task 1 on the real `vsim.wlf` one final
  time; put the complete before/after tables (load phases, repaint table) here.
- Update `docs/PERFORMANCE.md` with the new evidence and `docs/ARCHITECTURE.md` with the
  new pieces (viewport transform, integer search helper, parallel VCD reader, post-parse
  bus assembly, conversion progress). Update `README.md` and `docs/USER_GUIDE.md` only
  where user-visible behavior changed.
- Add a short tip to `docs/USER_GUIDE.md`: logging the same clock under many hierarchy
  names multiplies the file size and load time (in this capture seven copies of `clk` are
  a third of all changes), so log only the signals needed.
- Add to `AGENTS.md` the rules a future contributor must keep: integer keys for searches
  in int64 indices, one viewport transform per frame, VCD parsing through the pool with
  the single-process fallback. Match the style of the existing bullets.
- Extend `scripts/benchmark_waveform.py` or the new benchmark with an assertion that
  whole-capture repaint time does not grow with capture length by more than a small
  factor, so the float-key regression cannot return unnoticed.
- Add a closing section to `docs/WLF_LOAD_AND_LAG_ANALYSIS.md` (in Russian, like the rest
  of that file) stating what was fixed and the final numbers.

Note from task 6: `docs/ARCHITECTURE.md` should describe `ila_viewer/vcd_chunks.py`
(ranges that start at a `#digits` token not preceded by a `b`/`r`/`s` value, one part file
per range with a segment index, direct writes with a dict only for a repeated change in one
timestamp, the exact-order rerun of a failing range), `ParallelVcdLoad` (merge in file
order, fallbacks to one process, per-group placement, one split bus per worker through
`build_bus`, memory maps opened from 16 threads) and the `pool` argument of
`VcdCaptureReader`, `VcdCaptureSource` and `WlfCaptureSource`. The AGENTS.md bullet about
Qt-free worker modules should list `vcd` and `vcd_chunks`; tests of the parallel VCD path
force small ranges from the parent (`vcd.MIN_PARALLEL_CHUNK`, `vcd.RANGES_PER_WORKER`,
`vcd_chunks.SEGMENT`, or a patched `vcd_chunks.range_starts`), because patches do not reach
spawned workers.

Note from task 7: `docs/ARCHITECTURE.md` should describe `wlf_slices.SlicedWlfConversion`
(the measured slice behaviour it relies on, the checks, the fallback),
`progress.Phase` with `BackgroundTask.report` / `MainWindow.load_phase`, `ConversionWatch`,
and `diskspace` (estimate before a load, exact check before placement, full-disk translation,
`vcd.abandon` in error paths). `docs/USER_GUIDE.md`: WLF conversion now shows its progress, and
the free-space need (the VCD plus up to about three times its size). AGENTS.md: WLF slicing
relies only on measured wlfman/wlf2vcd behaviour that is checked on every conversion, with the
single wlf2vcd run as fallback; full-disk errors become `DiskSpaceError` after cleanup. Give the
speed-up curve as ranges of repeated runs.

Note from task 6 (memory, owner's decision): parent memory grows with the number of
distinct values of a signal, because `SignalData.values` and `value_ids` are in-memory
tables; it does not grow with capture length as such. This is an existing property of the
design and should be stated in `docs/PERFORMANCE.md` and `docs/ARCHITECTURE.md`, not fixed.

Note from task 4: `docs/ARCHITECTURE.md` should also describe `RowPainter` (row colors,
pens and labels resolved once per row, same-pen lines batched in `drawLines`) and
`viewport.pixel_runs`; the AGENTS.md rules should say that per-segment drawing must not
create colors or pens, and that golden frames (`tests/fixtures/frames`, with
`environment.json`) are regenerated only from a revision known to render correctly.

Note from the owner's addition to task 7:
- `docs/USER_GUIDE.md` and `docs/ARCHITECTURE.md` must say plainly that the sliced
  conversion was measured on Questa 2024.1, what is checked on every load (headers,
  slice starts, the full state of every signal at every seam), that a failed check loads
  through one wlf2vcd run with a status-bar notice, and how to turn it off (Settings →
  Sources, «Преобразовывать WLF по частям параллельно»). Export always uses one run.
- Note only, do not investigate: the parse phase is slower right after a conversion
  (22.0–22.7 s) than on a VCD written earlier (16.7–17.8 s).

### Expected result

All tests pass; documents describe the code as it is; the final tables are in this file.

### Result

**Final run**: `python -m scripts.benchmark_wlf --file vsim.wlf --workers 0 --disk` (15
workers, sliced conversion), against task 1 (`baseline_real_wlf.json`).

| Load phase | Task 1 | Final | Task 7 runs |
|---|---:|---:|---:|
| WLF conversion | 83.4 s | 13.91 s | 12.39 / 12.51 s |
| Parse | 262.58 s | 34.53 s | 31.47 / 32.03 s |
| — split / parse / place / buses | — | 0.11 / 24.49 / 3.01 / 6.64 s | |
| Seam check (14 seams) | — | 0.045 s | |
| **Total** | **345.98 s** | **48.49 s** | 43.86 / 44.54 s |
| Parent peak memory | 95.9 MB | 143.3 MB | 102.7 MB |
| Timestamps / changes | 24 397 216 / 528 626 740 | same | same |

- The final total is 4 s slower than the task 7 runs: conversion is 1.4 s slower and
  the parse phase 2.5 s slower. The range CPU time was 343 s, against 224–307 s for the
  same work in task 7, so per-core speed varied again (see task 7). The code path
  matches task 7 plus the 0.045 s check.
- Parent peak memory, 143.3 vs 102.7 MB, was not investigated. It is within the
  accepted task 5 limit (176.5 MB).
- `--disk` reported a 24 386 MB peak and 18 837 MB after. The sampler adds up every
  `ila_vcd_*` folder in `%TEMP%`, and two leftover 6 237 MB folders from my crashed
  measurement script (task 10) were there. Without them this load used 11 912 MB at
  the peak and 6 363 MB after, in line with task 7 (11 763–12 244 / 6 424.7 MB).

| Repaint, ms (best of 5) | 800×500, task 1 → final | 1920×1080, task 1 → final |
|---|---:|---:|
| Whole capture | 116.7 → 1.1 | 223.0 → 7.0 |
| 1 % | 60.9 → 1.8 | 204.4 → 36.7 |
| 2000 timestamps | 57.2 → 1.2 | 120.1 → 33.4 |
| 100 timestamps | 4.9 → 3.3 | 9.8 → 6.3 |
| Marker click at 1 % | 67.3 → 2.4 | 200.9 → 29.8 |
| Zoom step at 1 % | 170.2 → 2.2 | 368.8 → 23.7 |
| Scroll step at 1 % | 174.3 → 3.5 | 392.0 → 28.0 |

**Scaling assertion.** `python -m scripts.benchmark_wlf --check-scaling` (new, in
`scripts/benchmark_wlf.py`):
- It loads synthetic captures of 250 000 and 4 000 000 timestamps and measures
  whole-capture FHD repaint. It exits with status 1 when the ratio exceeds `--max-ratio`
  (default 3.0).
- Result: 4.7 ms and 5.0 ms, ratio 1.06, passed (41 s).
- With the task 1 float-key `searchsorted` patched back in (scratch `scaling_mutant.py`,
  not in the repository), the ratio is 12.99 and the check fails.
- AGENTS.md says it must pass.

**Documents changed**
- `docs/ARCHITECTURE.md`:
  - table rows for `vcd_chunks`, `wlf_slices`, `progress`, `diskspace`, `tempstore` and
    `viewport`;
  - a section «Большие VCD и WLF»: the parallel reader (ranges, part files, direct writes,
    exact-order rerun, merge in file order, fallbacks, placement, `build_bus`, 16-thread
    mapping, the `pool` argument), post-parse bus assembly, sliced conversion with the
    measured behaviour, every check, the fallback and the preference, export with one run,
    progress phases, disk-space checks with `vcd.abandon`, temporary folders and the sweep,
    the memory property, the search, viewport and `RowPainter` rules, golden frames and how
    the parallel tests force small ranges;
  - the Qt-free worker list now includes `vcd` and `vcd_chunks`.
- `docs/PERFORMANCE.md`: a new «Large WLF / VCD» section with both tables, the speed-up
  curve as ranges of repeated runs (73–81 s at 4 workers, 36–48 s at 8, 25–31 s at 15), the
  scaling check, the slower parse after a conversion, disk and memory.
- `docs/USER_GUIDE.md`:
  - conversion progress;
  - the sliced conversion: measured on Questa 2024.1, what is checked on every load, what
    a failed check does and the status-bar text, how to turn it off;
  - export always uses one run;
  - the `clk` copies tip;
  - the «Временные файлы» section (task 10).
- `README.md`: the faster WLF open, the check and the switch, and cleanup after a crash.
- `AGENTS.md`:
  - worker modules now include `vcd` and `vcd_chunks`;
  - pool parse with the one-process fallback;
  - WLF slicing is checked on every conversion and seam, and falls back to one run;
  - `DiskSpaceError` only after cleanup;
  - integer keys, one viewport transform per frame, no colors or pens per segment, golden
    frames, `--check-scaling`;
  - temporary folders only through `TempStorage` (task 10).
- `docs/WLF_LOAD_AND_LAG_ANALYSIS.md`: a closing section in Russian with what was fixed
  and the final numbers.

**Tests.** Full suite on the final tree: 206 tests, 206 passed, 0 failed (105 s). The
real `%TEMP%` held 20 `ila_*` folders before the run and 20 after.

**Left open (owner)**
- Not investigated, as instructed: the parse phase is slower right after a conversion
  (22.0–22.7 s in task 7, 24.5 s in this run) than on a VCD written earlier (16.7–17.8 s).
- About 12.5 GB of folders in `%TEMP%` (`ila_vcd_gq3j_4yy`, `ila_vcd_xa94dusf` and seven
  empty ones) were left by my measurement script crashing during the task 7 addition.
  The sweep removes them at the first start after 2026-11-15 18:23. Eight `ila_wave_*`
  folders from 2026-09-29 cannot be read by this account at all («Access is denied»), and
  the sweep leaves them.
- Task 9 (index cache) is still BLOCKED and needs the owner's decision.

---

## Task 10. Remove temporary files left by a crashed or killed run

Status: DONE — folders carry an owner lock; a background sweep at start removed 46 stale folders (413 MB) from %TEMP% in 0.37 s, start-up unchanged (0.41–0.44 s to first window).
Depends on: task 7. Do it before task 8, so the documentation covers it.

### Why

All temporary data lives in the system temporary folder (`tempfile.gettempdir()`):

| Folder prefix | Created in | Holds | Size for `vsim.wlf` |
|---|---|---|---|
| `ila_wlf_convert_` | `WlfCaptureSource.load` (`sources.py`) | the converted VCD and slice files | 3.9 GB |
| `ila_vcd_` | `VcdCaptureReader` (`vcd.py`) | index files of an open VCD/WLF | 6.4 GB, up to 11.9 GB while loading |
| `ila_wave_` | `load_csv` (`data.py`) | index files of an open CSV | depends on the file |

A normal exit removes them: the conversion folder when parsing ends, the index folder in
`Capture.close()` from `MainWindow.closeEvent` or when another file is opened. Nothing
removes them when the process is killed, crashes or loses power. One failed load of a
large WLF can leave up to 16 GB behind. On the owner's machine the temporary folder
already holds about 60 such leftover folders (`ila_vcd_*`, `ila_wave_*`, about 0.6 GB).

### How

1. One place creates the application's temporary folders. Add a Qt-free class (for
   example `TempStorage` in `ila_viewer/files.py` or its own module) that the three call
   sites above use instead of calling `tempfile.TemporaryDirectory` directly. It is
   injected, with the system temporary folder as the default root, so tests pass their
   own root. Keep the existing prefixes and the `Capture.storage` contract (an object
   with `name` and `cleanup()`).
2. Every folder it creates carries proof that its owner is alive. Do not rely on a PID
   alone: PIDs are reused. Prefer a lock file inside the folder that the owning process
   keeps open and locked for the folder's lifetime (`msvcrt.locking` on Windows,
   `fcntl.flock` elsewhere); a folder is stale when its lock can be taken. The worker
   processes of the pool write into the parent's folder, so only the parent holds the
   lock. Verify by test that the lock is released when the owner is killed, on Windows
   in particular.
3. A sweep removes stale folders. It considers only direct children of the root whose
   names start with the application's prefixes, never follows symbolic links or
   junctions out of the root, and never touches anything else in the temporary folder.
   - A folder with a lock file: removed when the lock can be taken.
   - A folder without one (left by earlier versions): removed only when nothing in it
     was modified during the last 24 hours. On Windows a file that is still mapped by a
     live process cannot be deleted; treat that failure as "in use" and leave the whole
     folder alone rather than deleting part of it. Check for this before deleting
     anything in the folder, for example by trying to rename the folder first.
   - Errors are never shown to the user and never stop the application.
4. The sweep runs once at application start, in the background (the existing
   `BackgroundTask` pattern), so the window appears without delay, and it can be
   cancelled on exit. Wire it in `application.py` / `MainWindow` by injection, not in
   `__main__.py`. Two instances of the application running at once must not remove each
   other's folders; test this.
5. Make the normal paths robust too: confirm that a cancelled load, a failed load and a
   full disk each leave no folder behind (task 7 fixed one such case), and add the
   missing tests.
6. The benchmark and the tests create folders with their own prefixes
   (`ila_benchmark_*`, `ila_prof_*`) and the tests' reference loader uses the
   application's prefixes. Make the test suite leave nothing behind in the system
   temporary folder: count the `ila_*` folders before and after a full run and report
   the difference. `%TEMP%\ila_bench` is the kept benchmark data; the sweep must not
   remove it (it does not match the application's prefixes — keep it that way).

### Expected result

- Tests, all on an injected temporary root, never on the real one:
  - a folder whose owner process was killed is removed by the sweep (start a child
    process that creates a folder and is then terminated);
  - a folder owned by a live process, in this process and in another one, survives;
  - an old unmarked folder is removed, a recent unmarked one survives, and an unmarked
    folder with a file mapped by a live process survives whole;
  - folders with other names, and a junction or symbolic link pointing outside the root,
    are untouched;
  - a sweep that hits a permission error finishes without raising.
- The application starts no slower: report the time to first window with and without the
  sweep, and the sweep's own duration on the owner's temporary folder.
- After the work, run the sweep once against the real temporary folder and report how
  many folders and how much space it removed and what it left and why. Do not delete
  anything there by any other means.
- `docs/USER_GUIDE.md` gains a short section: where temporary files live, how much space
  a large WLF needs, when they are removed, and that leftovers from a crash are removed
  at the next start. `AGENTS.md` gains the rule that temporary folders are created only
  through the new class.

### Result

**What was built**

- `ila_viewer/tempstore.py` (new, Qt-free):
  - `TempStorage(root=None, …)` with `create(prefix, root=None)`. `DEFAULT` uses the
    system temporary folder. `storage_or_default(temp)` is used by every call site.
  - `TempFolder` keeps the `Capture.storage` contract (`name`, `cleanup()`, context
    manager, a `weakref.finalize` at exit). It creates `.ila-owner.lock`, which it keeps
    open and locked: `msvcrt.locking` on 1 byte on Windows, `fcntl.flock` elsewhere.
  - `cleanup` deletes the contents while still locked, then unlocks and deletes the lock
    and the folder. If it fails halfway, the folder is left unlocked, so the next sweep
    removes it.
  - `sweep(cancel)` returns a `SweepReport` (`removed`, `removed_bytes`, `kept` with a
    reason, `errors`) and never raises. It looks only at direct children of the root
    whose names start with `PREFIXES` (`ila_wlf_convert_`, `ila_vcd_`, `ila_wave_`,
    `ila_wlf_export_`).
    - Links and junctions are skipped (`DirEntry.is_symlink` / `is_junction`).
    - A folder with a lock file goes when the lock can be taken.
    - An unmarked folder goes when nothing in it changed for 24 h.
    - Removal first renames the folder to `<name>.sweep-<hex>`, which keeps the prefix.
      On Windows the rename fails while a live process has a file in it open or mapped,
      and then nothing is deleted. A removal that stops halfway is found again by the
      next sweep.
    - Deletion is a bottom-up walk of its own that removes links inside instead of
      following them.
- Call sites, each with an injected `temp` argument:
  - `data.load_csv`, `VivadoCsvSource`;
  - `VcdCaptureReader` and `ParallelVcdLoad`; the reader's disk-space check now asks
    about `temp.root`;
  - `WlfCaptureSource` (the conversion folder, and the reader it creates);
  - `WlfExportService`: the folder next to the target, through `create(…, root=)`.
- Wiring:
  - `MainWindow.start_temp_sweep(storage)` runs the sweep in a `BackgroundTask` of its
    own, not `self.worker`, so opening a file never waits for it.
  - `closeEvent` interrupts the sweep and waits for it.
  - `WaveformApplication(temp_storage=DEFAULT)` starts the sweep with
    `QTimer.singleShot(0)` after `show()`.
  - A `MainWindow` built alone, as in the tests, never sweeps. `__main__.py` is
    unchanged.
- Tests that recorded folders by patching `tempfile.TemporaryDirectory` now patch
  `TempStorage.create`.
- Docs: a «Временные файлы» section in `docs/USER_GUIDE.md`, and the rule in `AGENTS.md`.

**Tests** (`TempStorageTests`, 7 new, all on their own root):
- A child process creates a folder and is killed (`Popen.kill`, TerminateProcess on
  Windows). Before the kill the sweep keeps it ("owner alive"); after it the sweep
  removes it. This also shows the OS releases the lock when the owner is killed.
- Live owners survive: a folder of this process, and one of a second process that
  itself sweeps while this one holds a folder. Neither removes the other's.
- Unmarked folders: an old one is removed, a recent one is kept, and an old one with a
  `numpy.memmap` open is kept whole ("in use", every file still there).
- `tmp_other`, `ila_bench`, a prefixed file, and a junction `ila_vcd_link` pointing
  outside are untouched. A junction inside a removed folder is removed without touching
  its target.
- Permission errors in removal, in the age scan and in listing the root: the sweep
  finishes, reports them, and the next sweep removes the renamed folders.
- These loads leave no folder behind: VCD cancelled, malformed and refused for space;
  CSV cancelled and malformed; WLF converter failure and cancel. A good load keeps its
  folder until `close()`.
- The window sweeps in the background and stops on close.
- Full suite: 206 tests, 206 passed, 0 failed.
- `ila_*` folders in the real `%TEMP%` (except `ila_bench`): 66 before the full run and
  66 after, with the same names, so the suite leaves nothing behind.

**Start-up** (`t10_startup.py`, process start to the first event after the window is
shown, plus a 50 ms timer):

| | First window, s | Sweep, s |
|---|---|---|
| Without the sweep (3 runs) | 0.642, 0.430, 0.404 | — |
| With the sweep, first real run | 0.408 | 0.366 (46 folders removed) |
| With the sweep, nothing stale (2 runs) | 0.430, 0.438 | 0.024, 0.021 |

The start is not slower; the sweep runs after the window appears.

**The one sweep of the real `%TEMP%`.** Before it there were 65 prefixed folders,
13 062 MB.
- Removed 46 folders, 413.4 MB.
- Kept 11 «recent, unmarked» folders, created less than 24 h ago. Nine of them, about
  12.5 GB (`ila_vcd_gq3j_4yy` and `ila_vcd_xa94dusf` at 6.2 GB each), were left at
  18:22–18:23 today by my own measurement script crashing during this task, before the
  lock existed. A sweep after 24 h removes them; I did not delete them by other means.
  The other two date from 02:52 and 15:06 today.
- 8 errors: `ila_wave_*` folders from 2026-09-29 give «Access is denied» even to
  `icacls`. Their permissions do not let this account read them, so the sweep reports
  them and goes on.
- Not touched, because it is not an application prefix: `ila_prof_99ebpscy`, an
  earlier profiling folder.

**Differences from the task text**
- The lock is on the folder's own `.ila-owner.lock`. Pool workers never create or lock
  folders.
- `ila_wlf_export_` also goes through `TempStorage` (root = the export target's
  folder). The startup sweep looks only at the system temporary folder, so leftovers next
  to an export target are not swept.
- `tests/reference.py` (the oracle loader) still uses `tempfile.TemporaryDirectory` with
  the application's prefixes. Its folders are cleaned when the test captures close, as
  the before/after count shows; the oracle was left as it is.

---

## Task 9. Cache of parsed indices next to the WLF

Status: BLOCKED — needs the owner's decision. Do not start.

Reopening the same WLF could take seconds if the parsed indices were kept on disk, keyed
by path, size and modification time. This stores capture data outside the session, and
`AGENTS.md` says to persist only general preferences, not capture state. It also needs
decisions on where the cache lives, its size limit and when it is deleted. The owner
decides whether this is wanted once the results of tasks 6 and 7 are known.
