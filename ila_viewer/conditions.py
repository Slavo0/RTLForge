# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Safe parser and evaluator for copy filters (never evaluates Python code)."""
from dataclasses import dataclass
import re


_TOKEN = re.compile(r"\s*(?:(?P<op>&&|\|\||==|!=|<=|>=|[()!~<>=])|(?P<quoted>`[^`]+`)|(?P<word>[A-Za-z_][\w$]*(?:\[\d+(?::\d+)?\])?|0[xX][\da-fA-F]+|0[bB][01]+|0[oO][0-7]+|\d+|[\da-fA-F]+))")


@dataclass(frozen=True)
class SignalValue:
    source: object
    radix: str

    def at(self, sample):
        return self.source.value_at(sample)


@dataclass(frozen=True)
class Literal:
    value: str


def _raw(value, sample):
    if isinstance(value, SignalValue): return value.at(sample)
    if isinstance(value, Literal):
        text = value.value
        try:
            if text.lower().startswith(("0x", "0b", "0o")): return int(text, 0)
            if text.isdigit(): return int(text, 10)
            if re.fullmatch(r"[\da-fA-F]+", text) and re.search(r"[a-fA-F]", text): return int(text, 16)
        except ValueError:
            pass
        return text
    return value


def _truth(value, sample):
    value = _raw(value, sample)
    return isinstance(value, int) and value != 0


def _coerce(value, other, sample):
    if isinstance(value, SignalValue) and isinstance(other, Literal):
        try:
            return value.source.query_value(other.value, value.radix), _raw(value, sample)
        except ValueError:
            return _raw(value, sample), other.value
    if isinstance(other, SignalValue) and isinstance(value, Literal):
        try:
            return _raw(other, sample), other.source.query_value(value.value, other.radix)
        except ValueError:
            return value.value, _raw(other, sample)
    return _raw(value, sample), _raw(other, sample)


class Condition:
    def __init__(self, expression, root):
        self.expression, self.root = expression, root

    def signal_names(self):
        names = set()
        def visit(node):
            if node[0] == "signal":
                names.add(node[1].source.name)
            elif node[0] == "not":
                visit(node[1])
            elif node[0] not in {"literal"}:
                visit(node[1])
                visit(node[2])
        visit(self.root)
        return frozenset(names)

    def matches(self, sample):
        def value(node):
            kind = node[0]
            if kind == "signal":
                return node[1]
            if kind == "literal":
                return node[1]
            if kind == "not":
                return not _truth(value(node[1]), sample)
            if kind == "and":
                return _truth(value(node[1]), sample) and _truth(value(node[2]), sample)
            if kind == "or":
                return _truth(value(node[1]), sample) or _truth(value(node[2]), sample)
            left, right = _coerce(value(node[1]), value(node[2]), sample)
            try:
                return {"==": lambda: left == right, "!=": lambda: left != right,
                        "<": lambda: left < right, ">": lambda: left > right,
                        "<=": lambda: left <= right, ">=": lambda: left >= right}[kind]()
            except (TypeError, ValueError):
                return False
        return _truth(value(self.root), sample)


class ConditionCompiler:
    """Resolves short signal names and backtick-quoted full names against a capture."""
    def __init__(self, signals, radix_by_name=None):
        self.signals = list(signals)
        self.radix_by_name = radix_by_name or {}
        self.by_full = {s.name.casefold(): s for s in self.signals}
        self.by_short = {}
        for signal in self.signals:
            short = signal.short_name
            aliases = {short, re.sub(r"\[\d+(?::\d+)?\]$", "", short)}
            stem = re.sub(r"\[\d+(?::\d+)?\]$", "", short)
            aliases.update(stem[index + 1:] for index, char in enumerate(stem) if char == "_")
            for alias in aliases:
                self.by_short.setdefault(alias.casefold(), []).append(signal)

    def token_for(self, signal):
        matches = self.by_short.get(signal.short_name.casefold(), [])
        simple = re.fullmatch(r"[A-Za-z_]\w*(?:\[\d+(?::\d+)?\])?", signal.short_name)
        if simple and len(matches) == 1:
            return signal.short_name
        return f"`{signal.name}`"

    def compile(self, expression):
        self.tokens = []
        pos = 0
        while pos < len(expression):
            match = _TOKEN.match(expression, pos)
            if not match:
                if expression[pos:].strip() == "": break
                raise ValueError(f"Не удалось разобрать условие около: {expression[pos:pos + 20]}")
            token = match.group("op") or match.group("quoted") or match.group("word")
            self.tokens.append(token)
            pos = match.end()
        self.pos = 0
        if not self.tokens:
            return None
        root = self._or()
        if self.pos != len(self.tokens):
            raise ValueError(f"Лишний элемент в условии: {self.tokens[self.pos]}")
        return Condition(expression, root)

    def _peek(self, *values):
        return self.pos < len(self.tokens) and self.tokens[self.pos] in values

    def _take(self):
        value = self.tokens[self.pos]
        self.pos += 1
        return value

    def _or(self):
        node = self._and()
        while self._peek("||"):
            self._take(); node = ("or", node, self._and())
        return node

    def _and(self):
        node = self._compare()
        while self._peek("&&"):
            self._take(); node = ("and", node, self._compare())
        return node

    def _compare(self):
        node = self._unary()
        if self._peek("=", "==", "!=", "<", ">", "<=", ">="):
            op = self._take(); node = ("==" if op == "=" else op, node, self._unary())
        return node

    def _unary(self):
        if self._peek("!", "~"):
            self._take(); return ("not", self._unary())
        if self._peek("("):
            self._take(); node = self._or()
            if not self._peek(")"):
                raise ValueError("Не закрыта скобка в условии")
            self._take(); return node
        if self.pos >= len(self.tokens):
            raise ValueError("Не хватает значения в условии")
        token = self._take()
        if token in ("&&", "||", ")"):
            raise ValueError(f"Неожиданный элемент: {token}")
        quoted = token.startswith("`")
        name = token[1:-1] if quoted else token
        signal = self.by_full.get(name.casefold()) if quoted else None
        if not quoted:
            choices = self.by_short.get(name.casefold(), [])
            if len(choices) == 1:
                signal = choices[0]
            elif len(choices) > 1:
                raise ValueError(f"Имя {name} неоднозначно; выберите его в списке сигналов")
        if signal:
            radix = self.radix_by_name.get(signal.name, "HEX")
            return ("signal", SignalValue(signal, radix))
        if quoted:
            raise ValueError(f"Сигнал {name} не найден")
        if name.lower() in ("true", "false"):
            return ("literal", Literal("1" if name.lower() == "true" else "0"))
        if re.fullmatch(r"(?:0[xX][\da-fA-F]+|0[bB][01]+|0[oO][0-7]+|\d+|[\da-fA-F]+)", name):
            return ("literal", Literal(name))
        raise ValueError(f"Сигнал {name} не найден")
