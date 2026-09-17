"""Read Claude Code's ``claudeMdExcludes`` setting and match paths against it.

Claude Code skips a memory file whose absolute path matches one of these glob
patterns (https://code.claude.com/docs/en/memory#exclude-specific-claude-md-files).
The documentation does not describe the glob syntax, so the matching here
follows what Claude Code 2.1.273 was recorded doing (the G10 cases in
tests/parity/cases.json):

- a pattern is compared with the whole absolute path, case-sensitively, and a
  pattern that starts with neither ``/`` nor ``**`` matches nothing; ``~`` is
  not expanded;
- ``*`` and ``?`` match within one path segment, ``**`` as a whole segment
  matches any number of segments, and inside a segment it acts as ``*``; all of
  them match names that start with a dot;
- ``{a,b}``, nested braces and integer ranges such as ``{1..3}`` expand;
- ``[ab]``, ``[a-c]`` and ``[^a]`` match one character, while ``!`` in a class
  is an ordinary member;
- a pattern naming a directory does not cover the files in it;
- the fixed directories at the start of a pattern also match through links;
- one entry that is not a string leaves the whole list unapplied.

A pattern using any other syntax, such as extglobs, escapes or a leading ``!``,
is ignored with a warning, so that it never hides a file Claude Code would load.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Sequence
from typing import TypeAlias

__all__ = ["MAX_SETTINGS_BYTES", "Excludes", "load_excludes"]

# Claude Code refuses a settings file larger than this.
MAX_SETTINGS_BYTES = 2 * 1024 * 1024
_MAX_PATTERN_LENGTH = 4096
# Patterns after brace expansion, over all entries; matching cost grows with them.
_MAX_ALTERNATIVES = 1000
# Deeper braces would make expansion take time quadratic in the pattern's length.
_MAX_BRACE_DEPTH = 16
_SHOWN_PATTERN_LENGTH = 100
_KEY = "claudeMdExcludes"


def load_excludes(settings_path: str) -> Excludes | None:
    """The ``claudeMdExcludes`` of the settings file at ``settings_path``.

    Returns ``None`` when the file does not exist or does not set the key.
    Anything else that keeps the patterns from applying, such as invalid JSON,
    gives an ``Excludes`` that matches nothing and carries a warning. Never
    raises.
    """
    try:
        data = _read_regular_file(settings_path, MAX_SETTINGS_BYTES + 1)
    except (FileNotFoundError, NotADirectoryError, ValueError):
        return None
    except _NotAFile:
        return _unapplied(settings_path, "not a regular file")
    except OSError as error:
        return _unapplied(settings_path, str(error.strerror or error))
    if len(data) > MAX_SETTINGS_BYTES:
        return _unapplied(settings_path, "larger than 2 MiB")
    try:
        settings = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _unapplied(settings_path, "not valid JSON")
    if not isinstance(settings, dict):
        return _unapplied(settings_path, "not a JSON object")
    if _KEY not in settings:
        return None
    patterns = settings[_KEY]
    if not isinstance(patterns, list):
        return _unapplied(settings_path, f"{_KEY} is not a list")
    if not all(isinstance(pattern, str) for pattern in patterns):
        return _unapplied(settings_path, f"{_KEY} has an entry that is not a string")
    return Excludes(patterns, settings_path)


class Excludes:
    """Compiled ``claudeMdExcludes`` patterns.

    Args:
        patterns: The patterns, in settings order.
        source: Where the patterns come from, for warnings.

    Attributes:
        warnings: Notes about patterns that are not applied.
        active: Whether any pattern can match.
    """

    def __init__(self, patterns: Sequence[str], source: str) -> None:
        self.warnings: list[str] = []
        self._alternatives: list[tuple[_Segment, ...]] = []
        count = 0
        for pattern in patterns:
            expanded = self._expand(pattern, source)
            if expanded is None:
                continue
            count += len(expanded)
            if count > _MAX_ALTERNATIVES:
                self.warnings.append(
                    f"{source}: {_KEY} expands to more than {_MAX_ALTERNATIVES} patterns; "
                    f"{_shown(pattern)} and the patterns after it are not applied"
                )
                break
            relative = False
            for alternative in expanded:
                segments = _compile(alternative)
                if segments is None:
                    relative = True
                else:
                    self._alternatives += _with_resolved_prefix(segments)
            if relative:
                self.warnings.append(
                    f"{source}: {_KEY} pattern {_shown(pattern)} matches no absolute path"
                    + ("; ~ is not expanded" if pattern.startswith("~") else "")
                    + "; start it with / or **/"
                )
        self.active = bool(self._alternatives)

    def matches(self, path: str) -> bool:
        """Whether the absolute, normalized ``path`` matches a pattern."""
        if not path.startswith("/"):
            return False
        parts = path.split("/")[1:]
        return any(_match_segments(segments, parts) for segments in self._alternatives)

    def _expand(self, pattern: str, source: str) -> list[str] | None:
        problem = _unsupported(pattern)
        expanded: list[str] = []
        if problem is None:
            try:
                expanded = _expand_braces(pattern)
            except _Unsupported as error:
                problem = str(error)
            except RecursionError:
                problem = "too many braces"
        if problem is not None:
            self.warnings.append(
                f"{source}: {_KEY} pattern {_shown(pattern)} uses {problem}, which "
                "c2c-rulesync does not support; the pattern is not applied"
            )
            return None
        return expanded


class _NotAFile(Exception):
    pass


class _Unsupported(Exception):
    pass


class _Globstar:
    """A whole ``**`` segment."""


_GLOBSTAR = _Globstar()


class _Class:
    """A bracket expression: one character in, or with ``^`` not in, a set."""

    __slots__ = ("chars", "negated", "ranges")

    def __init__(self, negated: bool, chars: str, ranges: tuple[tuple[str, str], ...]) -> None:
        self.negated = negated
        self.chars = chars
        self.ranges = ranges

    def matches(self, char: str) -> bool:
        found = char in self.chars or any(low <= char <= high for low, high in self.ranges)
        return found != self.negated


# A segment is a literal name, a whole ``**``, or tokens: single literal
# characters, the wildcards "*" and "?", and bracket expressions. Without
# escapes a literal "*" or "?" cannot occur, so the strings are unambiguous.
_Segment: TypeAlias = str | _Globstar | tuple[str | _Class, ...]


def _read_regular_file(path: str, limit: int) -> bytes:
    # Non-blocking, so that a FIFO in the settings file's place cannot stall the hook.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _NotAFile
        handle = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        return handle.read(limit)


def _reject_constant(name: str) -> None:
    raise ValueError(f"{name} is not JSON")


def _unapplied(settings_path: str, reason: str) -> Excludes:
    excludes = Excludes((), settings_path)
    excludes.warnings.append(f"{settings_path}: {reason}; its {_KEY} are not applied")
    return excludes


def _shown(pattern: str) -> str:
    if len(pattern) > _SHOWN_PATTERN_LENGTH:
        pattern = pattern[:_SHOWN_PATTERN_LENGTH] + "..."
    return json.dumps(pattern, ensure_ascii=False)


def _unsupported(pattern: str) -> str | None:
    """What keeps ``pattern`` from being matched as Claude Code was recorded matching it."""
    if not pattern:
        return "an empty pattern"
    if len(pattern) > _MAX_PATTERN_LENGTH:
        return f"more than {_MAX_PATTERN_LENGTH} characters"
    if pattern.startswith("!"):
        return "negation"
    if "\\" in pattern:
        return "a backslash"
    if any(char in pattern for char in "()|"):
        return "parentheses or |"
    outside = []
    position = 0
    while (start := pattern.find("[", position)) >= 0:
        end = _class_end(pattern, start)
        if end < 0 or _parse_class(pattern[start + 1 : end]) is None:
            return "a bracket expression"
        outside.append(pattern[position:start])
        position = end + 1
    outside.append(pattern[position:])
    if "]" in "".join(outside):
        return "a bracket expression"
    if any(name in (".", "..") for name in pattern.split("/")):
        return "a . or .. segment"
    return None


def _class_end(text: str, start: int) -> int:
    """The index of the ``]`` closing the bracket expression at ``start``, or -1."""
    return text.find("]", start + 2 if text[start + 1 : start + 2] == "^" else start + 1)


def _parse_class(body: str) -> _Class | None:
    """The bracket expression with ``body`` between its brackets, or ``None`` if unsupported."""
    negated = body.startswith("^")
    if negated:
        body = body[1:]
    if not body or any(char in body for char in "[/{},"):
        return None
    chars = []
    ranges = []
    index = 0
    while index < len(body):
        if index + 2 < len(body) and body[index + 1] == "-":
            if body[index] > body[index + 2]:
                return None
            ranges.append((body[index], body[index + 2]))
            index += 3
        else:
            chars.append(body[index])
            index += 1
    return _Class(negated, "".join(chars), tuple(ranges))


def _expand_braces(text: str) -> list[str]:
    """The brace alternatives of ``text``, in order."""
    start = text.find("{")
    closing = text.find("}")
    if start < 0:
        if closing >= 0:
            raise _Unsupported("an unmatched brace")
        return [text]
    if 0 <= closing < start:
        raise _Unsupported("an unmatched brace")
    depth = 0
    parts = []
    part_start = start + 1
    end = -1
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
            if depth > _MAX_BRACE_DEPTH:
                raise _Unsupported(f"braces nested more than {_MAX_BRACE_DEPTH} deep")
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
        elif char == "," and depth == 1:
            parts.append(text[part_start:index])
            part_start = index + 1
    if end < 0:
        raise _Unsupported("an unmatched brace")
    parts.append(text[part_start:end])
    if len(parts) == 1:
        alternatives = _integer_range(parts[0])
    elif "" in parts:
        raise _Unsupported("an empty brace alternative")
    else:
        alternatives = [expanded for part in parts for expanded in _expand_braces(part)]
    suffixes = _expand_braces(text[end + 1 :])
    if len(alternatives) * len(suffixes) > _MAX_ALTERNATIVES:
        raise _Unsupported(f"more than {_MAX_ALTERNATIVES} brace alternatives")
    prefix = text[:start]
    return [prefix + alternative + suffix for alternative in alternatives for suffix in suffixes]


def _integer_range(text: str) -> list[str]:
    low, separator, high = text.partition("..")
    if not separator or not _is_plain_integer(low) or not _is_plain_integer(high):
        raise _Unsupported("braces without a comma or an integer range")
    first, last = int(low), int(high)
    step = 1 if last >= first else -1
    if abs(last - first) >= _MAX_ALTERNATIVES:
        raise _Unsupported(f"more than {_MAX_ALTERNATIVES} brace alternatives")
    return [str(number) for number in range(first, last + step, step)]


def _is_plain_integer(text: str) -> bool:
    digits = text[1:] if text.startswith("-") else text
    return digits.isascii() and digits.isdigit() and (digits == "0" or not digits.startswith("0"))


def _compile(pattern: str) -> tuple[_Segment, ...] | None:
    """The segments of an expanded pattern, or ``None`` when it matches no absolute path."""
    names = pattern.split("/")
    if names[0] == "":
        names = names[1:]
    elif names[0] != "**":
        return None
    return tuple(_compile_segment(name) for name in names)


def _compile_segment(name: str) -> _Segment:
    if name == "**":
        return _GLOBSTAR
    if not any(char in name for char in "*?["):
        return name
    tokens: list[str | _Class] = []
    index = 0
    while index < len(name):
        char = name[index]
        if char == "[":
            end = _class_end(name, index)
            parsed = _parse_class(name[index + 1 : end])
            # _unsupported has rejected every pattern with an invalid class.
            assert parsed is not None
            tokens.append(parsed)
            index = end + 1
            continue
        if not (char == "*" and tokens and tokens[-1] == "*"):
            tokens.append(char)
        index += 1
    return tuple(tokens)


def _with_resolved_prefix(segments: tuple[_Segment, ...]) -> list[tuple[_Segment, ...]]:
    """``segments``, and them with their leading literal directories resolved when that differs.

    Claude Code 2.1.273 applied a pattern written through a linked directory to
    the files under the directory it leads to, but a pattern naming a linked
    file did not exclude the file the link leads to (G10-excludes-links).
    """
    fixed = 0
    while fixed < len(segments) - 1 and isinstance(segments[fixed], str) and segments[fixed]:
        fixed += 1
    if fixed == 0:
        return [segments]
    base = "/" + "/".join(str(segment) for segment in segments[:fixed])
    try:
        resolved = os.path.realpath(base)
    except (OSError, ValueError):
        return [segments]
    if resolved == base:
        return [segments]
    resolved_segments = tuple(name for name in resolved.split("/")[1:] if name)
    return [segments, resolved_segments + segments[fixed:]]


def _match_segments(pattern: tuple[_Segment, ...], parts: list[str]) -> bool:
    # A ``**`` segment matches any run of segments. On a mismatch the last ``**``
    # takes one more segment, so matching takes at most pattern * path steps.
    index = part = 0
    star = star_part = -1
    while part < len(parts):
        if index < len(pattern) and pattern[index] is _GLOBSTAR:
            star, star_part = index, part
            index += 1
        elif index < len(pattern) and _match_segment(pattern[index], parts[part]):
            index += 1
            part += 1
        elif star >= 0:
            star_part += 1
            index, part = star + 1, star_part
        else:
            return False
    while index < len(pattern) and pattern[index] is _GLOBSTAR:
        index += 1
    return index == len(pattern)


def _match_segment(segment: _Segment, name: str) -> bool:
    if isinstance(segment, str):
        return segment == name
    if isinstance(segment, _Globstar):
        return True
    # The same backtracking as for segments, one character at a time.
    index = position = 0
    star = star_position = -1
    while position < len(name):
        token = segment[index] if index < len(segment) else None
        if token == "*":
            star, star_position = index, position
            index += 1
        elif token is not None and (
            token == "?"
            or (isinstance(token, _Class) and token.matches(name[position]))
            or token == name[position]
        ):
            index += 1
            position += 1
        elif star >= 0:
            star_position += 1
            index, position = star + 1, star_position
        else:
            return False
    while index < len(segment) and segment[index] == "*":
        index += 1
    return index == len(segment)
