#!/usr/bin/env python3
"""Record which rule files real Claude Code loads, for c2c-rulesync's parity tests.

For every probe in tests/parity/cases.json this script builds the case's file
tree under /tmp and starts one short ``claude -p`` session. The session reads
exactly one probe file and nothing else. Two hooks passed through
``--settings`` observe it:

- an ``InstructionsLoaded`` hook records every instruction file Claude Code
  loads, with the reason (session start, nested traversal, glob match) and the
  normalized globs;
- a ``PreToolUse`` hook blocks every tool call except Read of the probe paths.

The recorded load events are written, with paths relative to the case root, to
tests/parity/claude-code-<version>.json. tests/test_parity.py replays them
against c2c-rulesync offline.

The run calls a paid model and uses the developer's Claude Code login, so it is
never run in CI. Project-level cases only: the developer's own user-level
rules and settings are excluded with ``--setting-sources project``. A probe's
``setting_sources`` replaces that value: all G9 probes but one add ``local`` so
that Claude Code loads CLAUDE.local.md files, and the remaining one records that
none load without it.

Usage:
    python3 scripts/claude_parity_oracle.py --claude-bin ~/.local/share/claude/versions/2.1.273
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = REPO_ROOT / "tests" / "parity" / "cases.json"
# Every session must load this file at start; its absence means the logging
# hook did not run. It is a CLAUDE.md so that no rule-directory layout under
# test can hide it, and it is left out of the recording.
CONTROL_FILE = "CLAUDE.md"
# Environment passed through to Claude Code. Everything else, including any
# CLAUDE* or ANTHROPIC* variable inherited from a surrounding session, is
# dropped so the recording does not depend on where it runs.
PASSED_ENVIRONMENT = ("PATH", "HOME", "LANG", "CLAUDE_CONFIG_DIR")
FIXED_ENVIRONMENT = {
    "TERM": "dumb",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
}
LOGGER = """import os, sys
data = sys.stdin.buffer.read()
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.write(fd, data.rstrip(b"\\n") + b"\\n")
os.close(fd)
"""
GUARD = """import json, sys
payload = json.load(sys.stdin)
allowed = set(json.loads(sys.argv[1]))
tool_input = payload.get("tool_input") or {}
if payload.get("tool_name") == "Read" and tool_input.get("file_path") in allowed:
    sys.exit(0)
sys.stderr.write("parity oracle: only the probe Read calls are allowed\\n")
sys.exit(2)
"""
INSTRUCTION_FILE_NAMES = ("CLAUDE.md", "CLAUDE.local.md")


class ProbeFailed(Exception):
    """A session did not produce a trustworthy recording."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--claude-bin", required=True, type=Path)
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument(
        "--only", action="append", default=[], help="case id to record (repeatable)"
    )
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    claude = arguments.claude_bin.expanduser().resolve()
    version = claude_version(claude)
    cases_bytes = arguments.cases.read_bytes()
    cases = json.loads(cases_bytes)["cases"]
    if arguments.only:
        cases = [case for case in cases if case["id"] in arguments.only]
    output = arguments.output or REPO_ROOT / "tests" / "parity" / f"claude-code-{version}.json"

    recorded: dict[str, Any] = {}
    report = []
    failed = []
    for case in cases:
        missing = [need for need in case.get("requires", []) if not requirement_met(need)]
        if missing:
            print(f"skip {case['id']}: requires {', '.join(missing)}", file=sys.stderr)
            continue
        case_result: dict[str, Any] = {"probes": {}}
        complete = True
        for probe in case["probes"]:
            key = probe_key(probe)
            for attempt in range(arguments.retries + 1):
                try:
                    result, cost = record_probe(claude, arguments.model, case, probe)
                except ProbeFailed as failure:
                    print(f"{case['id']} {key}: attempt {attempt + 1}: {failure}", file=sys.stderr)
                    continue
                report.append({"case": case["id"], "probe": key, "cost_usd": cost})
                case_result["probes"][key] = result
                print(f"{case['id']} {key}: {len(result['lazy'])} lazy loads", file=sys.stderr)
                break
            else:
                failed.append(f"{case['id']} {key}")
                complete = False
        # A case is recorded only as a whole, so a rerun with --only fills it in.
        if complete:
            recorded[case["id"]] = case_result

    if claude_version(claude) != version:
        raise SystemExit("Claude Code changed version during the run; discard the recording")
    existing: dict[str, Any] = {"cases": {}}
    if arguments.only and output.exists():
        existing = json.loads(output.read_text())
    existing["cases"].update(recorded)
    document = {
        "claude_code": version,
        "cases_sha256": hashlib.sha256(cases_bytes).hexdigest(),
        "cases": dict(sorted(existing["cases"].items())),
    }
    output.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
    total = sum(entry["cost_usd"] or 0 for entry in report)
    print(f"wrote {output}; {len(report)} sessions, reported cost ${total:.3f}", file=sys.stderr)
    if failed:
        print("no trustworthy recording, case not written:", *failed, sep="\n  ", file=sys.stderr)
        return 1
    return 0


def claude_version(claude: Path) -> str:
    completed = subprocess.run(
        [str(claude), "--version"],
        capture_output=True,
        text=True,
        env=child_environment(),
        check=True,
    )
    return completed.stdout.split()[0]


def requirement_met(requirement: str) -> bool:
    if requirement == "git":
        return shutil.which("git") is not None
    if requirement in ("symlink", "posix_names"):
        return os.name == "posix"
    if requirement == "case_sensitive_fs":
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            (Path(directory) / "a").touch()
            return not (Path(directory) / "A").exists()
    raise SystemExit(f"unknown requirement {requirement!r}")


def probe_key(probe: dict[str, Any]) -> str:
    """A stable name for a probe: its reads, plus its directory, arguments, environment and
    setting sources."""
    key = "+".join(probe["read"])
    if probe.get("cwd"):
        key += f" (cwd {probe['cwd']})"
    if probe.get("args"):
        key += " " + " ".join(probe["args"])
    if probe.get("env"):
        key += " " + " ".join(f"{name}={value}" for name, value in sorted(probe["env"].items()))
    if probe.get("setting_sources"):
        key += f" --setting-sources {probe['setting_sources']}"
    return key


def child_environment() -> dict[str, str]:
    environment = {name: os.environ[name] for name in PASSED_ENVIRONMENT if name in os.environ}
    environment.update(FIXED_ENVIRONMENT)
    return environment


def build_tree(root: Path, case: dict[str, Any]) -> None:
    for relative, content in case["tree"].items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, dict) and "symlink" in content:
            path.symlink_to(content["symlink"])
        elif isinstance(content, dict) and "base64" in content:
            path.write_bytes(base64.b64decode(content["base64"]))
        else:
            path.write_text(content, encoding="utf-8", newline="")
    for command in case.get("setup", []):
        subprocess.run(
            command["run"], cwd=root / command.get("cwd", "."), check=True, capture_output=True
        )


def check_clean_ancestors(root: Path) -> None:
    for ancestor in root.parents:
        for name in (
            *INSTRUCTION_FILE_NAMES,
            ".claude/CLAUDE.md",
            ".claude/CLAUDE.local.md",
            ".claude/rules",
        ):
            if (ancestor / name).exists():
                raise SystemExit(
                    f"{ancestor / name} would load in every case; use another temp dir"
                )


def record_probe(
    claude: Path, model: str, case: dict[str, Any], probe: dict[str, Any]
) -> tuple[dict[str, Any], float | None]:
    root = Path(tempfile.mkdtemp(prefix="c2cparity", dir="/tmp")).resolve()
    try:
        check_clean_ancestors(root)
        build_tree(root, case)
        cwd_relative = probe.get("cwd", case["cwd"])
        cwd = root / cwd_relative
        (cwd / CONTROL_FILE).write_text("Parity oracle control file.\n", encoding="utf-8")
        read_paths = [str(root / probe_path) for probe_path in probe["read"]]
        harness = root.parent / f"{root.name}-harness"
        harness.mkdir()
        events = harness / "events.jsonl"
        (harness / "log.py").write_text(LOGGER)
        (harness / "guard.py").write_text(GUARD)
        python = shutil.which("python3") or sys.executable
        logger = shlex.join([python, str(harness / "log.py"), str(events)])
        guard = shlex.join([python, str(harness / "guard.py"), json.dumps(read_paths)])
        settings = {
            "hooks": {
                "InstructionsLoaded": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": logger,
                            }
                        ]
                    }
                ],
                "PreToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [
                            {
                                "type": "command",
                                "command": guard,
                            }
                        ],
                    }
                ],
            }
        }
        settings_path = harness / "settings.json"
        settings_path.write_text(json.dumps(settings))
        instruction = " ".join(
            f'Call the Read tool with file_path "{path}".' for path in read_paths
        )
        command = [
            str(claude),
            # Options such as --add-dir take several values; the next option ends them.
            *[argument.replace("{root}", str(root)) for argument in probe.get("args", [])],
            "-p",
            "--model",
            model,
            "--setting-sources",
            probe.get("setting_sources", "project"),
            "--settings",
            str(settings_path),
            "--tools",
            "Read",
            "--allowedTools",
            "Read",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--max-turns",
            str(2 + len(read_paths)),
            "--max-budget-usd",
            "0.10",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--output-format",
            "stream-json",
            "--verbose",
            f"{instruction} Do not call any other tool. After the results, reply DONE.",
        ]
        environment = child_environment()
        for name, value in probe.get("env", {}).items():
            environment[name] = value.replace("{root}", str(root))
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
        )
        time.sleep(1.0)
        stream = [
            json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")
        ]
        read_errors = verify_reads(stream, read_paths, completed.stderr)
        control_file = os.path.realpath(cwd / CONTROL_FILE)
        loads: dict[str, Any] = parse_events(events, root)
        start = loads["session_start"]
        control = [load for load in start if os.path.realpath(root / load["file"]) == control_file]
        if not control:
            raise ProbeFailed("the control file's session start event is missing")
        loads["session_start"] = [load for load in start if load not in control]
        if read_errors:
            # The read failed, so its missing loads say nothing about rules.
            loads["read_errors"] = sorted(os.path.relpath(path, root) for path in read_errors)
        cost = next((m.get("total_cost_usd") for m in stream if m.get("type") == "result"), None)
        return loads, cost
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(root.parent / f"{root.name}-harness", ignore_errors=True)


def verify_reads(stream: list[dict[str, Any]], read_paths: list[str], stderr: str) -> list[str]:
    """Check that every probe file was requested, and return those whose Read failed."""
    requested = set()
    reads_by_id = {}
    failed = []
    for message in stream:
        for block in message.get("message", {}).get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Read":
                path = block.get("input", {}).get("file_path")
                requested.add(path)
                reads_by_id[block.get("id")] = path
            elif block.get("type") == "tool_result" and block.get("is_error"):
                if block.get("tool_use_id") in reads_by_id:
                    failed.append(reads_by_id[block["tool_use_id"]])
    if set(read_paths) - requested:
        result = next((m for m in stream if m.get("type") == "result"), {})
        detail = str(result.get("result") or stderr.strip())[:300]
        raise ProbeFailed(f"the model did not read {sorted(set(read_paths) - requested)}: {detail}")
    return failed


def parse_events(events: Path, root: Path) -> dict[str, list[dict[str, Any]]]:
    if not events.exists():
        raise ProbeFailed("no InstructionsLoaded event was recorded")
    session_start = []
    lazy = []
    for line in events.read_text().splitlines():
        event = json.loads(line)
        file_path = Path(event["file_path"])
        # Claude Code may report a path through a link; either spelling inside
        # the case root is accepted.
        relative = None
        for candidate in (file_path, file_path.resolve()):
            try:
                relative = candidate.relative_to(root).as_posix()
                break
            except ValueError:
                continue
        if relative is None:
            raise ProbeFailed(f"an instruction file outside the case root loaded: {file_path}")
        entry: dict[str, Any] = {"file": relative, "memory_type": event.get("memory_type")}
        if event.get("load_reason") == "session_start":
            session_start.append(entry)
            continue
        entry["load_reason"] = event.get("load_reason")
        if event.get("globs") is not None:
            entry["globs"] = event["globs"]
        trigger = event.get("trigger_file_path")
        if trigger:
            entry["trigger"] = os.path.relpath(trigger, root)
        lazy.append(entry)
    key = lambda entry: json.dumps(entry, sort_keys=True)  # noqa: E731
    return {"session_start": sorted(session_start, key=key), "lazy": sorted(lazy, key=key)}


if __name__ == "__main__":
    raise SystemExit(main())
