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
- Shift+click in Name/Value selects the inclusive row range and never changes markers.
- Persist only general preferences, not capture state. Tests must inject an isolated
  PreferencesStore. Per-signal Default inherits the general radix.
- Name and Value widths are independent and persistent; auto-fit is explicit only.
- Deleting a group removes its subtree from the view, never changes the CSV data.
- Radix/color and row dragging operate on the selected set; preserve selection on
  context-menu clicks. Validate multi-row moves before modifying the tree.
- Plain drag on the waveform zooms a range; dragging a marker moves it. Ignore
  zoom gestures smaller than two samples. Shift+drag still controls B.
- Copy from A to B with B excluded. Pack selected bits by physical significance, using the
  parent bus radix by default. Clipboard writes happen on the GUI thread only
  after successful background export. Tests must inject an isolated clipboard.
- Run `python -m unittest -v tests.test_waveform tests.test_extensions` for relevant
  behavioral changes. Use `python -m scripts.benchmark_waveform` for changes affecting large captures.
- Keep README.md, docs/USER_GUIDE.md and docs/ARCHITECTURE.md aligned with actual capabilities.
