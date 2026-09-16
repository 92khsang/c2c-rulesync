"""Turn rules into the text and JSON a Codex hook prints."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence

from c2c_rulesync.rules import Rule

__all__ = ["display_path", "hook_output", "render_rules"]

_CLOSING_TAG = re.compile(r"</(rule\s*)>", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def render_rules(rules: Sequence[Rule], cwd: str, home: str | None) -> str:
    """The model context for ``rules``: each body in a ``<rule path="...">`` element."""
    blocks = []
    for rule in rules:
        path = _attribute(display_path(rule.path, cwd, home))
        body = _CLOSING_TAG.sub(r"<\\/\1>", _valid_text(rule.body).strip("\r\n"))
        blocks.append(f'<rule path="{path}">\n{body}\n</rule>')
    return "\n\n".join(blocks)


def display_path(path: str, cwd: str, home: str | None) -> str:
    """``path`` relative to ``cwd`` when inside it, else under ``~`` when possible."""
    if path.startswith(cwd.rstrip(os.sep) + os.sep):
        return os.path.relpath(path, cwd)
    if home and path.startswith(home.rstrip(os.sep) + os.sep):
        return "~" + os.sep + os.path.relpath(path, home)
    return path


def hook_output(event: str, context: str, messages: Sequence[str]) -> bytes | None:
    """The JSON object a hook prints for ``event``, or ``None`` when there is nothing to say.

    Codex rejects unknown keys and invalid UTF-8, so the output holds only the
    keys its schema allows and is pure ASCII.
    """
    output: dict[str, object] = {}
    if messages:
        output["systemMessage"] = _valid_text("\n".join(messages))
    if context:
        output["hookSpecificOutput"] = {
            "hookEventName": event,
            "additionalContext": _valid_text(context),
        }
    if not output:
        return None
    return json.dumps(output, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _valid_text(text: str) -> str:
    # File names that are not valid UTF-8 decode to lone surrogates, which
    # Codex's JSON parser rejects.
    return text.encode("utf-8", "replace").decode("utf-8")


def _attribute(text: str) -> str:
    text = _valid_text(text).replace("&", "&amp;").replace('"', "&quot;")
    return _CONTROL.sub("?", text.replace("<", "&lt;").replace(">", "&gt;"))
