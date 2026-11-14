# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Named phases of long operations whose total is unknown; Qt-free.

A capture source reports ``progress(percent, rows)`` as before; during a phase such as
the WLF conversion it calls ``progress(-1, amount, phase)`` instead, and the window
shows ``phase.describe(amount)`` with a busy indicator rather than a made-up percentage.
"""
from dataclasses import dataclass

from .diskspace import format_bytes


@dataclass(frozen=True)
class Phase:
    title: str

    def describe(self, amount):
        return f"{self.title}: {format_bytes(amount)}"


CONVERTING_WLF = Phase("Преобразование WLF")  # amount: bytes of VCD written so far
# The sliced conversion failed its seam check; the file is converted again in one run.
RECONVERTING_WLF = Phase("Быстрое преобразование отклонено, повторное преобразование WLF")
