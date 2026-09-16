"""Read the file paths out of an ``apply_patch`` patch."""

from __future__ import annotations

__all__ = ["patch_paths"]

_HEADERS = ("*** Add File:", "*** Delete File:", "*** Update File:", "*** Move to:")


def patch_paths(text: str) -> list[str]:
    """File paths named by the headers of an ``apply_patch`` patch, in order.

    Headers count only between ``*** Begin Patch`` and ``*** End Patch``, and,
    as Codex reads them, after surrounding white space is removed. ``*** Move
    to:`` names the destination of a renamed file.
    """
    paths = []
    inside = False
    for line in text.splitlines():
        line = line.strip()
        if line == "*** Begin Patch":
            inside = True
        elif line == "*** End Patch":
            inside = False
        elif inside:
            for header in _HEADERS:
                if line.startswith(header):
                    path = line[len(header) :].strip()
                    if path:
                        paths.append(path)
                    break
    return paths
