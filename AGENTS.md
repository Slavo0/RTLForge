# Architecture and maintenance

The user requires object-oriented, extensible design for this application.

- Keep `ila_viewer/__main__.py` a stable launcher. Features belong in `ila_viewer/`.
- Separate data acquisition/storage, presentation state, rendering, input handling,
  context actions, formatting and themes. Prefer composition and dependency injection.
- Extend `CaptureSource`, `SourceNavigator`, `ValueFormat`, `ContextActionProvider`
  and `Theme` rather than adding feature-specific conditionals to the entry point.
- Do not modify acquired samples when reordering, grouping, reversing or styling
  signals. Keep these changes in the presentation model.
- Preserve disk-backed indexing, viewport-bounded rendering and cancellation for
  expensive operations. Expanded bits need their own actual transition indices.
- Route CSV, VCD and WLF through CaptureSource implementations. WLF uses the
  vendor wlf2vcd utility; never guess its binary layout. Keep VCD timestamps
  disk-backed and draw simulator captures proportional to their real time axis.
- A plain left click on the waveform removes B; Shift+left click creates/moves B.
- A plain waveform click selects its row and moves A; Ctrl+click toggles that row;
  Shift+click moves B without changing row selection.
- Mouse placement rounds to the nearest sample. During period calibration,
  plain click/drag controls marker 1 and Shift+click/drag controls marker 2.
- Ctrl+A selects visible signals after Name/Value interaction; after waveform
  interaction it places A at the capture start and B at the exclusive boundary
  after the final sample, so whole-capture copying includes every sample.
- Name filtering must not change bus/group expansion state or visually force it open.
- Shift+click in Name/Value selects the inclusive row range and never changes markers.
- Persist only general preferences, not capture state. Tests must inject an isolated
  PreferencesStore. Per-signal Default inherits the general radix. The ILA sample
  period (CSV time scale), clock edge choice and reverse-bit-rows option are general.
- Remember the last CSV directory and theme-specific bus/bit colors in general
  preferences. Highlight palette persists, but group opacity and groups never do.
- Name and Value widths are independent and persistent; Name auto-fit is explicit;
  Value grows when displayed text overflows.
- Deleting a group removes its subtree from the view, never changes the CSV data.
- Radix/color and row dragging operate on the selected set; preserve selection on
  context-menu clicks. Validate multi-row moves before modifying the tree.
- Plain drag on the waveform zooms a range; dragging a marker moves it. Ignore
  zoom gestures smaller than two samples. Shift+drag still controls B.
- Copy between A and B with the earlier boundary included and the later boundary excluded,
  regardless of marker order. Pack selected bits by physical significance, using the
  parent bus radix by default. Clipboard writes happen on the GUI thread only
  after successful background export. Tests must inject an isolated clipboard.
- With no B, copy or highlight the entire capture. The copy dialog owns per-channel
  radix/bit ranges, separator, unique-values filtering and optional sort.
- Calibrate copy period in an inline banner with draggable markers 1/2 while A/B
  are hidden. Sampling uses the first marker's phase modulo the period, including
  samples before that marker. The condition editor can reopen calibration.
- The same banner can choose a 1-bit clock signal (clk-like names first). Clock
  sampling uses only real rising/falling/both edges (gated clocks such as SPI SCK),
  reads values just before the edge by default, and caps highlight intervals at one
  typical clock period so idle gaps stay unpainted. Route all sampling through
  SamplingGrid implementations in sampling.py.
- Time text follows Vivado: grouped digits, a unit chosen from the ruler step, no
  exponent notation. Reverse Bit Order also reverses expanded bit rows by default.
- Build conditional highlights in cancellable background work with disk-backed,
  merged intervals. Render only intervals intersecting the visible viewport;
  dispose indices when a group or capture is removed.
- Numeric condition literals inherit the compared signal's display radix unless
  prefixed with h/b/d. Preserve operand order for relational comparisons.
  Advanced Highlight stages match consecutive calibrated samples; prev(signal)
  refers to the preceding sample on that period grid or clock-edge grid. Keep overlapping matches
  and rebuild advanced groups after CSV refresh when their signals still exist.
- CSV refresh reconciles signal styling by source name and recompiles highlight
  conditions against the new capture; opening another CSV starts fresh. Keep
  copy/highlight editors nonmodal so navigation and zoom remain available.
- Run `python -m unittest -v tests.test_waveform tests.test_extensions` for relevant
  behavioral changes. Use `python -m scripts.benchmark_waveform` for changes affecting large captures.
- Keep README.md, docs/USER_GUIDE.md and docs/ARCHITECTURE.md aligned with actual capabilities.
