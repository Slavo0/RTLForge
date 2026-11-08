# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Command sets of serial flash devices: built-in ones and JSON files (no Qt).

A command set is the decoder's command table plus the effects each command has on the
device state. The file format (UTF-8 JSON) mirrors the datasheet command table::

    {
      "format": "spi-command-set", "version": 1,
      "name": "Micron MT25QL128ABA",
      "source": "datasheet, Table 18",                    (optional)
      "commands": [
        {"code": "EB", "name": "QUAD INPUT/OUTPUT FAST READ",
         "extended": "1-4-4", "quad": "4-4-4", "address": 3, "dummy": [10, 0, 10],
         "data": "read", "dummy_setting": true, "xip": true},
        {"code": "35", "name": "ENTER QUAD I/O MODE", "extended": "1-0-0", "dual": "2-0-0",
         "quad": "4-0-0", "effects": [{"set": "protocol", "value": "quad"}]},
        {"code": "61", "name": "WRITE ENHANCED VOLATILE CONFIGURATION REGISTER",
         "extended": "1-0-1", "data": "write",
         "effects": [{"set": "protocol", "byte": 0, "bits": "7:6",
                      "values": {"00": "quad", "01": "quad", "10": "dual", "11": "extended"}}]}
      ]
    }

Command keys: ``code`` (``"EB"``, ``"0xEB"``, ``"EBh"``, a number, or a list of codes for
aliases such as ``["C7", "60"]``), ``name``, ``extended``/``dual``/``quad`` (format
``command-address-data``; an absent protocol scales the first given format), ``address``
(bytes), ``dummy`` (one number or ``[extended, dual, quad]``), ``data`` (``read``,
``write`` or ``none``), ``dtr``, ``dummy_setting`` (the dummy follows the dialog setting
and register writes), ``xip`` (IO0 on the first dummy clock is the XIP confirmation bit).

Effect keys: ``set`` (``protocol``, ``dtr``, ``dummy``, ``address_bytes``, ``xip``,
``reset``), and either ``value`` or ``bits`` (``"7:6"`` or ``"3"`` of data byte ``byte``,
default 0) with ``values`` keyed by the field in binary. ``after`` names the command
that must come right before (``"after": "66"`` for RESET MEMORY). Unknown keys are errors,
so a misspelt key never silently changes the decoding.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

from .files import atomic_write
from .spi import BusWidths, CommandEffect, CommandSpec, DIRECTIONS, default_commands

FORMAT = "spi-command-set"
VERSION = 1
PROFILE_DIR = Path(__file__).with_name("assets") / "spi"
GENERIC_NAME = "Общий набор NOR"

_COMMAND_KEYS = {"code", "name", "extended", "dual", "quad", "address", "dummy", "data", "dtr",
                 "dummy_setting", "xip", "effects", "comment"}
_EFFECT_KEYS = {"set", "value", "byte", "bits", "values", "after", "comment"}
_FILE_KEYS = {"format", "version", "name", "source", "comment", "commands"}


@dataclass(frozen=True)
class CommandSet:
    name: str
    commands: tuple
    source: str = ""
    path: Path | None = None


class CommandSetError(ValueError):
    pass


def _code(value, where):
    if isinstance(value, bool):
        raise CommandSetError(f"{where}: код команды — HEX, например \"EB\"")
    if isinstance(value, int):
        code = value
    else:
        text = str(value).strip()
        match = re.fullmatch(r"(?:0x)?([0-9a-fA-F]{1,2})h?", text)
        if not match:
            raise CommandSetError(f"{where}: код «{text}» — ожидается HEX 00…FF, например \"EB\"")
        code = int(match[1], 16)
    if not 0 <= code <= 0xFF:
        raise CommandSetError(f"{where}: код {code} вне 00…FF")
    return code


def _int(value, where, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CommandSetError(f"{where}: ожидается целое {low}…{high}, а не {value!r}")
    return value


def _bool(value, where):
    if not isinstance(value, bool):
        raise CommandSetError(f"{where}: ожидается true или false, а не {value!r}")
    return value


def _unknown_keys(data, allowed, where):
    extra = sorted(set(data) - allowed)
    if extra:
        raise CommandSetError(f"{where}: неизвестные ключи {', '.join(extra)}; допустимы "
                              + ", ".join(sorted(allowed - {'comment'})))


def _effect(data, where):
    if not isinstance(data, dict):
        raise CommandSetError(f"{where}: действие — объект {{\"set\": …}}")
    _unknown_keys(data, _EFFECT_KEYS, where)
    if "set" not in data:
        raise CommandSetError(f"{where}: нет ключа \"set\"")
    target = data["set"]
    after = _code(data["after"], f"{where}, after") if "after" in data else None
    try:
        if "bits" not in data:
            if "values" in data or "byte" in data:
                raise CommandSetError(f"{where}: \"values\" и \"byte\" задаются вместе с \"bits\"")
            return CommandEffect(target, data.get("value"), after=after)
        if "value" in data:
            raise CommandSetError(f"{where}: либо \"value\", либо \"bits\" с \"values\"")
        match = re.fullmatch(r"\s*(\d)\s*(?::\s*(\d)\s*)?", str(data["bits"]))
        if not match:
            raise CommandSetError(f"{where}: bits — \"7:6\" или \"3\"")
        high = int(match[1])
        low = int(match[2]) if match[2] is not None else high
        width = high - low + 1
        values = data.get("values", {})
        if not isinstance(values, dict):
            raise CommandSetError(f"{where}: values — объект {{\"00\": …}}")
        pairs = []
        for key, value in values.items():
            if not re.fullmatch(f"[01]{{{max(1, width)}}}", str(key)):
                raise CommandSetError(f"{where}: ключ «{key}» — {width} бит(а) в двоичном виде, "
                                      f"как поле {high}:{low}")
            pairs.append((int(key, 2), value))
        byte = _int(data.get("byte", 0), f"{where}, byte", 0, 255)
        return CommandEffect(target, None, byte, (high, low), tuple(pairs), after)
    except CommandSetError:
        raise
    except ValueError as exc:
        raise CommandSetError(f"{where}: {exc}") from None


def commands_from_list(items, where="таблица"):
    """CommandSpec tuple from parsed JSON command objects; errors name the entry."""
    if not isinstance(items, list) or not items:
        raise CommandSetError(f"{where}: \"commands\" — непустой список команд")
    commands, seen = [], {}
    for number, data in enumerate(items, 1):
        here = f"{where}, команда {number}"
        if not isinstance(data, dict):
            raise CommandSetError(f"{here}: ожидается объект {{\"code\": …}}")
        if "code" in data and not isinstance(data["code"], list):
            here += f" ({data['code']})"
        _unknown_keys(data, _COMMAND_KEYS, here)
        if "code" not in data:
            raise CommandSetError(f"{here}: нет ключа \"code\"")
        codes = data["code"] if isinstance(data["code"], list) else [data["code"]]
        codes = [_code(code, here) for code in codes]
        formats = []
        for key in ("extended", "dual", "quad"):
            text = str(data.get(key) or "").strip()
            if text:
                try:
                    BusWidths.parse(text)
                except ValueError as exc:
                    raise CommandSetError(f"{here}, {key}: {exc}") from None
            formats.append(text)
        if not any(formats):
            raise CommandSetError(f"{here}: нужен формат хотя бы одного протокола (\"extended\": \"1-1-1\")")
        dummy = data.get("dummy", 0)
        dummy = [dummy] * 3 if not isinstance(dummy, list) else dummy
        if len(dummy) != 3:
            raise CommandSetError(f"{here}: dummy — число или [extended, dual, quad]")
        dummy = tuple(_int(value, f"{here}, dummy", 0, 255) for value in dummy)
        direction = data.get("data", "none")
        if direction not in DIRECTIONS:
            raise CommandSetError(f"{here}: data — read, write или none, а не {direction!r}")
        effects = data.get("effects", [])
        if not isinstance(effects, list):
            raise CommandSetError(f"{here}: effects — список действий")
        effects = tuple(_effect(effect, f"{here}, действие {index}") for index, effect in enumerate(effects, 1))
        name = str(data.get("name") or "").strip()
        for code in codes:
            if code in seen:
                raise CommandSetError(f"{here}: код {code:02X}h уже описан в команде {seen[code]}")
            seen[code] = number
            commands.append(CommandSpec(
                code, name or f"CMD {code:02X}", tuple(formats), _int(data.get("address", 0), f"{here}, address", 0, 4),
                dummy, direction, _bool(data.get("dtr", False), f"{here}, dtr"),
                _bool(data.get("dummy_setting", False), f"{here}, dummy_setting"),
                _bool(data.get("xip", False), f"{here}, xip"), effects))
    return tuple(commands)


def _effect_dict(effect):
    data = {"set": effect.target}
    if effect.bits is None:
        if effect.target != "reset":
            data["value"] = effect.value
    else:
        high, low = effect.bits
        width = high - low + 1
        if effect.byte:
            data["byte"] = effect.byte
        data["bits"] = f"{high}:{low}" if high != low else str(high)
        data["values"] = {format(key, f"0{width}b"): value for key, value in effect.values}
    if effect.after is not None:
        data["after"] = f"{effect.after:02X}"
    return data


def command_dict(spec):
    data = {"code": f"{spec.opcode:02X}", "name": spec.name}
    for key, text in zip(("extended", "dual", "quad"), spec.formats):
        if text:
            data[key] = text
    if spec.address_bytes:
        data["address"] = spec.address_bytes
    if any(spec.dummy):
        data["dummy"] = list(spec.dummy) if len(set(spec.dummy)) > 1 else spec.dummy[0]
    if spec.direction != "none":
        data["data"] = spec.direction
    for key, value in (("dtr", spec.dtr), ("dummy_setting", spec.configurable_dummy), ("xip", spec.xip)):
        if value:
            data[key] = True
    if spec.effects:
        data["effects"] = [_effect_dict(effect) for effect in spec.effects]
    return data


def commands_to_list(commands):
    return [command_dict(spec) for spec in commands]


def load(path):
    """CommandSet from a JSON file; CommandSetError names the file and the entry."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise CommandSetError(f"{path.name}: не удалось прочитать ({exc.strerror or exc})") from None
    except json.JSONDecodeError as exc:
        raise CommandSetError(f"{path.name}: ошибка JSON в строке {exc.lineno}, столбце {exc.colno}: {exc.msg}") \
            from None
    if not isinstance(data, dict):
        raise CommandSetError(f"{path.name}: ожидается объект с ключом \"commands\"")
    _unknown_keys(data, _FILE_KEYS, path.name)
    if data.get("format", FORMAT) != FORMAT:
        raise CommandSetError(f"{path.name}: format должен быть \"{FORMAT}\"")
    if data.get("version", VERSION) != VERSION:
        raise CommandSetError(f"{path.name}: версия {data.get('version')!r} не поддерживается (нужна {VERSION})")
    commands = commands_from_list(data.get("commands"), path.name)
    return CommandSet(str(data.get("name") or path.stem), commands, str(data.get("source") or ""), path)


def save(path, name, commands, source=""):
    data = {"format": FORMAT, "version": VERSION, "name": name}
    if source:
        data["source"] = source
    data["commands"] = commands_to_list(commands)
    text = json.dumps(data, ensure_ascii=False, indent=1)
    with atomic_write(Path(path), "w", encoding="utf-8") as stream:
        stream.write(text + "\n")


def builtin():
    """The generic table first, then the device files shipped in ``assets/spi``."""
    sets = [CommandSet(GENERIC_NAME, default_commands())]
    for path in sorted(PROFILE_DIR.glob("*.json")):
        sets.append(load(path))
    return sets
