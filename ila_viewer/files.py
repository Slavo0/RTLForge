# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""Atomic file replacement through a unique sibling temporary file."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile


@contextmanager
def atomic_write(path, mode="wb", encoding=None):
    """Yield a stream to a unique temporary next to ``path``; replace ``path`` on success.

    The temporary name is created exclusively, so no existing neighbour file is ever
    truncated or removed, and two writers never share a temporary. On any exception the
    destination keeps its previous content.
    """
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, mode, encoding=encoding) as stream:
            yield stream
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def same_file(first, second):
    """True when both paths name the same existing file (or resolve to the same path)."""
    first, second = Path(first), Path(second)
    try:
        if first.exists() and second.exists():
            return os.path.samefile(first, second)
    except OSError:
        pass
    return os.path.normcase(str(first.resolve())) == os.path.normcase(str(second.resolve()))
