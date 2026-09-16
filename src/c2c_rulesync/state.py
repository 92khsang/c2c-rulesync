"""Remember, per Codex session and thread, which rules a thread already received.

Layout under the state root::

    sessions/<session>/<thread>.json    what the thread received
    sessions/<session>/<thread>.epoch   changes when the thread's context is compacted
    sessions/<session>/<thread>.lock    serializes hook processes for the thread
    sweep                               when old sessions were last removed

Several hook processes can run for one thread at once: Codex runs tool calls
in parallel, and the same hook may be registered twice. A process holds the
thread's lock from reading the state until it has written its output and
replaced the state file, so each rule is delivered once.

Compaction removes injected rules from the conversation, so the thread must
receive them again. It is recorded by replacing the epoch file, without the
lock: a state file written for an older epoch counts as empty. A process that
is still finishing for the old epoch can then only cause a rule to be
delivered twice, never cause it to be lost.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Iterator, Mapping

__all__ = ["LockTimeout", "State", "ThreadState", "state_root", "sweep"]

STATE_VERSION = 1
LOCK_TIMEOUT_SECONDS = 2.0
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600
SWEEP_INTERVAL_SECONDS = 24 * 3600
# Warnings are shown once per thread; past this many the oldest are forgotten.
MAX_WARNINGS = 2000

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_STATE_SUFFIXES = (".json", ".epoch", ".lock", ".tmp")


class LockTimeout(Exception):
    """Another hook process held the thread's lock for too long."""


def state_root(environ: Mapping[str, str]) -> str | None:
    """The directory holding hook state, or ``None`` when none can be determined.

    ``C2C_RULESYNC_STATE_DIR`` wins, then ``$XDG_STATE_HOME/c2c-rulesync``, then
    ``~/.local/state/c2c-rulesync``. Relative values are ignored, because Codex
    runs hooks in the project directory.
    """
    configured = environ.get("C2C_RULESYNC_STATE_DIR", "")
    if os.path.isabs(configured):
        return configured
    xdg = environ.get("XDG_STATE_HOME", "")
    if os.path.isabs(xdg):
        return os.path.join(xdg, "c2c-rulesync")
    home = environ.get("HOME", "")
    if os.path.isabs(home):
        return os.path.join(home, ".local", "state", "c2c-rulesync")
    return None


class State:
    """What one thread received in the current epoch.

    Attributes:
        start_done: Whether the thread received its session start rules.
        loaded: Resolved paths of rule files the thread received or read.
        warned: Warnings already shown to the user.
    """

    __slots__ = ("loaded", "start_done", "warned")

    def __init__(self, start_done: bool, loaded: set[str], warned: list[str]) -> None:
        self.start_done = start_done
        self.loaded = loaded
        self.warned = warned


class ThreadState:
    """The state files of one thread of one session."""

    def __init__(self, root: str, session_id: str, thread: str) -> None:
        self.directory = os.path.join(root, "sessions", _file_name(session_id))
        base = os.path.join(self.directory, _file_name(thread))
        self.state_path = base + ".json"
        self.epoch_path = base + ".epoch"
        self.lock_path = base + ".lock"

    def new_epoch(self) -> None:
        """Mark the thread's context as compacted, so its state counts as empty."""
        os.makedirs(self.directory, exist_ok=True)
        _replace_file(self.epoch_path, str(time.time_ns()).encode() + os.urandom(4).hex().encode())

    def touch(self) -> None:
        """Mark the thread as in use, so that the sweep keeps its session."""
        with contextlib.suppress(FileNotFoundError):
            os.utime(self.lock_path)

    def forget(self) -> None:
        """Remove the thread's record, which then counts as empty.

        Raises:
            OSError: The record exists and cannot be removed.
        """
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.state_path)

    def peek(self) -> State:
        """Read the state without the lock; the result may be stale."""
        return self._read(self._epoch())

    @contextlib.contextmanager
    def locked(self) -> Iterator[Transaction]:
        """Hold the thread's lock and yield its state for an update.

        Raises:
            LockTimeout: The lock was not acquired within ``LOCK_TIMEOUT_SECONDS``.
            OSError: The state directory cannot be created or locked.
        """
        os.makedirs(self.directory, exist_ok=True)
        fd = _lock(self.lock_path, time.monotonic() + LOCK_TIMEOUT_SECONDS)
        transaction = None
        try:
            # A session in use is not swept, even when its record stays the same.
            with contextlib.suppress(OSError):
                os.utime(fd)
            epoch = self._epoch()
            transaction = Transaction(self, epoch, self._read(epoch))
            yield transaction
        finally:
            if transaction is not None:
                transaction.discard()
            os.close(fd)

    def _epoch(self) -> str:
        try:
            with open(self.epoch_path, "rb") as handle:
                return handle.read(64).decode("ascii", "replace")
        except OSError:
            return ""

    def _read(self, epoch: str) -> State:
        try:
            with open(self.state_path, "rb") as handle:
                data = json.loads(handle.read())
        except (OSError, ValueError, RecursionError):
            return State(False, set(), [])
        if (
            not isinstance(data, dict)
            or data.get("version") != STATE_VERSION
            or data.get("epoch") != epoch
        ):
            return State(False, set(), [])
        loaded = data.get("loaded")
        warned = data.get("warned")
        return State(
            data.get("start_done") is True,
            {path for path in loaded if isinstance(path, str)}
            if isinstance(loaded, list)
            else set(),
            [text for text in warned if isinstance(text, str)] if isinstance(warned, list) else [],
        )


class Transaction:
    """A state update under the thread's lock.

    ``stage`` writes the new state beside the state file, and ``commit``
    replaces the state file with it. Output goes between the two: a failed
    stage means no output, and output that was written but not committed only
    repeats later.
    """

    def __init__(self, thread: ThreadState, epoch: str, state: State) -> None:
        self.state = state
        self._thread = thread
        self._epoch = epoch
        self._staged: str | None = None

    def stage(self, state: State) -> None:
        """Write ``state`` to a temporary file.

        Raises:
            OSError: The file cannot be written.
        """
        document = {
            "version": STATE_VERSION,
            "epoch": self._epoch,
            "start_done": state.start_done,
            "loaded": sorted(state.loaded),
            "warned": state.warned[-MAX_WARNINGS:],
        }
        data = json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        self._staged = _write_temporary(self._thread.state_path, data)

    def commit(self) -> None:
        """Replace the state file with the staged state."""
        if self._staged is not None:
            os.replace(self._staged, self._thread.state_path)
            self._staged = None

    def discard(self) -> None:
        if self._staged is not None:
            with contextlib.suppress(OSError):
                os.unlink(self._staged)
            self._staged = None


def sweep(root: str, now: float | None = None) -> None:
    """Remove the state of sessions unused for a week, at most once a day.

    Only the hook's own files are removed: in each real directory under
    ``sessions``, files ending in ``.json``, ``.epoch``, ``.lock`` or ``.tmp``,
    and the directory itself once it is empty.
    """
    now = time.time() if now is None else now
    marker = os.path.join(root, "sweep")
    try:
        if now - os.lstat(marker).st_mtime < SWEEP_INTERVAL_SECONDS:
            return
    except FileNotFoundError:
        pass
    sessions = os.path.join(root, "sessions")
    try:
        names = os.listdir(sessions)
    except OSError:
        return
    _replace_file(marker, b"")
    os.utime(marker, (now, now))
    for name in names:
        with contextlib.suppress(OSError):
            _remove_stale_session(os.path.join(sessions, name), now)


def _remove_stale_session(directory: str, now: float) -> None:
    if not stat.S_ISDIR(os.lstat(directory).st_mode):
        return
    entries = [os.path.join(directory, name) for name in os.listdir(directory)]
    owned = []
    for entry in entries:
        info = os.lstat(entry)
        if now - info.st_mtime < SESSION_MAX_AGE_SECONDS:
            return
        if stat.S_ISREG(info.st_mode) and entry.endswith(_STATE_SUFFIXES):
            owned.append(entry)
    held = []
    try:
        for entry in owned:
            if entry.endswith(".lock"):
                fd = os.open(entry, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
                held.append(fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for entry in owned:
            os.unlink(entry)
        if len(owned) == len(entries):
            os.rmdir(directory)
    finally:
        for fd in held:
            os.close(fd)


def _file_name(value: str) -> str:
    if _SAFE_NAME.fullmatch(value) and value not in (".", ".."):
        return value
    digest = hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()
    return f"h-{digest[:40]}"


def _lock(path: str, deadline: float) -> int:
    delay = 0.005
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise
            if time.monotonic() >= deadline:
                raise LockTimeout(path) from None
            time.sleep(delay)
            delay = min(delay * 2, 0.05)
            continue
        # A sweep may have removed the file after it was opened; a lock on the
        # removed file excludes nobody.
        try:
            same = os.fstat(fd).st_ino == os.stat(path).st_ino
        except OSError:
            same = False
        if same:
            return fd
        os.close(fd)


def _write_temporary(path: str, data: bytes) -> str:
    temporary = f"{path}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    os.close(fd)
    return temporary


def _replace_file(path: str, data: bytes) -> None:
    temporary = _write_temporary(path, data)
    try:
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
