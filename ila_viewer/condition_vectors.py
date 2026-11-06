# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Array evaluation of a ``Condition`` over many grid points at once.

``Condition.matches`` stays the reference for one sample; this module must give
the same answer for every point. Work is done in *code space* where possible:
each signal leaf has a table over its distinct values (``SignalData.values``),
so a comparison with a literal is evaluated once per distinct value and then
looked up for every point. Values that fit in int64 use numpy; X/Z texts and
wider values fall back to the scalar rules of ``conditions`` for just those codes
(or points), so semantics never diverge.
"""
from __future__ import annotations

import operator

import numpy as np

_INT64 = (-(1 << 63), (1 << 63) - 1)
_NUMERIC_OPS = {"==": np.equal, "!=": np.not_equal, "<": np.less, ">": np.greater,
                "<=": np.less_equal, ">=": np.greater_equal}
_PY_OPS = {"==": operator.eq, "!=": operator.ne, "<": operator.lt, ">": operator.gt,
           "<=": operator.le, ">=": operator.ge}


def _fits(value):
    return type(value) is int and _INT64[0] <= value <= _INT64[1]


def compare_scalar(kind, left, right):
    """The scalar comparison rule of ``Condition.matches``."""
    try:
        if kind in {"<", ">", "<=", ">="} and not all(isinstance(x, (int, float)) for x in (left, right)):
            return False
        return bool(_PY_OPS[kind](left, right))
    except (TypeError, ValueError):
        return False


def truth_scalar(value):
    return isinstance(value, (int, float)) and value != 0


class LeafTable:
    """Per-code values of one ``SignalValue``: int64 where representable, objects otherwise."""

    def __init__(self, leaf):
        self.leaf = leaf
        source = leaf.source
        values = source.values
        count = max(1, len(values))
        self.ok = np.zeros(count, dtype=bool)
        self.num = np.zeros(count, dtype=np.int64)
        self._objects = {}
        is_int = np.fromiter((type(v) is int for v in values), dtype=bool, count=len(values))
        if source.width <= 62 and len(values):
            raw = np.zeros(len(values), dtype=np.int64)
            if is_int.all():
                raw[:] = values
            elif is_int.any():
                raw[is_int] = [v for v in values if type(v) is int]
            selected = self._select(raw)
            self.num[:len(values)] = self._signed(selected & ((1 << leaf.width) - 1))
            self.ok[:len(values)] = is_int
            for code in np.flatnonzero(~is_int):  # X/Z text: a bit selection may still be a number
                self._learn(int(code))
        else:
            for code in range(len(values)):
                self._learn(code)

    def _select(self, raw):
        bits = self.leaf.bits
        if bits is None:
            return raw
        packed = np.zeros_like(raw)
        for offset in bits:
            packed = (packed << 1) | ((raw >> offset) & 1)
        return packed

    def _signed(self, bits):
        leaf, width = self.leaf, self.leaf.width
        sign = 1 << (width - 1)
        if leaf.effective == "SIGNED":
            return np.where(bits & sign, bits - (1 << width), bits)
        if leaf.effective == "SIGNED_MAGNITUDE":
            return np.where(bits & sign, -(bits & (sign - 1)), bits)
        return bits

    def _learn(self, code):
        value = self.object(code)
        if _fits(value):
            self.ok[code] = True
            self.num[code] = value

    def object(self, code):
        """The scalar value ``SignalValue.at`` would return for this code."""
        if code not in self._objects:
            leaf = self.leaf
            value = leaf._select(leaf.source.values[code])
            if isinstance(value, int):
                value = leaf._signed(value & ((1 << leaf.width) - 1))
            self._objects[code] = value
        return self._objects[code]

    def lut(self, scalar_rule, numeric_rule=None):
        """Bool per code: numpy on int64 codes when ``numeric_rule`` is given, scalar rule otherwise."""
        result = np.zeros(len(self.ok), dtype=bool)
        slow = np.flatnonzero(~self.ok) if numeric_rule is not None else np.arange(len(self.leaf.source.values))
        if numeric_rule is not None:
            result[self.ok] = numeric_rule(self.num[self.ok])
        for code in slow:
            if code < len(self.leaf.source.values):
                result[code] = scalar_rule(self.object(int(code)))
        return result


class _Points:
    """A value per grid point: int64 + ok mask, with a scalar getter for the rest."""

    def __init__(self, num, ok, getter):
        self.num, self.ok, self.getter = num, ok, getter


class VectorEvaluator:
    """Evaluate one compiled condition on arrays; tables are cached across chunks."""

    def __init__(self, condition):
        self.condition = condition
        self.tables = {}

    def table(self, leaf):
        key = id(leaf)
        if key not in self.tables:
            self.tables[key] = (leaf, LeafTable(leaf))
        return self.tables[key][1]

    def evaluate(self, at, previous):
        at = np.asarray(at, dtype=np.int64)
        previous = np.asarray(previous, dtype=np.int64)
        self._codes = {}
        self.at, self.previous = at, previous
        result = self._truth(self._node(self.condition.root))
        result = np.broadcast_to(result, at.shape).copy() if np.ndim(result) == 0 else result.copy()
        if self.condition.uses_history:
            result &= previous >= 0
        return result

    # -- operands -----------------------------------------------------------------
    def _leaf_codes(self, leaf):
        key = (id(leaf.source), leaf.lag)
        if key not in self._codes:
            source = leaf.source
            targets = np.maximum(self.previous, 0) if leaf.lag else self.at
            runs = np.searchsorted(source.starts, targets, side="right") - 1
            self._codes[key] = np.asarray(source.codes[np.maximum(runs, 0)], dtype=np.int64)
        return self._codes[key]

    def _node(self, node):
        """-> ("leaf", SignalValue) | ("const", value) | ("bool", array or bool)."""
        kind = node[0]
        if kind == "signal":
            return ("leaf", node[1])
        if kind == "literal":
            return ("const", node[1])
        if kind == "not":
            return ("bool", ~self._truth(self._node(node[1])))
        if kind == "and":
            return ("bool", self._truth(self._node(node[1])) & self._truth(self._node(node[2])))
        if kind == "or":
            return ("bool", self._truth(self._node(node[1])) | self._truth(self._node(node[2])))
        return ("bool", self._compare(kind, self._node(node[1]), self._node(node[2])))

    def _truth(self, operand):
        from .conditions import _raw
        tag, value = operand
        if tag == "bool":
            return np.asarray(value, dtype=bool)
        if tag == "const":
            return np.bool_(truth_scalar(_raw(value, 0)))
        table = self.table(value)
        lut = table.lut(truth_scalar, lambda num: num != 0)
        return lut[self._leaf_codes(value)]

    def _points(self, operand):
        """Generic per-point view of any operand (``_raw`` semantics)."""
        from .conditions import _raw
        tag, value = operand
        n = len(self.at)
        if tag == "bool":
            array = np.broadcast_to(np.asarray(value, dtype=bool), (n,))
            return _Points(array.astype(np.int64), np.ones(n, dtype=bool), lambda i: bool(array[i]))
        if tag == "const":
            scalar = _raw(value, 0)
            ok = _fits(scalar) or type(scalar) is bool
            return _Points(np.full(n, int(scalar) if ok else 0, dtype=np.int64), np.full(n, ok),
                           lambda i: scalar)
        table, codes = self.table(value), self._leaf_codes(value)
        return _Points(table.num[codes], table.ok[codes], lambda i: table.object(int(codes[i])))

    def _compare(self, kind, left, right):
        from .conditions import _raw
        if left[0] == "leaf" and right[0] == "const":
            return self._compare_literal(kind, left[1], right[1], literal_right=True)
        if right[0] == "leaf" and left[0] == "const":
            return self._compare_literal(kind, right[1], left[1], literal_right=False)
        if left[0] == "const" and right[0] == "const":
            return np.bool_(compare_scalar(kind, _raw(left[1], 0), _raw(right[1], 0)))
        a, b = self._points(left), self._points(right)
        both = a.ok & b.ok
        result = np.zeros(len(self.at), dtype=bool)
        result[both] = _NUMERIC_OPS[kind](a.num[both], b.num[both])
        for index in np.flatnonzero(~both):
            result[index] = compare_scalar(kind, a.getter(int(index)), b.getter(int(index)))
        return result

    def _compare_literal(self, kind, leaf, literal, literal_right):
        """Signal against a literal: the literal takes the signal's radix (``_coerce``)."""
        try:
            parsed = leaf.parse_literal(literal.value)
        except ValueError:
            parsed = literal.value
        if literal_right:
            scalar = lambda value: compare_scalar(kind, value, parsed)
        else:
            scalar = lambda value: compare_scalar(kind, parsed, value)
        numeric = None
        if _fits(parsed):
            op = _NUMERIC_OPS[kind]
            numeric = (lambda num: op(num, parsed)) if literal_right else (lambda num: op(parsed, num))
        elif isinstance(parsed, str):
            # An int never equals a text, and relational operators need two numbers.
            constant = kind == "!="
            numeric = lambda num: np.full(len(num), constant)
        lut = self.table(leaf).lut(scalar, numeric)
        return lut[self._leaf_codes(leaf)]
