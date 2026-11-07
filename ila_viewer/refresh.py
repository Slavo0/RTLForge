# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Capture-scoped presentation state that can be reconciled after CSV reload."""

from dataclasses import dataclass, replace

from .data import LoadCancelled
from .highlights import HighlightGroup
from .sampling import PeriodicSampling, as_sampling


def compile_expression(compiler, expression, mode="simple"):
    return compiler.compile_pattern(expression) if mode == "advanced" else compiler.compile(expression)


def resolved_radixes(compiler, condition):
    """Full names a condition depends on and the radix its literals were parsed in."""
    if condition is None:
        return {}
    return {name: compiler.radix_by_name.get(name, "HEX") for name in condition.signal_names()}


def legacy_period(sampling):
    """Store fixed periods as (period, phase); clock grids travel as objects."""
    if isinstance(sampling, PeriodicSampling):
        return sampling.period, sampling.phase
    return sampling, 0


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
    period: object  # SamplingGrid or a legacy sample count
    phase: int
    visible: bool
    id: str
    dependencies: frozenset[str]
    mode: str = "simple"
    radixes: dict | None = None  # literal radix per dependency, fixed when the group was made


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
        groups = []
        for g in model.groups:
            radixes = dict(getattr(g, "radixes", None) or {})
            if not radixes and g.expression:
                # A group without a saved resolution: resolve now, skip it if it no longer compiles.
                try:
                    condition = compile_expression(compiler, g.expression, g.mode)
                except ValueError:
                    continue
                radixes = resolved_radixes(compiler, condition)
            groups.append(HighlightSpec(g.name, g.expression, g.color, g.opacity,
                                        g.marker_a, g.marker_b, g.period, g.phase, g.visible, g.id,
                                        frozenset(radixes), g.mode, radixes))
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
                    # Literals keep the radix they were written in, not the view's current one.
                    saved = type(compiler)(compiler.signals, {**compiler.radix_by_name, **(item.radixes or {})})
                    condition = compile_expression(saved, item.expression, item.mode)
                except ValueError:
                    continue  # A referenced signal disappeared from the refreshed CSV.
                a = min(item.marker_a, capture.count)
                b = capture.count if item.marker_b == self.source_count else min(item.marker_b, capture.count)
                if a == b:
                    continue
                # A clock-edge grid follows its clock signal into the refreshed capture.
                sampling = as_sampling(item.period, item.phase).rebind(capture)
                if sampling is None:
                    continue
                builder = advanced_service if item.mode == "advanced" else service
                intervals = builder.build(capture, condition, a, b, sampling, 0,
                                          lambda percent, rows: progress(percent, rows), cancel)
                rebuilt.append(HighlightGroup(item.name, item.expression, item.color,
                                              intervals, a, b, *legacy_period(sampling),
                                              item.visible, item.id, item.opacity, item.mode,
                                              dict(item.radixes or {})))
                progress(round((index + 1) * 100 / max(1, len(self.highlights))), len(rebuilt))
            return rebuilt
        except BaseException:
            for group in rebuilt:
                group.intervals.close()
            raise
