# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""General application preferences; never stores capture or signal state."""
from dataclasses import asdict, dataclass, field, fields
import json
from pathlib import Path
import re

from .formats import FORMATS


@dataclass
class ViewerPreferences:
    theme: str = "dark"
    global_radix: str = "HEX"
    fill_high: bool = False
    full_names: bool = False
    name_width: int = 280
    value_width: int = 90
    layout_version: int = 2
    window_width: int = 1280
    window_height: int = 800
    maximized: bool = False
    copy_separator: str = "space"
    last_csv_directory: str = ""
    separate_signal_styles: bool = False
    fill_bus: bool = False
    signal_colors: dict = field(default_factory=dict)
    highlight_palette: list = field(default_factory=lambda: ["#ffcc74", "#61dfb5", "#da9cff", "#74b6ff"])
    highlight_opacities: list = field(default_factory=lambda: [28, 28, 28, 28])
    restore_highlight_visibility: bool = True
    hide_empty_highlight_panel: bool = False


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
        if data.get("layout_version") != 2 and data.get("value_width") == 120:
            result.value_width = 90
        result.layout_version = 2
        valid_color = lambda color: isinstance(color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", color)
        result.signal_colors = {
            theme: {kind: color for kind, color in colors.items() if kind in {"bus", "bit"} and valid_color(color)}
            for theme, colors in result.signal_colors.items()
            if isinstance(theme, str) and isinstance(colors, dict)
        }
        result.highlight_palette = [color for color in result.highlight_palette if valid_color(color)][:12]
        if not result.highlight_palette:
            result.highlight_palette = ViewerPreferences().highlight_palette
        result.highlight_opacities = [max(0, min(100, opacity)) for opacity in result.highlight_opacities
                                      if type(opacity) is int][:len(result.highlight_palette)]
        result.highlight_opacities.extend([28] * (len(result.highlight_palette) - len(result.highlight_opacities)))
        if result.global_radix not in {"DEFAULT", *FORMATS.formats} or result.global_radix == "REAL":
            result.global_radix = "HEX"
        if result.copy_separator not in {"space", "comma", "text", "tab", "newline"}:
            result.copy_separator = "space"
        if result.last_csv_directory and not Path(result.last_csv_directory).is_dir():
            result.last_csv_directory = ""
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
