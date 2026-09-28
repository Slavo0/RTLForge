# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Composite row model, presentation state and source-independent operations."""
from __future__ import annotations

from dataclasses import dataclass
import re
from uuid import uuid4

from .bits import BitCodec
from .formats import DisplayOptions, FORMATS


class DisplaySignal:
    """Adapter: presentation changes never mutate acquired samples."""
    def __init__(self, source, options):
        self.source, self.options = source, options

    def __getattr__(self, name):
        return getattr(self.source, name)

    @property
    def name(self):
        if self.options.reverse:
            return re.sub(r"\[(\d+)\s*:\s*(\d+)\]$", lambda m: f"[{m[2]}:{m[1]}]", self.source.name)
        return self.source.name

    @property
    def short_name(self):
        return self.name.rsplit("/", 1)[-1]

    def format_value(self, value, radix=None):
        if self.options.reverse:
            value = BitCodec.reverse(value, self.width, self.source.radix)
        return FORMATS.format(value, self.width, radix or self.options.radix, self.options.real, self.source.radix)

    def query_value(self, text, radix=None):
        value = FORMATS.parse(text, self.width, radix or self.options.radix, self.options.real, self.source.radix)
        if self.options.reverse:
            if isinstance(value, str):
                # Unknown patterns must be matched against displayed binary bits.
                target = BitCodec.binary(value, self.width, FORMATS.effective(radix or self.options.radix, self.source.radix))
                for original in self.source.values:
                    if isinstance(original, str) and BitCodec.binary(original, self.width, self.source.radix)[::-1] == target:
                        return original
                return "<unknown-pattern-not-present>"
            value = BitCodec.reverse(value, self.width, self.source.radix)
        if isinstance(value, int) and self.source.radix == "SIGNED" and value & (1 << (self.width - 1)):
            value -= 1 << self.width
        return value


class WaveNode:
    kind = "node"

    def __init__(self, label, source=None):
        self.id = uuid4().hex
        self.label = label
        self.parent = None
        self.children = []
        self.expanded = False
        self.options = DisplayOptions()
        self.signal = DisplaySignal(source, self.options) if source else None
        if self.signal:
            self.signal.node = self

    @property
    def expandable(self):
        return self.kind in ("bus", "group")

    def add(self, node, index=None):
        node.parent = self
        self.children.insert(len(self.children) if index is None else index, node)

    def contains(self, node):
        while node:
            if node is self:
                return True
            node = node.parent
        return False


class SignalNode(WaveNode):
    kind = "signal"


class BusNode(SignalNode):
    kind = "bus"


class BitNode(SignalNode):
    kind = "bit"


class ParameterNode(SignalNode):
    kind = "parameter"

    def __init__(self, label, values):
        super().__init__(label)
        self.values = values


class GroupNode(WaveNode):
    kind = "group"


class DividerNode(WaveNode):
    kind = "divider"


@dataclass
class VisibleRow:
    node: WaveNode
    depth: int


class SignalTree:
    def __init__(self, signals=(), metadata=None):
        self.root = GroupNode("root")
        self.root.expanded = True
        for signal in signals:
            cls = BusNode if signal.is_bus else SignalNode
            self.root.add(cls(signal.short_name, signal))
        if metadata:
            group = GroupNode("Параметры захвата")
            for name, values in metadata.items():
                group.add(ParameterNode(name, values))
            self.root.add(group)

    def rows(self, query=""):
        query = query.casefold()
        rows = []

        def visit(node, depth, inherited_match=False):
            name = node.signal.name if node.signal else node.label
            match = inherited_match or not query or query in name.casefold()
            child_rows = []
            if node.expanded or query:
                before = len(rows)
                for child in node.children:
                    visit(child, depth + 1, match if query else False)
                child_rows = rows[before:]
                del rows[before:]
            if match or child_rows:
                rows.append(VisibleRow(node, depth))
                rows.extend(child_rows)

        for child in self.root.children:
            visit(child, 0)
        return rows

    def add_bits(self, bus, signals):
        for signal in signals:
            label = re.search(r"\[\d+\]$", signal.name).group()
            bus.add(BitNode(label, signal))
        bus.expanded = True

    def move(self, node, target, position="before"):
        if node is self.root or node is target or node.contains(target):
            return False
        parent = target if position == "inside" else target.parent
        if parent is None or (position == "inside" and target.kind != "group"):
            return False
        # Expanded bits are children of their physical bus, never independent signals.
        if node.kind == "bit":
            if parent is not node.parent:
                return False
        elif parent.kind != "group":
            return False
        node.parent.children.remove(node)
        index = len(parent.children) if position == "inside" else parent.children.index(target) + (position == "after")
        parent.add(node, index)
        if position == "inside":
            parent.expanded = True
        return True

    def top_level_selection(self, nodes):
        chosen = set(nodes)
        return [node for node in nodes if not any(other is not node and other.contains(node) for other in chosen)]

    def move_many(self, nodes, target, position="before"):
        nodes = self.top_level_selection(nodes)
        parent = target if position == "inside" else target.parent
        if not nodes or parent is None or (position == "inside" and target.kind != "group"):
            return False
        for node in nodes:
            if node is self.root or node.parent is None or node.contains(target):
                return False
            if node.kind == "bit":
                if parent is not node.parent:
                    return False
            elif parent.kind != "group":
                return False
        # Validate the entire batch before changing any parent, then preserve row order.
        for node in nodes:
            node.parent.children.remove(node)
        index = len(parent.children) if position == "inside" else parent.children.index(target) + (position == "after")
        for node in nodes:
            parent.add(node, index)
            index += 1
        if position == "inside":
            parent.expanded = True
        return True

    def create_group_many(self, label, nodes):
        nodes = self.top_level_selection(nodes)
        if not nodes or any(n.kind == "bit" for n in nodes):
            return self.create_group(label)
        first = nodes[0]
        group = GroupNode(label)
        group.expanded = True
        parent, index = first.parent, first.parent.children.index(first)
        for node in nodes:
            node.parent.children.remove(node)
        parent.add(group, index)
        for node in nodes:
            group.add(node)
        return group

    def create_group(self, label, selected=None):
        group = GroupNode(label)
        group.expanded = True
        if selected and selected.kind != "bit":
            parent = selected.parent
            index = parent.children.index(selected)
            parent.children.remove(selected)
            parent.add(group, index)
            group.add(selected)
        else:
            self.root.add(group)
        return group

    def create_divider(self, label, selected=None):
        divider = DividerNode(label)
        if selected and selected.kind == "bit":
            selected = selected.parent
        parent = selected.parent if selected else self.root
        parent.add(divider, parent.children.index(selected) + 1 if selected else None)
        return divider

    def remove_container(self, node):
        if node.kind not in ("group", "divider") or node is self.root:
            return
        parent = node.parent
        parent.children.remove(node)
        node.parent = None

    def inherited_color(self, node):
        while node and node is not self.root:
            if node.options.color:
                return node.options.color
            node = node.parent
        return None
