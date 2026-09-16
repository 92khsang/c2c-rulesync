"""The Codex configuration in README.md is the one the hook is built for."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

README = Path(__file__).parent.parent / "README.md"


def readme_config() -> dict[str, Any]:
    blocks = re.findall(r"```toml\n(.*?)```", README.read_text(encoding="utf-8"), re.DOTALL)
    assert len(blocks) == 1
    config: dict[str, Any] = tomllib.loads(blocks[0])
    return config


def test_readme_wires_every_event_the_hook_handles() -> None:
    hooks = readme_config()["hooks"]

    assert {
        event: [group.get("matcher") for group in groups] for event, groups in hooks.items()
    } == {
        "SessionStart": ["startup|clear|compact"],
        "SubagentStart": [None],
        "PreToolUse": ["Bash|apply_patch|view_image"],
        "PostCompact": [None],
    }


def test_readme_handlers_are_bounded_commands_that_deliver_whole_rules() -> None:
    for event, groups in readme_config()["hooks"].items():
        (handler,) = groups[0]["hooks"]
        expected = {"type": "command", "command": "c2c-rulesync hook", "timeout": 10}
        if event != "PostCompact":
            # Codex ignores the limit, with a warning, on events without context.
            expected["additionalContextLimit"] = 0
        assert handler == expected, event
