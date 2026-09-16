"""Find the files a Codex tool call is about to read or change."""

from __future__ import annotations

import os

from c2c_rulesync.patch import patch_paths
from c2c_rulesync.shell import shell_paths

__all__ = ["touched_paths"]

# Tool input fields that name a file, as `view_image` sends `path`.
_PATH_FIELDS = ("path", "file_path", "filePath")


def touched_paths(tool_name: str, tool_input: object, cwd: str, home: str | None) -> list[str]:
    """Absolute paths of the files a tool call touches.

    Args:
        tool_name: The hook payload's ``tool_name``, such as ``Bash`` or
            ``apply_patch``.
        tool_input: The hook payload's ``tool_input``. Codex sends the raw
            string when a tool's arguments are not valid JSON; nothing is found
            in that case.
        cwd: The absolute directory the tool call runs in.
        home: The home directory for ``~`` in shell commands, or ``None``.

    Returns:
        Normalized absolute paths without duplicates: path fields, then the
        files of the patch or command. Existing directories named by a path
        field or a command are left out.
    """
    if not isinstance(tool_input, dict):
        return []
    paths = [
        path
        for field in _PATH_FIELDS
        if isinstance(value := tool_input.get(field), str) and value
        if not os.path.isdir(path := os.path.normpath(os.path.join(cwd, value)))
    ]
    command = tool_input.get("command")
    if isinstance(command, str):
        if tool_name == "apply_patch":
            paths += [os.path.normpath(os.path.join(cwd, path)) for path in patch_paths(command)]
        elif tool_name == "Bash":
            paths += shell_paths(command, cwd, home)
    return list(dict.fromkeys(paths))
