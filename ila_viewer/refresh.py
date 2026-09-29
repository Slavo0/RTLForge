# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Capture-scoped presentation state that can be reconciled after CSV reload."""

from dataclasses import dataclass, replace

from .data import LoadCancelled
from .highlights import HighlightGroup


@dataclass(frozen=True)
class SignalStyle:
    width: int
    options: object


@dataclass(frozen=True)
class HighlightSpec:
    name: str
    expression: str
    color: str
    opacity: int
    marker_a: int
    marker_b: int
    period: int
    phase: int
    visible: bool
    id: str
    dependencies: frozenset[str]
    mode: str = "simple"


@dataclass
class CaptureRefreshState:
    source_count: int
    styles: dict
    highlights: list
    master_enabled: bool

    @classmethod
    def capture(cls, view, model, compiler):
        styles = {}
        def visit(node):
            if node.signal:
                real = replace(node.options.real) if node.options.real else None
                styles[node.signal.source.name] = SignalStyle(
                    node.signal.width, replace(node.options, real=real))
            for child in node.children:
                visit(child)
        visit(view.tree.root)
        groups = [HighlightSpec(g.name, g.expression, g.color, g.opacity,
                                g.marker_a, g.marker_b, g.period, g.phase, g.visible, g.id,
                                (compiler.compile_pattern(g.expression) if g.mode == "advanced"
                                 else compiler.compile(g.expression)).signal_names() if g.expression else frozenset(),
                                g.mode)
                  for g in model.groups]
        return cls(view.capture.count, styles, groups, model.master_enabled)

    def apply_styles(self, view):
        def visit(node):
            self.apply_style(node)
            for child in node.children:
                visit(child)
        visit(view.tree.root)
        view.refresh_rows()

    def apply_style(self, node):
        if not node.signal:
            return
        saved = self.styles.get(node.signal.source.name)
        if not saved:
            return
        node.options.color = saved.options.color
        node.options.radix = saved.options.radix
        if node.signal.width == saved.width:
            node.options.reverse = saved.options.reverse
            node.options.real = replace(saved.options.real) if saved.options.real else None

    def rebuild_highlights(self, capture, compiler, service, advanced_service,
                           progress=lambda percent, count: None, cancel=lambda: False):
        rebuilt = []
        available = {signal.name for signal in capture.signals}
        try:
            for index, item in enumerate(self.highlights):
                if cancel():
                    raise LoadCancelled()
                if not item.dependencies <= available:
                    continue
                try:
                    condition = (compiler.compile_pattern(item.expression) if item.mode == "advanced"
                                 else compiler.compile(item.expression))
                except ValueError:
                    continue  # A referenced signal disappeared from the refreshed CSV.
                a = min(item.marker_a, capture.count)
                b = capture.count if item.marker_b == self.source_count else min(item.marker_b, capture.count)
                if a == b:
                    continue
                builder = advanced_service if item.mode == "advanced" else service
                intervals = builder.build(capture, condition, a, b, item.period, item.phase,
                                          lambda percent, rows: progress(percent, rows), cancel)
                rebuilt.append(HighlightGroup(item.name, item.expression, item.color,
                                              intervals, a, b, item.period, item.phase,
                                              item.visible, item.id, item.opacity, item.mode))
                progress(round((index + 1) * 100 / max(1, len(self.highlights))), len(rebuilt))
            return rebuilt
        except BaseException:
            for group in rebuilt:
                group.intervals.close()
            raise
