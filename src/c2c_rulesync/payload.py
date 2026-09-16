"""Read the JSON payload Codex sends a command hook on stdin."""

from __future__ import annotations

import json
import os
import re

__all__ = ["Payload", "parse_payload"]

# Codex names a thread's transcript `rollout-<timestamp>-<thread id>.jsonl`.
_ROLLOUT_THREAD_ID = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl(?:\.zst)?\Z",
    re.IGNORECASE,
)


class Payload:
    """The fields of a hook payload that c2c-rulesync uses.

    Attributes:
        event: ``hook_event_name``, such as ``PreToolUse``.
        session_id: The session id, or ``None`` when missing.
        cwd: The absolute working directory, or ``None`` when missing or relative.
        source: ``SessionStart``'s ``source``, such as ``startup``, or ``None``.
        agent_id: The subagent id of a thread spawned by a subagent tool, or ``None``.
        transcript_path: The thread's transcript file, or ``None``.
        tool_name: ``PreToolUse``'s ``tool_name``, or ``None``.
        tool_input: ``PreToolUse``'s ``tool_input``, of any JSON type.
    """

    __slots__ = (
        "agent_id",
        "cwd",
        "event",
        "session_id",
        "source",
        "tool_input",
        "tool_name",
        "transcript_path",
    )

    def __init__(self, fields: dict[str, object]) -> None:
        self.event = _string(fields.get("hook_event_name")) or ""
        self.session_id = _string(fields.get("session_id"))
        cwd = _string(fields.get("cwd"))
        self.cwd = cwd if cwd is not None and os.path.isabs(cwd) else None
        self.source = _string(fields.get("source"))
        self.agent_id = _string(fields.get("agent_id"))
        self.transcript_path = _string(fields.get("transcript_path"))
        self.tool_name = _string(fields.get("tool_name"))
        self.tool_input = fields.get("tool_input")

    def thread(self) -> str:
        """A name for the conversation thread the event belongs to.

        Subagents spawned by a tool carry their own ``agent_id``. Other internal
        subagents, such as a review, share the session id with the main thread
        but write their own transcript, whose thread id then tells them apart.
        """
        if self.agent_id:
            return f"agent-{self.agent_id}"
        match = _ROLLOUT_THREAD_ID.search(os.path.basename(self.transcript_path or ""))
        if match and match.group(1).lower() != (self.session_id or "").lower():
            return f"thread-{match.group(1).lower()}"
        return "root"


def parse_payload(data: bytes) -> Payload | None:
    """Parse stdin bytes, or return ``None`` when they are not a JSON object."""
    try:
        fields = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return Payload(fields) if isinstance(fields, dict) else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
