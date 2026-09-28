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
  PreferencesStore. Per-signal Default inherits the general radix.
- Remember the last CSV directory and theme-specific bus/bit colors in general
  preferences. Highlight palette persists, but group opacity and groups never do.
- Name and Value widths are independent and persistent; Name auto-fit is explicit;
  Value grows when displayed text overflows.
- Deleting a group removes its subtree from the view, never changes the CSV data.
- Radix/color and row dragging operate on the selected set; preserve selection on
  context-menu clicks. Validate multi-row moves before modifying the tree.
- Plain drag on the waveform zooms a range; dragging a marker moves it. Ignore
  zoom gestures smaller than two samples. Shift+drag still controls B.
- Copy from A to B with B excluded. Pack selected bits by physical significance, using the
  parent bus radix by default. Clipboard writes happen on the GUI thread only
  after successful background export. Tests must inject an isolated clipboard.
- With no B, copy or highlight the entire capture. The copy dialog owns per-channel
  radix/bit ranges, separator, unique-values filtering and optional sort.
- Calibrate copy period in an inline banner with draggable markers 1/2 while A/B
  are hidden. Sampling uses the first marker's phase modulo the period, including
  samples before that marker. The condition editor can reopen calibration.
- Build conditional highlights in cancellable background work with disk-backed,
  merged intervals. Render only intervals intersecting the visible viewport;
  dispose indices when a group or capture is removed.
- CSV refresh reconciles signal styling by source name and recompiles highlight
  conditions against the new capture; opening another CSV starts fresh. Keep
  copy/highlight editors nonmodal so navigation and zoom remain available.
- Run `python -m unittest -v tests.test_waveform tests.test_extensions` for relevant
  behavioral changes. Use `python -m scripts.benchmark_waveform` for changes affecting large captures.
- Keep README.md, docs/USER_GUIDE.md and docs/ARCHITECTURE.md aligned with actual capabilities.
