"""The hook runtime: what each Codex event injects, and when."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from c2c_rulesync.hook import run_hook
from c2c_rulesync.state import sweep

SCHEMAS = Path(__file__).parent / "vectors" / "codex-0.154.0"
SESSION = "019a0000-0000-7000-8000-000000000001"


def schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    return loaded


class Codex:
    """Sends hook payloads for one project the way Codex 0.154.0 does."""

    def __init__(self, tmp_path: Path) -> None:
        self.project = tmp_path / "project"
        self.project.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.state = tmp_path / "state"
        self.environ = {"HOME": str(self.home), "C2C_RULESYNC_STATE_DIR": str(self.state)}

    def write(self, files: dict[str, str]) -> None:
        for relative, text in files.items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def payload(self, event: str, **fields: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "session_id": SESSION,
            "transcript_path": f"/codex/sessions/rollout-2026-09-16T00-00-00-{SESSION}.jsonl",
            "cwd": str(self.project),
            "hook_event_name": event,
            "model": "gpt-5",
            "permission_mode": "default",
        }
        if event == "SessionStart":
            base["source"] = "startup"
        if event in ("PreToolUse", "SubagentStart", "PostCompact"):
            base["turn_id"] = "turn-1"
        if event == "PreToolUse":
            base.update(tool_name="Bash", tool_input={"command": "true"}, tool_use_id="call-1")
        if event == "PostCompact":
            base["trigger"] = "auto"
            del base["permission_mode"]
        if event == "SubagentStart":
            base.update(agent_id="agent-1", agent_type="worker")
        base.update(fields)
        return base

    def send(self, event: str, **fields: Any) -> dict[str, Any] | None:
        payload = self.payload(event, **fields)
        schema_name = _schema_name(event)
        jsonschema.validate(payload, schema(f"{schema_name}.command.input"))
        outputs: list[bytes] = []
        run_hook(json.dumps(payload).encode(), outputs.append, self.environ)
        assert len(outputs) <= 1
        if not outputs:
            return None
        assert outputs[0].isascii()
        document: dict[str, Any] = json.loads(outputs[0])
        jsonschema.validate(document, schema(f"{schema_name}.command.output"))
        return document

    def bash(self, command: str, **fields: Any) -> dict[str, Any] | None:
        return self.send("PreToolUse", tool_input={"command": command}, **fields)


def _schema_name(event: str) -> str:
    return {
        "SessionStart": "session-start",
        "SubagentStart": "subagent-start",
        "PreToolUse": "pre-tool-use",
        "PostCompact": "post-compact",
    }[event]


def context(output: dict[str, Any] | None) -> str:
    assert output is not None
    text: str = output["hookSpecificOutput"]["additionalContext"]
    return text


def rule_paths(output: dict[str, Any] | None) -> list[str]:
    if output is None or "hookSpecificOutput" not in output:
        return []
    return [
        line[len('<rule path="') : -2]
        for line in context(output).splitlines()
        if line.startswith('<rule path="')
    ]


SCOPED = '---\npaths: "src/**"\n---\nScoped rule.\n'


@pytest.fixture
def codex(tmp_path: Path) -> Codex:
    return Codex(tmp_path)


# Session start -------------------------------------------------------------------------


def test_session_start_injects_unconditional_rules_once(codex: Codex) -> None:
    codex.write({".claude/rules/style.md": "Use tabs.\n", ".claude/rules/src.md": SCOPED})

    first = codex.send("SessionStart")

    assert context(first) == '<rule path=".claude/rules/style.md">\nUse tabs.\n</rule>'
    assert first is not None and "style.md" in first["systemMessage"]
    assert codex.send("SessionStart") is None


def test_a_resumed_session_receives_nothing_again(codex: Codex) -> None:
    codex.write({".claude/rules/style.md": "Use tabs.\n"})

    assert codex.send("SessionStart", source="resume") is None


def test_user_rules_come_from_the_claude_config_directory(codex: Codex, tmp_path: Path) -> None:
    config = tmp_path / "config"
    (config / "rules").mkdir(parents=True)
    (config / "rules" / "mine.md").write_text("Mine.\n")
    (codex.home / ".claude" / "rules").mkdir(parents=True)
    (codex.home / ".claude" / "rules" / "home.md").write_text("Home.\n")

    codex.environ["CLAUDE_CONFIG_DIR"] = str(config)
    assert rule_paths(codex.send("SessionStart")) == [f"{config}/rules/mine.md"]


def test_user_rules_default_to_the_home_directory_and_can_be_turned_off(codex: Codex) -> None:
    (codex.home / ".claude" / "rules").mkdir(parents=True)
    (codex.home / ".claude" / "rules" / "home.md").write_text("Home.\n")

    assert rule_paths(codex.send("SessionStart", session_id="s-1")) == ["~/.claude/rules/home.md"]
    codex.environ["C2C_RULESYNC_USER_RULES"] = "0"
    assert codex.send("SessionStart", session_id="s-2") is None


@pytest.mark.parametrize("config", ["{home}/.claude-extra", "~/.claude-extra"])
def test_home_rules_load_as_project_rules_beside_another_config_directory(
    codex: Codex, config: str
) -> None:
    project = codex.home / "work" / "spec"
    project.mkdir(parents=True)
    python = '---\npaths: "*.py"\n---\nComment Python.\n'
    for directory in (".claude-extra", ".claude"):
        (codex.home / directory / "rules").mkdir(parents=True)
        (codex.home / directory / "rules" / "comments-python.md").write_text(python)
    (codex.home / ".claude" / "rules" / "context7.md").write_text("Use Context7.\n")

    codex.environ["CLAUDE_CONFIG_DIR"] = config.format(home=codex.home)
    assert rule_paths(codex.send("SessionStart", cwd=str(project))) == [
        "~/.claude/rules/context7.md"
    ]
    assert rule_paths(codex.bash("cat src/a.py", cwd=str(project))) == [
        "~/.claude-extra/rules/comments-python.md",
        "~/.claude/rules/comments-python.md",
    ]


# Tool calls ----------------------------------------------------------------------------


def test_a_tool_call_injects_matching_rules_once(codex: Codex) -> None:
    codex.write({".claude/rules/src.md": SCOPED, "src/a.ts": ""})
    codex.send("SessionStart")

    assert rule_paths(codex.bash("cat src/a.ts")) == [".claude/rules/src.md"]
    assert codex.bash("sed -n 1,5p src/a.ts") is None
    assert codex.bash("cat README.md") is None


def test_apply_patch_and_view_image_trigger_rules(codex: Codex) -> None:
    codex.write(
        {".claude/rules/src.md": SCOPED, ".claude/rules/img.md": '---\npaths: "*.png"\n---\nImg.\n'}
    )
    codex.send("SessionStart")
    patch = "*** Begin Patch\n*** Add File: src/new.ts\n+x\n*** End Patch\n"

    assert rule_paths(
        codex.send("PreToolUse", tool_name="apply_patch", tool_input={"command": patch})
    ) == [".claude/rules/src.md"]
    assert rule_paths(
        codex.send("PreToolUse", tool_name="view_image", tool_input={"path": "shot.png"})
    ) == [".claude/rules/img.md"]


def test_a_tool_call_delivers_start_rules_a_failed_session_start_missed(codex: Codex) -> None:
    codex.write({".claude/rules/style.md": "Use tabs.\n", ".claude/rules/src.md": SCOPED})

    assert rule_paths(codex.bash("true")) == [".claude/rules/style.md"]
    assert rule_paths(codex.bash("cat src/a.ts")) == [".claude/rules/src.md"]
    assert codex.send("SessionStart") is None


def test_reading_a_rule_file_counts_as_loading_it(codex: Codex) -> None:
    codex.write({".claude/rules/src.md": SCOPED})
    codex.send("SessionStart")

    assert codex.bash("cat .claude/rules/src.md") is None
    assert codex.bash("cat src/a.ts") is None


def test_tool_calls_without_a_session_id_inject_nothing(codex: Codex) -> None:
    codex.write({".claude/rules/style.md": "Use tabs.\n", ".claude/rules/src.md": SCOPED})
    payload = codex.payload("PreToolUse", tool_input={"command": "cat src/a.ts"})
    del payload["session_id"]
    outputs: list[bytes] = []

    run_hook(json.dumps(payload).encode(), outputs.append, codex.environ)

    assert outputs == []


def test_session_start_without_state_still_injects(codex: Codex) -> None:
    codex.write({".claude/rules/style.md": "Use tabs.\n"})
    del codex.environ["C2C_RULESYNC_STATE_DIR"]
    del codex.environ["HOME"]

    assert rule_paths(codex.send("SessionStart")) == [".claude/rules/style.md"]
    assert rule_paths(codex.send("SessionStart")) == [".claude/rules/style.md"]


# Compaction and threads ----------------------------------------------------------------


def test_compaction_makes_every_rule_deliverable_again(codex: Codex) -> None:
    codex.write({".claude/rules/style.md": "Use tabs.\n", ".claude/rules/src.md": SCOPED})
    codex.send("SessionStart")
    codex.bash("cat src/a.ts")

    assert codex.send("PostCompact") is None
    assert rule_paths(codex.send("SessionStart", source="compact")) == [".claude/rules/style.md"]
    assert rule_paths(codex.bash("cat src/a.ts")) == [".claude/rules/src.md"]


def test_a_subagent_compaction_resets_only_that_subagent(codex: Codex) -> None:
    codex.write({".claude/rules/style.md": "Use tabs.\n"})
    codex.send("SessionStart")
    assert rule_paths(codex.send("SubagentStart")) == [".claude/rules/style.md"]

    codex.send("PostCompact", agent_id="agent-1", agent_type="worker")

    assert rule_paths(codex.bash("true", agent_id="agent-1", agent_type="worker")) == [
        ".claude/rules/style.md"
    ]
    assert codex.bash("true") is None


def test_a_review_thread_with_its_own_transcript_has_its_own_state(codex: Codex) -> None:
    codex.write({".claude/rules/src.md": SCOPED})
    codex.send("SessionStart")
    review = (
        "/codex/sessions/rollout-2026-09-16T00-00-00-019a0000-0000-7000-8000-00000000beef.jsonl"
    )

    assert rule_paths(codex.bash("cat src/a.ts", transcript_path=review)) == [
        ".claude/rules/src.md"
    ]
    assert rule_paths(codex.bash("cat src/a.ts")) == [".claude/rules/src.md"]


# State ---------------------------------------------------------------------------------


def test_corrupt_state_counts_as_empty(codex: Codex) -> None:
    codex.write({".claude/rules/style.md": "Use tabs.\n"})
    codex.send("SessionStart")
    for path in codex.state.glob("sessions/*/*.json"):
        path.write_text("{not json")

    assert rule_paths(codex.bash("true")) == [".claude/rules/style.md"]


def test_warnings_are_shown_once_per_thread(codex: Codex) -> None:
    codex.write({".claude/rules/broken.md": "---\npaths:\n  - **/*.ts\n---\nBody\n"})

    first = codex.send("SessionStart")

    assert first is not None and "not valid YAML" in first["systemMessage"]
    assert codex.bash("true") is None


def test_sweep_removes_only_sessions_idle_for_a_week(tmp_path: Path) -> None:
    old = tmp_path / "sessions" / "old"
    new = tmp_path / "sessions" / "new"
    for directory in (old, new):
        directory.mkdir(parents=True)
        (directory / "root.json").write_text("{}")
        (directory / "root.lock").write_text("")
    week_ago = time.time() - 8 * 24 * 3600
    for path in old.iterdir():
        os.utime(path, (week_ago, week_ago))

    sweep(str(tmp_path))

    assert not old.exists()
    assert new.exists()
    (new / "root.json").unlink()
    sweep(str(tmp_path))
    assert (new / "root.lock").exists()


# Output --------------------------------------------------------------------------------


def test_rule_text_cannot_close_its_element_and_paths_are_escaped(codex: Codex) -> None:
    codex.write({'.claude/rules/a"<b>&.md': "Before </rule> after </RULE > end.\n"})

    text = context(codex.send("SessionStart"))

    assert text == (
        '<rule path=".claude/rules/a&quot;&lt;b&gt;&amp;.md">\n'
        "Before <\\/rule> after <\\/RULE > end.\n</rule>"
    )


@pytest.mark.skipif(sys.platform == "darwin", reason="APFS requires UTF-8 names")
def test_undecodable_paths_and_bytes_still_produce_valid_output(
    codex: Codex, tmp_path: Path
) -> None:
    config = os.fsdecode(os.fsencode(tmp_path) + b"/conf\xff")
    os.makedirs(os.path.join(config, "rules"))
    with open(os.path.join(config, "rules", "u.md"), "wb") as handle:
        handle.write(b"Accents \xc3\xa9 and invalid \xff bytes.\n")
    codex.environ["CLAUDE_CONFIG_DIR"] = config

    text = context(codex.send("SessionStart"))

    assert "conf?/rules/u.md" in text
    assert "Accents \U000000e9 and invalid \U0000fffd bytes." in text


# Processes -----------------------------------------------------------------------------


def run_cli(codex: Codex, payload: dict[str, Any]) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        [sys.executable, "-m", "c2c_rulesync", "hook"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, **codex.environ},
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload).encode())
    process.stdin.close()
    # communicate() on Python 3.11 flushes stdin even when it is closed.
    process.stdin = None
    return process


def test_parallel_tool_calls_deliver_each_rule_once(codex: Codex) -> None:
    rules = {
        f".claude/rules/r{index}.md": f'---\npaths: "src/{index}.ts"\n---\nR{index}\n'
        for index in range(8)
    }
    codex.write({**rules, ".claude/rules/style.md": "Use tabs.\n"})
    command = "cat " + " ".join(f"src/{index}.ts" for index in range(8))
    processes = [
        run_cli(codex, codex.payload("PreToolUse", tool_input={"command": command}))
        for _ in range(16)
    ]

    delivered: list[str] = []
    for process in processes:
        stdout, _ = process.communicate(timeout=60)
        assert process.returncode == 0
        if stdout:
            delivered += rule_paths(json.loads(stdout))

    assert sorted(delivered) == sorted([*rules, ".claude/rules/style.md"])


def test_an_unwritable_state_directory_fails_open(codex: Codex) -> None:
    codex.write({".claude/rules/src.md": SCOPED})
    codex.state.write_text("not a directory")

    process = run_cli(codex, codex.payload("PreToolUse", tool_input={"command": "cat src/a.ts"}))
    stdout, _ = process.communicate(timeout=60)

    assert process.returncode == 0
    assert stdout == b""


# Review cases ----------------------------------------------------------------------------


def test_sweep_removes_only_the_hooks_own_files(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    mine = sessions / "old"
    theirs = sessions / "2025-conference"
    elsewhere = tmp_path / "elsewhere"
    for directory in (mine, theirs, elsewhere):
        directory.mkdir(parents=True)
    (mine / "root.json").write_text("{}")
    (mine / "root.lock").write_text("")
    (theirs / "notes.md").write_text("keep")
    (theirs / "slides.json").write_text("keep")
    (elsewhere / "root.json").write_text("keep")
    (sessions / "linked").symlink_to(elsewhere)
    week_ago = time.time() - 8 * 24 * 3600
    for path in [*mine.iterdir(), *theirs.iterdir(), *elsewhere.iterdir()]:
        os.utime(path, (week_ago, week_ago))

    sweep(str(tmp_path))

    assert not mine.exists()
    assert (theirs / "notes.md").exists()
    assert (elsewhere / "root.json").exists()


def test_a_session_in_use_is_not_swept(codex: Codex) -> None:
    codex.write({".claude/rules/style.md": "Use tabs.\n", ".claude/rules/src.md": SCOPED})
    codex.send("SessionStart")
    codex.bash("cat src/a.ts")
    week_ago = time.time() - 8 * 24 * 3600
    for path in codex.state.rglob("*"):
        os.utime(path, (week_ago, week_ago))

    assert codex.bash("cat src/a.ts") is None
    sweep(str(codex.state))

    assert codex.bash("cat src/b.ts") is None


def test_compaction_still_resets_when_the_epoch_cannot_be_written(codex: Codex) -> None:
    codex.write({".claude/rules/style.md": "Use tabs.\n", ".claude/rules/src.md": SCOPED})
    codex.send("SessionStart")
    codex.bash("cat src/a.ts")
    for record in codex.state.glob("sessions/*/root.json"):
        (record.parent / "root.epoch").mkdir()

    codex.send("PostCompact")

    assert rule_paths(codex.send("SessionStart", source="compact")) == [".claude/rules/style.md"]
    assert rule_paths(codex.bash("cat src/a.ts")) == [".claude/rules/src.md"]


def test_an_unusable_state_directory_is_reported_at_session_start(codex: Codex) -> None:
    codex.write({".claude/rules/style.md": "Use tabs.\n"})
    codex.state.write_text("not a directory")

    output = codex.send("SessionStart")

    assert rule_paths(output) == [".claude/rules/style.md"]
    assert output is not None and "cannot record delivered rules" in output["systemMessage"]


def test_a_held_lock_falls_back_at_start_and_stays_silent_for_tool_calls(
    codex: Codex, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    from c2c_rulesync import state

    codex.write({".claude/rules/style.md": "Use tabs.\n", ".claude/rules/src.md": SCOPED})
    codex.send("SessionStart", session_id="other")
    (lock,) = codex.state.glob("sessions/other/*.lock")
    monkeypatch.setattr(state, "LOCK_TIMEOUT_SECONDS", 0.05)
    with open(lock, "rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        started = codex.send("SessionStart", session_id="other")
        assert rule_paths(started) == [".claude/rules/style.md"]
        assert started is not None and "another hook process" in started["systemMessage"]
        # The error reaches the CLI, which exits 0 without output.
        with pytest.raises(state.LockTimeout):
            codex.bash("cat src/a.ts", session_id="other")


def test_tool_calls_look_up_files_within_a_time_budget(
    codex: Codex, monkeypatch: pytest.MonkeyPatch
) -> None:
    from c2c_rulesync import hook

    codex.write({".claude/rules/src.md": SCOPED})
    codex.send("SessionStart")
    monkeypatch.setattr(hook, "TRIGGER_BUDGET_SECONDS", -1.0)
    assert codex.bash("cat src/a.ts") is None

    monkeypatch.setattr(hook, "TRIGGER_BUDGET_SECONDS", 3.0)
    assert rule_paths(codex.bash("cat src/a.ts")) == [".claude/rules/src.md"]


def test_only_markdown_reads_are_recorded(codex: Codex) -> None:
    codex.write({".claude/rules/src.md": SCOPED})
    codex.send("SessionStart")
    codex.bash("cat src/a.ts src/b.ts README.md")

    (record,) = codex.state.glob("sessions/*/root.json")
    loaded = json.loads(record.read_text())["loaded"]

    assert sorted(os.path.relpath(path, codex.project) for path in loaded) == [
        ".claude/rules/src.md",
        "README.md",
    ]


def test_start_rules_due_again_after_a_stale_read_are_delivered(
    codex: Codex, monkeypatch: pytest.MonkeyPatch
) -> None:
    from c2c_rulesync.state import State, ThreadState

    codex.write({".claude/rules/style.md": "Use tabs.\n", ".claude/rules/src.md": SCOPED})
    codex.send("SessionStart")
    codex.send("PostCompact")
    monkeypatch.setattr(ThreadState, "peek", lambda self: State(True, set(), []))

    assert rule_paths(codex.bash("cat src/a.ts")) == [
        ".claude/rules/style.md",
        ".claude/rules/src.md",
    ]


def test_threads_without_a_transcript_share_the_main_thread(codex: Codex) -> None:
    codex.write({".claude/rules/src.md": SCOPED})
    codex.send("SessionStart", transcript_path=None)

    assert rule_paths(codex.bash("cat src/a.ts", transcript_path=None)) == [".claude/rules/src.md"]
    assert codex.bash("cat src/a.ts") is None


@pytest.mark.parametrize("event", ["SubagentStart", "PreToolUse"])
def test_warnings_alone_are_valid_output(codex: Codex, event: str) -> None:
    codex.write({".claude/rules/broken.md": "---\npaths:\n  - **/*.ts\n---\n\n"})

    output = codex.send(event)

    assert output is not None and "hookSpecificOutput" not in output
    assert "not valid YAML" in output["systemMessage"]


def test_clear_starts_like_startup_and_resume_keeps_the_record(codex: Codex) -> None:
    codex.write({".claude/rules/style.md": "Use tabs.\n", ".claude/rules/src.md": SCOPED})

    assert rule_paths(codex.send("SessionStart", source="clear")) == [".claude/rules/style.md"]
    assert codex.send("SessionStart", source="resume") is None
    assert rule_paths(codex.bash("cat src/a.ts")) == [".claude/rules/src.md"]


def test_duplicate_session_start_processes_deliver_once(codex: Codex) -> None:
    codex.write({".claude/rules/style.md": "Use tabs.\n"})
    processes = [run_cli(codex, codex.payload("SessionStart")) for _ in range(8)]

    delivered: list[str] = []
    for process in processes:
        stdout, _ = process.communicate(timeout=60)
        assert process.returncode == 0
        if stdout:
            delivered += rule_paths(json.loads(stdout))

    assert delivered == [".claude/rules/style.md"]
