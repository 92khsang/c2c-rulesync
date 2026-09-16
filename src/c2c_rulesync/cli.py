"""Command-line entry point.

``c2c-rulesync hook`` is the only mode Codex runs. It must never block a tool
call, so every path through it exits 0 and writes either nothing or exactly one
JSON object to stdout. The other commands are for people and may exit 1, but no
command exits 2: Codex treats exit 2 with stderr output from a ``PreToolUse``
hook as a request to block the tool call.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import sys
from collections.abc import Callable
from types import FrameType
from typing import NoReturn

USAGE = """\
Usage: c2c-rulesync hook
       c2c-rulesync --version
       c2c-rulesync --help

Commands:
  hook       Read a Codex hook payload from stdin and write the hook output to stdout.

Options:
  --version  Print the version and exit.
  --help     Print this help and exit.
"""

# Codex waits for the hook before running the tool, so the hook gives up well
# before the recommended 10 second handler timeout.
HOOK_DEADLINE_SECONDS = 5.0


class HookDeadlineExceeded(BaseException):
    """Raised by the SIGALRM handler when the hook runs past its deadline."""


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["hook"]:
        run_hook_mode()
    if args in (["--help"], ["-h"]):
        sys.stdout.write(USAGE)
        return 0
    if args in (["--version"], ["-V"]):
        sys.stdout.write(f"c2c-rulesync {_version()}\n")
        return 0

    problem = "missing command" if not args else f"unknown arguments: {' '.join(args)}"
    sys.stderr.write(f"c2c-rulesync: {problem}\n\n{USAGE}")
    return 1


def run_hook_mode() -> NoReturn:
    """Run one hook invocation and terminate the process with status 0."""
    try:
        try:
            _arm_deadline()
            hook_stdout = _isolate_stdout()
            payload = sys.stdin.buffer.read()

            def emit(output: bytes) -> None:
                # Output that has started must not be cut short by the deadline.
                _disarm_deadline()
                _write_fully(hook_stdout, output)

            handle_payload(payload, emit)
        except BaseException as error:  # the hook must fail open on everything
            _report(error)
        finally:
            _disarm_deadline()
            _flush_quietly()
    except BaseException:
        # The one-shot deadline can still fire while the block above unwinds;
        # once it has fired or been disarmed, nothing else can interrupt this.
        pass
    finally:
        os._exit(0)


def handle_payload(payload: bytes, emit: Callable[[bytes], None]) -> None:
    """Handle a raw hook payload, passing the one JSON object to print, if any, to ``emit``."""
    from c2c_rulesync.hook import run_hook

    run_hook(payload, emit)


def _isolate_stdout() -> int:
    """Keep a private handle on stdout and send every other stdout write elsewhere.

    Codex parses the whole of stdout as one JSON object, and on ``SessionStart``
    it treats plain text as model context, so nothing but the hook output may
    reach the real stdout. Stray writes go to stderr, or to the null device when
    stderr is closed.

    Raises:
        OSError: stdout is not open.
    """
    private = fcntl.fcntl(1, fcntl.F_DUPFD_CLOEXEC, 3)
    try:
        os.fstat(2)
    except OSError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)
    else:
        os.dup2(2, 1)
    return private


def _arm_deadline() -> None:
    if hasattr(signal, "setitimer"):
        signal.signal(signal.SIGALRM, _deadline_exceeded)
        signal.setitimer(signal.ITIMER_REAL, HOOK_DEADLINE_SECONDS)


def _disarm_deadline() -> None:
    if hasattr(signal, "setitimer"):
        signal.setitimer(signal.ITIMER_REAL, 0)


def _deadline_exceeded(signum: int, frame: FrameType | None) -> None:
    del signum, frame
    raise HookDeadlineExceeded(f"hook exceeded its {HOOK_DEADLINE_SECONDS:g}s deadline")


def _write_fully(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _report(error: BaseException) -> None:
    # Reporting must not raise either.
    with contextlib.suppress(BaseException):
        sys.stderr.write(f"c2c-rulesync: {type(error).__name__}: {error}\n")


def _flush_quietly() -> None:
    # A closed pipe must not change the exit status.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(BaseException):
            stream.flush()


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("c2c-rulesync")
    except PackageNotFoundError:
        return "unknown"
