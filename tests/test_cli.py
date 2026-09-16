"""Process-level contract of the command-line entry point."""

from __future__ import annotations

import subprocess
import sys

import pytest


def run_cli(*args: str, stdin: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "c2c_rulesync", *args],
        input=stdin,
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_version_prints_the_package_version() -> None:
    result = run_cli("--version")

    assert result.returncode == 0
    assert result.stdout == b"c2c-rulesync 0.1.0\n"


def test_help_prints_usage_to_stdout() -> None:
    result = run_cli("--help")

    assert result.returncode == 0
    assert result.stdout.startswith(b"Usage: c2c-rulesync hook")


@pytest.mark.parametrize("args", [(), ("--verbose",), ("hok",), ("--version", "extra")])
def test_invalid_invocations_exit_1_without_stdout(args: tuple[str, ...]) -> None:
    result = run_cli(*args)

    assert result.returncode == 1
    assert result.stdout == b""
    assert b"Usage: c2c-rulesync hook" in result.stderr


@pytest.mark.parametrize(
    "stdin",
    [
        b"",
        b"not json",
        b"\xff\xfe\x00 invalid utf-8",
        b'{"hook_event_name": "PreToolUse"}',
        b"[" * (2 * 1024 * 1024),
    ],
    ids=["empty", "not-json", "invalid-utf8", "minimal-payload", "two-mib"],
)
def test_hook_mode_exits_0_and_prints_nothing(stdin: bytes) -> None:
    result = run_cli("hook", stdin=stdin)

    assert result.returncode == 0
    assert result.stdout == b""


def test_hook_mode_ignores_extra_arguments() -> None:
    result = run_cli("hook", "--unexpected", "value", stdin=b"{}")

    assert result.returncode == 0
    assert result.stdout == b""
