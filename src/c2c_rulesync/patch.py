"""Read the file paths out of an ``apply_patch`` patch."""

from __future__ import annotations

__all__ = ["patch_paths"]

_HEADERS = ("*** Add File:", "*** Delete File:", "*** Update File:", "*** Move to:")


def patch_paths(text: str) -> list[str]:
    """File paths named by the headers of an ``apply_patch`` patch, in order.

    Headers count only between ``*** Begin Patch`` and ``*** End Patch``.
    ``*** Move to:`` names the destination of a renamed file. As Codex reads
    them, lines are split at line feeds only, and white space around a header is
    ignored, except that inside an ``*** Update File:`` section only trailing
    white space is: an indented header-like line there is context of the diff.
    """
    paths = []
    inside = False
    in_update = False
    for raw_line in text.split("\n"):
        line = raw_line.rstrip() if in_update else raw_line.strip()
        if line == "*** Begin Patch":
            inside = True
            in_update = False
        elif line == "*** End Patch":
            inside = False
            in_update = False
        elif inside:
            for header in _HEADERS:
                if line.startswith(header):
                    path = line[len(header) :].strip()
                    if path:
                        paths.append(path)
                    if header != "*** Move to:":
                        in_update = header == "*** Update File:"
                    break
    return paths
