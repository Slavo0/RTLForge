# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""General application preferences; never stores capture or signal state."""
from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path

from .formats import FORMATS


@dataclass
class ViewerPreferences:
    theme: str = "dark"
    global_radix: str = "HEX"
    fill_high: bool = False
    full_names: bool = False
    name_width: int = 280
    value_width: int = 120
    window_width: int = 1280
    window_height: int = 800
    maximized: bool = False
    copy_separator: str = "space"
    copy_samples: str = "all"


class PreferencesStore:
    def __init__(self, path=None):
        """None creates an isolated in-memory store (tests / embedded viewers)."""
        self.path = Path(path) if path is not None else None
        self._memory = ViewerPreferences()
        self.error = None

    @classmethod
    def for_application(cls):
        return cls(Path(__file__).resolve().parents[1] / ".viewer-settings.json")

    def load(self):
        self.error = None
        if self.path is None:
            return ViewerPreferences(**asdict(self._memory))
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Настройки должны быть JSON-объектом")
        except FileNotFoundError:
            return ViewerPreferences()
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            return ViewerPreferences()
        result = ViewerPreferences()
        for field in fields(result):
            value = data.get(field.name)
            expected = type(getattr(result, field.name))
            if type(value) is expected:
                setattr(result, field.name, value)
        if result.global_radix not in {"DEFAULT", *FORMATS.formats} or result.global_radix == "REAL":
            result.global_radix = "HEX"
        if result.copy_separator not in {"space", "comma", "text", "tab", "newline"}:
            result.copy_separator = "space"
        if result.copy_samples not in {"all", "changes"}:
            result.copy_samples = "all"
        for key, low, high in (("name_width", 100, 10000), ("value_width", 60, 10000),
                               ("window_width", 400, 16000), ("window_height", 300, 16000)):
            setattr(result, key, max(low, min(high, getattr(result, key))))
        return result

    def save(self, preferences):
        self._memory = ViewerPreferences(**asdict(preferences))
        if self.path is None:
            return
        # Atomic replacement avoids a truncated settings file after interruption.
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(asdict(preferences), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
