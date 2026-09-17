"""Process-level contract of the command-line entry point."""

from __future__ import annotations

import os
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
    assert result.stdout == b"c2c-rulesync 0.3.0\n"


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


def run_hook_with_handler(
    handler_source: str, *, shell_redirect: str = ""
) -> subprocess.CompletedProcess[bytes]:
    """Run hook mode in a child whose ``handle_payload`` is replaced by ``handler_source``."""
    script = (
        "import time\n"
        "from c2c_rulesync import cli\n"
        f"{handler_source}\n"
        "cli.handle_payload = handle_payload\n"
        "cli.run_hook_mode()\n"
    )
    command = f'"{sys.executable}" -c "$SCRIPT" {shell_redirect}'
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=b"{}",
        capture_output=True,
        check=False,
        timeout=30,
        env={**os.environ, "SCRIPT": script},
    )


def test_hook_mode_keeps_stray_prints_off_stdout() -> None:
    result = run_hook_with_handler(
        "def handle_payload(payload, emit):\n"
        "    print('stray diagnostic')\n"
        "    emit(b'{\"ok\":true}')\n"
    )

    assert result.returncode == 0
    assert result.stdout == b'{"ok":true}'
    assert b"stray diagnostic" in result.stderr


def test_hook_mode_keeps_stray_prints_off_stdout_when_stderr_is_closed() -> None:
    result = run_hook_with_handler(
        "def handle_payload(payload, emit):\n"
        "    print('stray diagnostic')\n"
        "    emit(b'{\"ok\":true}')\n",
        shell_redirect="2>&-",
    )

    assert result.returncode == 0
    assert result.stdout == b'{"ok":true}'


def test_hook_mode_exits_0_when_stdout_is_closed() -> None:
    result = run_hook_with_handler(
        "def handle_payload(payload, emit):\n    emit(b'{}')\n",
        shell_redirect=">&-",
    )

    assert result.returncode == 0


def test_hook_mode_exits_0_without_output_past_the_deadline() -> None:
    result = run_hook_with_handler(
        "cli.HOOK_DEADLINE_SECONDS = 0.05\n"
        "def handle_payload(payload, emit):\n"
        "    time.sleep(5)\n"
        "    emit(b'{\"late\":true}')\n"
    )

    assert result.returncode == 0
    assert result.stdout == b""
    assert b"HookDeadlineExceeded" in result.stderr
