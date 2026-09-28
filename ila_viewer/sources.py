# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Data-source boundary: a future simulator can implement CaptureSource."""
from abc import ABC, abstractmethod
from .data import load_csv


class CaptureSource(ABC):
    @abstractmethod
    def load(self, location, progress, cancel):
        """Return an owned Capture. UI must not depend on its origin."""


class VivadoCsvSource(CaptureSource):
    def load(self, location, progress=lambda p, n: None, cancel=lambda: False):
        return load_csv(location, progress, cancel)


class SourceNavigator(ABC):
    @abstractmethod
    def available(self, signal) -> bool: ...

    @abstractmethod
    def go_to(self, signal): ...


class UnconfiguredSourceNavigator(SourceNavigator):
    """Extension point for HDL file/line mapping; never guesses file locations."""
    def available(self, signal):
        return False

    def go_to(self, signal):
        raise NotImplementedError("Связь сигналов с исходным HDL ещё не настроена")
