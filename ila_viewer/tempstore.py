# Copyright 2026 Вячеслав Рудаков
# SPDX-License-Identifier: Apache-2.0

"""The application's temporary folders, and removal of those left by a dead process; Qt-free.

Every folder is created through ``TempStorage.create``. It holds a lock file that the
creating process keeps open and locked for the folder's lifetime (``msvcrt.locking``
on Windows, ``fcntl.flock`` elsewhere). The operating system releases the lock when
the process ends in any way, so a folder whose lock can be taken has no live owner.
A process id alone would not prove that: ids are reused. Pool workers write into the
parent's folders; only the parent holds the lock.

``TempStorage.sweep`` removes stale folders among the direct children of the root
whose names start with ``PREFIXES``:

- with a lock file: when its lock can be taken;
- without one (made by earlier versions): when nothing in it changed for ``UNMARKED_AGE``
  seconds and it can be renamed. On Windows a folder that holds an open or mapped file
  cannot be renamed, so such a folder is left whole instead of being deleted in part.

Symbolic links and junctions are never followed, and nothing else in the root is
touched. Errors are recorded in the report, never raised.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
import uuid
import weakref

if os.name == "nt":
    import msvcrt
else:
    import fcntl

PREFIXES = ("ila_wlf_convert_", "ila_vcd_", "ila_wave_", "ila_wlf_export_")
LOCK_NAME = ".ila-owner.lock"
UNMARKED_AGE = 24 * 3600  # seconds without a change before an unmarked folder counts as stale


def _try_lock(stream):
    """Take an exclusive lock on ``stream`` without waiting; False when another holder has it."""
    try:
        if os.name == "nt":
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(stream):
    try:
        if os.name == "nt":
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _is_link(path_or_entry):
    """True for a symbolic link or a Windows junction (never followed)."""
    if isinstance(path_or_entry, os.DirEntry):
        entry = path_or_entry
        if entry.is_symlink():
            return True
        junction = getattr(entry, "is_junction", None)
        return bool(junction and junction())
    path = os.fspath(path_or_entry)
    return os.path.islink(path) or bool(getattr(os.path, "isjunction", lambda p: False)(path))


def _remove_tree(path):
    """Delete ``path`` bottom-up; links and junctions inside are removed, not followed."""
    with os.scandir(path) as entries:
        children = list(entries)
    for entry in children:
        if _is_link(entry):
            try:
                os.unlink(entry.path)
            except (IsADirectoryError, PermissionError):
                os.rmdir(entry.path)  # a junction or a directory symlink on Windows
        elif entry.is_dir(follow_symlinks=False):
            _remove_tree(entry.path)
        else:
            try:
                os.unlink(entry.path)
            except PermissionError:
                os.chmod(entry.path, stat.S_IWRITE)  # read-only files, as TemporaryDirectory does
                os.unlink(entry.path)
    os.rmdir(path)


class TempFolder:
    """An owned temporary folder: ``name`` and ``cleanup()`` like ``tempfile.TemporaryDirectory``."""

    def __init__(self, root, prefix):
        self.name = tempfile.mkdtemp(prefix=prefix, dir=root)
        self._lock = None
        try:
            self._lock = open(Path(self.name) / LOCK_NAME, "a+b")
            if not _try_lock(self._lock):
                raise OSError(f"cannot lock {self._lock.name}")
        except BaseException:
            self._release()
            shutil.rmtree(self.name, ignore_errors=True)
            raise
        self._finalizer = weakref.finalize(self, TempFolder._cleanup, self.name, self._lock)

    def _release(self):
        if self._lock is not None and not self._lock.closed:
            _unlock(self._lock)
            self._lock.close()

    @staticmethod
    def _cleanup(name, lock):
        # Remove the contents while the lock still marks the folder as owned, then the lock.
        # A failure leaves an unlocked folder, which the next start's sweep removes.
        try:
            folder = Path(name)
            if folder.is_dir():
                for entry in list(os.scandir(folder)):
                    if entry.name == LOCK_NAME:
                        continue
                    if _is_link(entry) or not entry.is_dir(follow_symlinks=False):
                        os.unlink(entry.path)
                    else:
                        _remove_tree(entry.path)
        finally:
            if not lock.closed:
                _unlock(lock)
                lock.close()
        if Path(name).is_dir():
            _remove_tree(name)

    def cleanup(self):
        if self._finalizer.detach():
            self._cleanup(self.name, self._lock)

    def __enter__(self):
        return self.name

    def __exit__(self, *exc):
        self.cleanup()

    def __repr__(self):
        return f"<TempFolder {self.name!r}>"


@dataclass
class SweepReport:
    removed: list = field(default_factory=list)    # folder names
    removed_bytes: int = 0
    kept: dict = field(default_factory=dict)       # folder name: reason
    errors: dict = field(default_factory=dict)     # folder name: error text


class TempStorage:
    """Creates the application's temporary folders under ``root`` and sweeps stale ones."""

    def __init__(self, root=None, prefixes=PREFIXES, unmarked_age=UNMARKED_AGE, clock=time.time):
        self._root = Path(root) if root is not None else None
        self.prefixes = tuple(prefixes)
        self.unmarked_age = unmarked_age
        self.clock = clock

    @property
    def root(self):
        return self._root if self._root is not None else Path(tempfile.gettempdir())

    def create(self, prefix, root=None):
        """A new locked folder; ``root`` overrides the storage root (WLF export next to its target)."""
        if not prefix.startswith(self.prefixes):
            raise ValueError(f"unknown temporary folder prefix {prefix!r}")
        return TempFolder(root if root is not None else self.root, prefix)

    # ------------------------------------------------------------------ sweep

    def sweep(self, cancel=lambda: False):
        report = SweepReport()
        try:
            with os.scandir(self.root) as entries:
                candidates = [entry for entry in entries if entry.name.startswith(self.prefixes)]
        except OSError as exc:
            report.errors[str(self.root)] = str(exc)
            return report
        for entry in candidates:
            if cancel():
                break
            try:
                if _is_link(entry) or not entry.is_dir(follow_symlinks=False):
                    report.kept[entry.name] = "not a folder"
                    continue
                reason = self._stale(Path(entry.path))
                if reason is not None:
                    report.kept[entry.name] = reason
                    continue
                size = self._size(entry.path)
                reason = self._remove(Path(entry.path))
                if reason is None:
                    report.removed.append(entry.name)
                    report.removed_bytes += size
                else:
                    report.kept[entry.name] = reason
            except Exception as exc:  # never stop the application
                report.errors[entry.name] = f"{type(exc).__name__}: {exc}"
        return report

    def _stale(self, folder):
        """None when ``folder`` may be removed, else the reason to keep it."""
        lock = folder / LOCK_NAME
        if lock.is_file() and not _is_link(lock):
            try:
                with open(lock, "r+b") as stream:
                    if not _try_lock(stream):
                        return "owner alive"
                    _unlock(stream)
            except PermissionError:
                return "owner alive"  # Windows: the owner's handle keeps the file in use
            return None
        if self.clock() - self._newest(folder) < self.unmarked_age:
            return "recent, unmarked"
        return None

    @staticmethod
    def _newest(folder):
        newest = os.stat(folder, follow_symlinks=False).st_mtime
        stack = [folder]
        while stack:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    newest = max(newest, info.st_mtime)
                    if entry.is_dir(follow_symlinks=False) and not _is_link(entry):
                        stack.append(entry.path)
        return newest

    @staticmethod
    def _size(folder):
        total = 0
        stack = [folder]
        while stack:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if _is_link(entry):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        total += entry.stat(follow_symlinks=False).st_size
        return total

    @staticmethod
    def _remove(folder):
        """Rename first: on Windows that fails while a live process has a file inside open or
        mapped, and then nothing is deleted. The new name keeps the prefix, so a folder whose
        removal stops halfway is found again by the next sweep."""
        moved = folder.with_name(f"{folder.name}.sweep-{uuid.uuid4().hex[:8]}")
        try:
            os.rename(folder, moved)
        except OSError:
            return "in use"
        try:
            _remove_tree(moved)
        except OSError as exc:
            return f"partly removed: {exc}"
        return None


DEFAULT = TempStorage()  # the system temporary folder; callers take a ``temp`` argument to override it


def storage_or_default(temp):
    return temp if temp is not None else DEFAULT
