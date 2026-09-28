# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Stable tree-row selection with an anchor independent of the active row."""


class RowSelection:
    def __init__(self):
        self.ids = set()
        self.anchor = None

    def select(self, rows, index, extend=False, toggle=False):
        if not 0 <= index < len(rows):
            return
        node = rows[index].node
        anchor_index = next((i for i, row in enumerate(rows) if row.node.id == self.anchor), index)
        if extend:
            lo, hi = sorted((anchor_index, index))
            self.ids = {row.node.id for row in rows[lo:hi + 1]}
            if self.anchor is None:
                self.anchor = rows[anchor_index].node.id
        elif toggle:
            self.ids.symmetric_difference_update({node.id})
            self.anchor = node.id
        else:
            self.ids = {node.id}
            self.anchor = node.id

    def retain_visible(self, rows):
        self.ids.intersection_update(row.node.id for row in rows)
        if not any(row.node.id == self.anchor for row in rows):
            self.anchor = None
