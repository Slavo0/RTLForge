# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Safe value and sampled-event conditions; never evaluates user Python code."""
from dataclasses import dataclass, replace
import re

from .bits import BitCodec
from .formats import FORMATS

# A bit selection after a name: data[3], data[1:0], data[7,5,4,2-0].
_SELECT = r"(?:\[[\d\s,:\-]+\])?"
_TOKEN = re.compile(r"\s*(?:(?P<op>&&|\|\||==|!=|<=|>=|[()!~<>=])|(?P<quoted>`[^`]+`" + _SELECT +
                    r")|(?P<word>d-\d+|[A-Za-z_][\w$]*" + _SELECT +
                    r"|0[xX][\da-fA-F]+|0[bB][01]+|0[oO][0-7]+|[\da-fA-F]+|-?\d+))", re.IGNORECASE)
_EXPLICIT = re.compile(r"(?i)(h[0-9a-f][0-9a-f_]*|b[01][01_]*|d-?[0-9][0-9_]*|0x[0-9a-f_]+|0b[01_]+|0o[0-7_]+)")
_SELECTED_NAME = re.compile(r"(?P<base>.+?)\[(?P<selection>[\d\s,:\-]+)\]")
_SIGNED = {"SIGNED", "SIGNED_MAGNITUDE"}


def bit_selection(signal, text):
    """Physical bit labels in written order -> offsets, MSB of the result first.

    ``7,5,4,2-0`` and ``7:4`` follow Verilog concatenation order; ``0-2`` puts bit 0 first.
    """
    labels = BitCodec.labels(signal)
    offsets = {label: signal.width - 1 - index for index, label in enumerate(labels)}
    chosen = []
    for part in text.split(","):
        match = re.fullmatch(r"\s*(\d+)\s*(?:[:\-]\s*(\d+))?\s*", part)
        if not match:
            raise ValueError(f"Неверный выбор битов [{text}]: используйте [3], [7:4] или [7,5,2-0]")
        first = int(match[1])
        last = int(match[2]) if match[2] is not None else first
        step = 1 if last >= first else -1
        for label in range(first, last + step, step):
            if label not in offsets:
                raise ValueError(f"Бит {label} вне сигнала {signal.short_name} "
                                 f"(доступны {labels[0]}…{labels[-1]})")
            if offsets[label] in chosen:
                raise ValueError(f"Бит {label} указан в [{text}] дважды")
            chosen.append(offsets[label])
    return tuple(chosen)


@dataclass(frozen=True)
class SignalValue:
    source: object
    radix: str
    lag: int = 0
    bits: tuple | None = None  # selected physical offsets; None means the whole signal

    @property
    def width(self):
        return len(self.bits) if self.bits is not None else self.source.width

    @property
    def effective(self):
        effective = FORMATS.effective(self.radix, self.source.radix)
        # A bit selection is a plain bit pattern: it has no sign bit of its own.
        return "UNSIGNED" if self.bits is not None and effective in _SIGNED else effective

    def _select(self, value):
        if self.bits is None:
            return value
        if isinstance(value, int):
            packed = 0
            for offset in self.bits:
                packed = (packed << 1) | ((value >> offset) & 1)
            return packed
        pattern = BitCodec.binary(value, self.source.width, self.source.radix)
        picked = "".join(pattern[self.source.width - 1 - offset] for offset in self.bits)
        return int(picked, 2) if all(c in "01" for c in picked) else picked

    def _signed(self, bits):
        width = self.width
        if self.effective == "SIGNED" and bits & (1 << (width - 1)):
            return bits - (1 << width)
        if self.effective == "SIGNED_MAGNITUDE" and bits & (1 << (width - 1)):
            return -(bits & ((1 << (width - 1)) - 1))
        return bits

    def at(self, sample, previous_sample=None):
        target = previous_sample if self.lag else sample
        if target is None:
            return None
        value = self._select(self.source.value_at(target))
        if not isinstance(value, int):
            return value
        return self._signed(value & ((1 << self.width) - 1))

    def parse_literal(self, token):
        width = self.width
        explicit = _explicit_number(token)
        if explicit is not None:
            pattern = not token.lower().startswith(("d", "-"))
            if self.effective in _SIGNED and pattern:
                # h/b/0x literals are bit patterns: hFF on a signed byte means -1.
                if not 0 <= explicit < (1 << width):
                    raise ValueError(f"Значение {token} не помещается в {width} бит")
                return self._signed(explicit)
            minimum = -(1 << (width - 1)) if self.effective in _SIGNED else 0
            maximum = (1 << (width - 1)) if self.effective in _SIGNED else (1 << width)
            if not minimum <= explicit < maximum:
                raise ValueError(f"Значение {token} не помещается в {width} бит")
            return explicit
        radix = self.radix if self.bits is None else self.effective
        source_radix = self.source.radix if self.bits is None else "BINARY"
        bits = FORMATS.parse(token, width, radix, source_radix=source_radix)
        if isinstance(bits, str):
            return bits
        return self._signed(bits & ((1 << width) - 1))


@dataclass(frozen=True)
class Literal:
    value: str


def _explicit_number(text):
    token = text.replace("_", "")
    if not _EXPLICIT.fullmatch(text):
        return None
    prefix = token[0].lower()
    if prefix in "hbd" and not token.lower().startswith("0"):
        return int(token[1:], {"h": 16, "b": 2, "d": 10}[prefix])
    return int(token, 0)


def _raw(value, sample, previous_sample=None):
    if isinstance(value, SignalValue): return value.at(sample, previous_sample)
    if isinstance(value, Literal):
        text = value.value
        try:
            explicit = _explicit_number(text)
            if explicit is not None: return explicit
            if re.fullmatch(r"-?\d+", text): return int(text, 10)
            if re.fullmatch(r"[\da-fA-F]+", text) and re.search(r"[a-fA-F]", text): return int(text, 16)
        except ValueError:
            pass
        return text
    return value


def _truth(value, sample, previous_sample=None):
    value = _raw(value, sample, previous_sample)
    return isinstance(value, (int, float)) and value != 0


def _coerce(value, other, sample, previous_sample=None):
    if isinstance(value, SignalValue) and isinstance(other, Literal):
        try:
            return value.at(sample, previous_sample), value.parse_literal(other.value)
        except ValueError:
            return value.at(sample, previous_sample), other.value
    if isinstance(other, SignalValue) and isinstance(value, Literal):
        try:
            return other.parse_literal(value.value), other.at(sample, previous_sample)
        except ValueError:
            return value.value, other.at(sample, previous_sample)
    return _raw(value, sample, previous_sample), _raw(other, sample, previous_sample)


class Condition:
    def __init__(self, expression, root):
        self.expression, self.root = expression, root
        self.uses_history = self._uses_history(root)
        self._vector = None

    def evaluate(self, at, previous):
        """``matches`` for arrays of samples; ``previous`` holds -1 where there is no previous point."""
        if self._vector is None:
            from .condition_vectors import VectorEvaluator
            self._vector = VectorEvaluator(self)
        return self._vector.evaluate(at, previous)

    @classmethod
    def _uses_history(cls, node):
        if node[0] == "signal":
            return node[1].lag > 0
        if node[0] == "literal":
            return False
        return any(cls._uses_history(child) for child in node[1:])

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

    def matches(self, sample, previous_sample=None):
        if self.uses_history and previous_sample is None:
            return False
        def value(node):
            kind = node[0]
            if kind == "signal":
                return node[1]
            if kind == "literal":
                return node[1]
            if kind == "not":
                return not _truth(value(node[1]), sample, previous_sample)
            if kind == "and":
                return _truth(value(node[1]), sample, previous_sample) and _truth(value(node[2]), sample, previous_sample)
            if kind == "or":
                return _truth(value(node[1]), sample, previous_sample) or _truth(value(node[2]), sample, previous_sample)
            left, right = _coerce(value(node[1]), value(node[2]), sample, previous_sample)
            try:
                if kind in {"<", ">", "<=", ">="} and not all(isinstance(x, (int, float)) for x in (left, right)):
                    return False
                return {"==": lambda: left == right, "!=": lambda: left != right,
                        "<": lambda: left < right, ">": lambda: left > right,
                        "<=": lambda: left <= right, ">=": lambda: left >= right}[kind]()
            except (TypeError, ValueError):
                return False
        return _truth(value(self.root), sample, previous_sample)


@dataclass(frozen=True)
class AdvancedPattern:
    """Each condition is checked on the next calibrated sample, not every CSV row."""
    expression: str
    stages: tuple[Condition, ...]

    def signal_names(self):
        return frozenset().union(*(stage.signal_names() for stage in self.stages))


class ConditionCompiler:
    """Resolves short signal names and backtick-quoted full names against a capture."""
    def __init__(self, signals, radix_by_name=None):
        self.signals = list(signals)
        self.radix_by_name = radix_by_name or {}
        self.by_name = {s.name: s for s in self.signals}
        self.by_full = {}
        for signal in self.signals:
            self.by_full.setdefault(signal.name.casefold(), []).append(signal)
        self.by_exact = {}
        self.by_short = {}
        for signal in self.signals:
            short = signal.short_name
            self.by_exact.setdefault(short.casefold(), []).append(signal)
            aliases = {short, re.sub(r"\[\d+(?::\d+)?\]$", "", short)}
            stem = re.sub(r"\[\d+(?::\d+)?\]$", "", short)
            aliases.update(stem[index + 1:] for index, char in enumerate(stem) if char == "_")
            for alias in aliases:
                self.by_short.setdefault(alias.casefold(), []).append(signal)

    def token_for(self, signal):
        matches = self.choices_for(signal.short_name)
        simple = re.fullmatch(r"[A-Za-z_]\w*(?:\[\d+(?::\d+)?\])?", signal.short_name)
        if simple and len(matches) == 1 and not self.looks_literal(signal.short_name):
            return signal.short_name
        return f"`{signal.name}`"

    @staticmethod
    def looks_literal(name):
        """A bare name the parser would read as a number or keyword (b1, hFF, abc, true)."""
        stem = re.sub(r"\[[^\]]*\]$", "", name)
        return bool(_EXPLICIT.fullmatch(stem) or re.fullmatch(r"[\da-fA-F]+", stem)
                    or stem.casefold() in {"true", "false", "prev"})

    def full_name(self, name):
        """Exact full name first; a case-insensitive match only when it is unique."""
        if name in self.by_name:
            return self.by_name[name]
        choices = self.by_full.get(name.casefold(), [])
        if len(choices) > 1:
            raise ValueError(f"Имя `{name}` совпадает с несколькими сигналами без учёта регистра; "
                             "укажите точный регистр")
        return choices[0] if choices else None

    def choices_for(self, name):
        """Prefer an exact short name over suffix aliases from other signals."""
        key = name.casefold()
        exact = self.by_exact.get(key, [])
        if exact:
            return exact
        choices = self.by_short.get(key, [])
        # A split VCD bus has physical bit declarations plus the assembled bus.
        # Its bare stem should name the bus, not an ambiguous set of bits.
        buses = [signal for signal in choices if signal.width > 1 and
                 re.sub(r"\[\d+:\d+\]$", "", signal.short_name).casefold() == key]
        return buses or choices

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

    @staticmethod
    def split_sequence(expression):
        parts = []
        start = depth = 0
        quoted = False
        index = 0
        while index < len(expression):
            char = expression[index]
            if char == "`":
                quoted = not quoted
            elif not quoted:
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                elif depth == 0 and expression[index:index + 2] == "->":
                    parts.append(expression[start:index].strip())
                    index += 2
                    start = index
                    continue
            index += 1
        parts.append(expression[start:].strip())
        if len(parts) == 1 and "\n" in expression:
            parts = [line.strip() for line in expression.splitlines()]
        return parts

    def compile_pattern(self, expression):
        parts = self.split_sequence(expression)
        if not 2 <= len(parts) <= 16:
            raise ValueError("Advanced Highlight требует от 2 до 16 последовательных шагов")
        if not all(parts):
            raise ValueError("Заполните условие каждого шага Advanced Highlight")
        return AdvancedPattern(expression, tuple(self.compile(part) for part in parts))

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
            op = self._take()
            right = self._unary()
            for signal_node, literal_node in ((node, right), (right, node)):
                if signal_node[0] == "signal" and literal_node[0] == "literal":
                    signal_node[1].parse_literal(literal_node[1].value)
            node = ("==" if op == "=" else op, node, right)
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
        if token.casefold() == "prev" and self._peek("("):
            self._take()
            inner = self._unary()
            if inner[0] != "signal" or inner[1].lag:
                raise ValueError("prev(...) принимает имя одного сигнала")
            if not self._peek(")"):
                raise ValueError("Не закрыта скобка prev(...)")
            self._take()
            return ("signal", replace(inner[1], lag=1))
        if token in ("&&", "||", ")"):
            raise ValueError(f"Неожиданный элемент: {token}")
        quoted = token.startswith("`")
        selection = None
        if quoted:
            closing = token.rindex("`")
            name, suffix = token[1:closing], token[closing + 1:]
            selection = suffix[1:-1] if suffix else None
        else:
            name = token
        if not quoted and _EXPLICIT.fullmatch(name):
            return ("literal", Literal(name))
        signal = self.full_name(name) if quoted else None
        lag = 0
        if not quoted:
            choices = self.choices_for(name)
            if len(choices) == 1:
                signal = choices[0]
            elif len(choices) > 1:
                raise ValueError(f"Имя {name} неоднозначно; выберите его в списке сигналов")
            if signal is None and name.casefold().endswith("_prev"):
                choices = self.choices_for(name[:-5])
                if len(choices) == 1:
                    signal, lag = choices[0], 1
                elif len(choices) > 1:
                    raise ValueError(f"Имя {name[:-5]} неоднозначно; используйте prev(`полное_имя`)")
            # An exact name such as a split VCD bit rx_data[3] wins; otherwise
            # data[3:0] selects bits of data.
            selected = _SELECTED_NAME.fullmatch(name) if signal is None else None
            if selected:
                choices = self.choices_for(selected["base"])
                if len(choices) > 1:
                    raise ValueError(f"Имя {selected['base']} неоднозначно; используйте `полное_имя`[{selected['selection']}]")
                if choices:
                    signal, selection = choices[0], selected["selection"]
        if signal:
            radix = self.radix_by_name.get(signal.name, "HEX")
            bits = bit_selection(signal, selection) if selection is not None else None
            return ("signal", SignalValue(signal, radix, lag, bits))
        if quoted:
            raise ValueError(f"Сигнал {name} не найден")
        if name.lower() in ("true", "false"):
            return ("literal", Literal("1" if name.lower() == "true" else "0"))
        if re.fullmatch(r"(?:0[xX][\da-fA-F]+|0[bB][01]+|0[oO][0-7]+|-?\d+|[\da-fA-F]+)", name):
            return ("literal", Literal(name))
        raise ValueError(f"Сигнал {name} не найден")
