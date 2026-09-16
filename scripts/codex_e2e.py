#!/usr/bin/env python3
"""Check c2c-rulesync end to end with the real Codex CLI.

The script builds a throwaway git repository with rule files under /tmp, runs
short ``codex exec`` sessions against it with the hook wired in, and checks the
rules Codex actually recorded as developer messages in each session's rollout
file, plus the hook's own state.

It calls a paid model with the developer's Codex login and configuration
(including their own hooks, which ``--dangerously-bypass-hook-trust`` also
runs), so it never runs in CI.

Usage:
    python3 scripts/codex_e2e.py [--model gpt-5.6-luna] [--hook .venv/bin/c2c-rulesync]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RULE_ELEMENT = re.compile(r'<rule path=\\?"([^"\\]+)\\?">')

TREE = {
    ".claude/rules/project.md": "E2E project rule: always answer briefly.\n",
    ".claude/rules/src.md": '---\npaths: "src/**"\n---\nE2E src rule: prefer const.\n',
    "pkg/.claude/rules/pkg.md": "E2E pkg rule: document every export.\n",
    "src/app.ts": "export const answer = 42;\n",
    "pkg/index.ts": "export {};\n",
}
OUTER_RULE = "E2E outer rule: never use tabs.\n"


class Failure(Exception):
    """An expectation about what Codex received did not hold."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--hook", type=Path, default=REPO_ROOT / ".venv" / "bin" / "c2c-rulesync")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    arguments = parser.parse_args()

    hook = arguments.hook.resolve()
    if not os.access(hook, os.X_OK):
        raise SystemExit(f"{hook} is not executable; run `uv sync` first")
    version = subprocess.run(["codex", "--version"], capture_output=True, text=True, check=True)
    print(version.stdout.strip(), file=sys.stderr)

    # A name without dots, which Codex's -c key splitting would break.
    base = Path(tempfile.mkdtemp(prefix="c2ce2e", dir="/tmp")).resolve()
    try:
        return run_scenarios(base, hook, arguments.model)
    finally:
        if arguments.keep:
            print(f"kept {base}", file=sys.stderr)
        else:
            shutil.rmtree(base, ignore_errors=True)


def run_scenarios(base: Path, hook: Path, model: str) -> int:
    outer = base / "outer"
    repo = outer / "repo"
    write(outer, {".claude/rules/outer.md": OUTER_RULE})
    write(repo, TREE)
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=e2e", "-c", "user.email=e2e@example.com", "commit", "-qm", "init")
    state = base / "state"
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    session = Session(repo, hook, model, state, codex_home)

    results = []

    def check(name: str, scenario: Any) -> None:
        try:
            scenario()
        except Failure as failure:
            results.append((name, f"FAILED: {failure}"))
        else:
            results.append((name, "passed"))
        print(f"{name}: {results[-1][1]}", file=sys.stderr)

    thread: dict[str, str] = {}

    def startup_and_read() -> None:
        thread_id, _ = session.exec(
            "Run the shell command `cat src/app.ts`, then reply with the single word DONE."
        )
        thread["id"] = thread_id
        rules = session.injected(thread_id)
        expect_once(rules, [".claude/rules/project.md", "~outer", ".claude/rules/src.md"])

    def resume_injects_nothing_new() -> None:
        before = session.injected(thread["id"])
        session.exec(
            "Run the shell command `cat src/app.ts` again, then reply with the single word DONE.",
            resume=thread["id"],
        )
        after = session.injected(thread["id"])
        if after != before:
            raise Failure(f"resume added {after[len(before) :]}")

    def apply_patch_loads_nested_rules() -> None:
        thread_id, _ = session.exec(
            "Use the apply_patch tool to create pkg/extra.ts containing `export const x = 1;`, "
            "then reply with the single word DONE.",
            sandbox="workspace-write",
        )
        expect_once(session.injected(thread_id), ["pkg/.claude/rules/pkg.md"])

    def project_config_hooks() -> None:
        codex_dir = repo / ".codex"
        codex_dir.mkdir(exist_ok=True)
        (codex_dir / "config.toml").write_text(project_config(hook))
        try:
            thread_id, _ = session.exec(
                "Reply with the single word DONE.",
                inline_hooks=False,
                extra=["-c", f'projects={{"{repo}"={{trust_level="trusted"}}}}'],
            )
        finally:
            shutil.rmtree(codex_dir)
        expect_once(session.injected(thread_id), [".claude/rules/project.md"])

    check("startup rules and a shell read", startup_and_read)
    if "id" in thread:
        check("resume injects nothing new", resume_injects_nothing_new)
    check("apply_patch loads nested rules", apply_patch_loads_nested_rules)
    check("hooks from a trusted project config", project_config_hooks)

    state_files = sorted(str(path.relative_to(state)) for path in state.rglob("*.json"))
    print(json.dumps({"results": results, "state_files": state_files}, indent=1))
    return 0 if all(result == "passed" for _, result in results) else 1


class Session:
    def __init__(self, repo: Path, hook: Path, model: str, state: Path, codex_home: Path) -> None:
        self.repo = repo
        self.hook = hook
        self.model = model
        self.state = state
        self.codex_home = codex_home

    def exec(
        self,
        prompt: str,
        *,
        resume: str | None = None,
        sandbox: str = "read-only",
        inline_hooks: bool = True,
        extra: list[str] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        command = ["codex", "exec"]
        if resume:
            command.append("resume")
        command += [
            "--json",
            "-m",
            self.model,
            "-c",
            'model_reasoning_effort="low"',
            "--dangerously-bypass-hook-trust",
        ]
        if not resume:
            command += ["-s", sandbox, "-C", str(self.repo)]
        if inline_hooks:
            command += inline_hook_overrides(self.hook)
        command += extra or []
        command += [resume, prompt] if resume else [prompt]
        environment = {
            **os.environ,
            "C2C_RULESYNC_STATE_DIR": str(self.state),
            "C2C_RULESYNC_USER_RULES": "0",
        }
        completed = subprocess.run(
            command,
            cwd=self.repo,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=600,
        )
        events = [
            json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")
        ]
        if completed.returncode != 0:
            raise Failure(f"codex exec exited {completed.returncode}: {completed.stderr[-500:]}")
        thread_id = resume or next(
            (event.get("thread_id") for event in events if event.get("type") == "thread.started"),
            None,
        )
        if not thread_id:
            raise Failure("no thread.started event")
        return thread_id, events

    def injected(self, thread_id: str) -> list[str]:
        """Rule paths in developer messages of the thread's rollout, in order."""
        rollouts = list((self.codex_home / "sessions").rglob(f"rollout-*{thread_id}.jsonl"))
        if len(rollouts) != 1:
            raise Failure(f"expected one rollout for {thread_id}, found {len(rollouts)}")
        paths = []
        for line in rollouts[0].read_text(encoding="utf-8").splitlines():
            if '"developer"' in line and "<rule path=" in line:
                paths += RULE_ELEMENT.findall(line)
        return paths


def expect_once(rules: list[str], expected: list[str]) -> None:
    for path in expected:
        matches = [
            rule for rule in rules if rule == path or (path == "~outer" and "outer.md" in rule)
        ]
        if len(matches) != 1:
            raise Failure(f"{path} delivered {len(matches)} times; delivered: {rules}")


def handler(hook: Path, *, context: bool) -> str:
    fields = f'type="command",command="{hook} hook",timeout=10'
    return "{" + fields + (",additionalContextLimit=0" if context else "") + "}"


def inline_hook_overrides(hook: Path) -> list[str]:
    groups = {
        "SessionStart": ('matcher="startup|clear|compact",', True),
        "SubagentStart": ("", True),
        "PreToolUse": ('matcher="Bash|apply_patch|view_image",', True),
        "PostCompact": ("", False),
    }
    overrides = []
    for event, (matcher, context) in groups.items():
        overrides += [
            "-c",
            f"hooks.{event}=[{{{matcher}hooks=[{handler(hook, context=context)}]}}]",
        ]
    return overrides


def project_config(hook: Path) -> str:
    return (
        "[[hooks.SessionStart]]\n"
        'matcher = "startup|clear|compact"\n\n'
        "[[hooks.SessionStart.hooks]]\n"
        'type = "command"\n'
        f'command = "{hook} hook"\n'
        "timeout = 10\n"
        "additionalContextLimit = 0\n"
    )


def write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
