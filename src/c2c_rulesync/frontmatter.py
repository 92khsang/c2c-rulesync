"""Read a rule file the way Claude Code 2.1.273 reads it.

A rule file is Markdown with optional YAML front matter. Claude Code takes the
front matter's ``paths`` value, turns it into a list of globs, and injects the
body with block-level HTML comments removed. Each step below follows the
behavior documented in ``docs/behavior.md``; the places where Claude Code's
behavior surprises (a failed YAML parse makes a rule unconditional, ``src/**``
becomes ``src``) are kept on purpose.
"""

from __future__ import annotations

import re
from typing import Any

from c2c_rulesync import bun_yaml
from c2c_rulesync.markdown_blocks import strip_block_comments

__all__ = ["RuleText", "normalize_globs", "parse_rule_text", "strip_html_comments"]

# JavaScript's white space and line terminators, as matched by \s and removed
# by String.prototype.trim.
_JS_WHITESPACE = (
    "\t\n\x0b\x0c\r \xa0\U00001680"
    + "".join(map(chr, range(0x2000, 0x200B)))
    + "\U00002028\U00002029\U0000202f\U0000205f\U00003000\U0000feff"
)
_JS_WHITESPACE_CLASS = "[" + re.escape(_JS_WHITESPACE) + "]"
# Front matter opens with `---` at the very start and closes at the first
# later `---`, even one in the middle of a line.
_FRONT_MATTER = re.compile(rf"---{_JS_WHITESPACE_CLASS}*\n([\s\S]*?)---{_JS_WHITESPACE_CLASS}*\n?")
# A top-level `key: value` line whose value Claude Code quotes when the first
# parse fails. JavaScript's `.` stops at line terminators, so a line ending in
# `\r` never matches.
_REWRITABLE_LINE = re.compile(
    rf"([a-zA-Z_-]+):{_JS_WHITESPACE_CLASS}+([^\n\r\U00002028\U00002029]+)\Z"
)
_NEEDS_QUOTES = re.compile(r"[{}\[\]*&#!|>%@`]|: ")
_LEADING_TABS = re.compile("(?:^|(?<=[\n\r\U00002028\U00002029]))\t+")
_BRACE_GROUP = re.compile(r"([^{]*)\{([^}]+)\}([^\n\r\U00002028\U00002029]*)\Z")
_BRACE_RESULTS_BUDGET = 1000
_BRACE_BYTES_BUDGET = 4 * 1024 * 1024


class RuleText:
    """A parsed rule file.

    Attributes:
        body: The text injected for the rule.
        globs: The rule's globs, or ``None`` when the rule applies unconditionally.
        warnings: Human-readable notes about how the file was interpreted.
    """

    __slots__ = ("body", "globs", "warnings")

    def __init__(self, body: str, globs: tuple[str, ...] | None, warnings: tuple[str, ...]) -> None:
        self.body = body
        self.globs = globs
        self.warnings = warnings

    def __repr__(self) -> str:
        return f"RuleText(body={self.body!r}, globs={self.globs!r}, warnings={self.warnings!r})"


def parse_rule_text(text: str) -> RuleText:
    """Split a rule file into its injected body and its globs."""
    warnings: list[str] = []
    stripped = text[1:] if text.startswith("\U0000feff") else text
    match = _FRONT_MATTER.match(stripped)
    if match is None:
        front_matter: dict[str, Any] = {}
        body = text
    else:
        front_matter = _parse_front_matter(match.group(1), warnings)
        body = stripped[match.end() :]
    globs = _globs(front_matter.get("paths"), warnings)
    if "<!--" in body:
        body = strip_html_comments(body)
    return RuleText(body, globs, tuple(warnings))


def _parse_front_matter(text: str, warnings: list[str]) -> dict[str, Any]:
    try:
        try:
            value = bun_yaml.parse(text)
        except bun_yaml.YamlError:
            retried = _LEADING_TABS.sub(
                lambda m: "  " * len(m.group(0)), _quote_special_values(text)
            )
            try:
                value = bun_yaml.parse(retried)
            except bun_yaml.YamlError as error:
                warnings.append(
                    f"front matter is not valid YAML ({error}); Claude Code ignores it and "
                    "loads this rule for every file"
                )
                return {}
    except bun_yaml.UnsupportedYaml as error:
        warnings.append(
            f"front matter uses YAML c2c-rulesync cannot interpret exactly ({error}); "
            "`paths` was read line by line and may differ from Claude Code"
        )
        return _best_effort_front_matter(text)
    return value if isinstance(value, dict) else {}


def _quote_special_values(text: str) -> str:
    """Quote top-level values containing YAML indicators, as Claude Code's retry does."""
    lines = []
    for line in text.split("\n"):
        match = _REWRITABLE_LINE.match(line)
        if match is None:
            lines.append(line)
            continue
        key, value = match.group(1), match.group(2)
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            lines.append(line)
            continue
        if value.startswith("[") and value.endswith("]") and _parses_as_list(value):
            lines.append(line)
            continue
        if _NEEDS_QUOTES.search(value):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}: "{escaped}"')
            continue
        lines.append(line)
    return "\n".join(lines)


def _parses_as_list(value: str) -> bool:
    try:
        return isinstance(bun_yaml.parse(value), list)
    except bun_yaml.YamlError:
        return False


def _best_effort_front_matter(text: str) -> dict[str, Any]:
    lines = text.split("\n")
    for index, line in enumerate(lines):
        match = re.match(r"paths:[ \t]*(.*?)[ \t]*\r?\Z", line)
        if match is None:
            continue
        value = match.group(1)
        if value and not value.startswith("#"):
            return {"paths": _unquote(value)}
        items = []
        for following in lines[index + 1 :]:
            item = re.match(r"[ \t]*-[ \t]+(.*?)[ \t]*\r?\Z", following)
            if item is None:
                if following.strip() == "":
                    continue
                break
            items.append(_unquote(item.group(1)))
        return {"paths": items}
    return {}


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return re.sub(r"[ \t]+#.*\Z", "", value)


# Globs ----------------------------------------------------------------------


def _globs(paths: Any, warnings: list[str]) -> tuple[str, ...] | None:
    if not _js_truthy(paths):
        return None
    globs = [
        glob[:-3] if glob.endswith("/**") else glob for glob in normalize_globs(paths, warnings)
    ]
    globs = [glob for glob in globs if glob]
    if not globs or all(glob == "**" for glob in globs):
        return None
    return tuple(globs)


def _js_truthy(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == value and value != 0
    if isinstance(value, str):
        return value != ""
    return True


def normalize_globs(paths: Any, warnings: list[str] | None = None) -> list[str]:
    """Flatten a ``paths`` value into globs, splitting commas and expanding braces.

    Strings are split on commas outside braces and trimmed; nested lists are
    flattened and other values ignored. Brace groups expand left to right, and
    a rule's expansions share one budget of 1,000 results and 4 MiB; an item
    that would exceed it is kept unexpanded.
    """
    budget = [_BRACE_RESULTS_BUDGET, _BRACE_BYTES_BUDGET]
    return _normalize(paths, budget, warnings if warnings is not None else [])


def _normalize(value: Any, budget: list[int], warnings: list[str]) -> list[str]:
    if isinstance(value, list):
        return [glob for item in value for glob in _normalize(item, budget, warnings)]
    if not isinstance(value, str):
        return []
    items: list[str] = []
    current: list[str] = []
    depth = 0
    for char in value:
        if char == "{":
            depth += 1
            current.append(char)
        elif char == "}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            item = _js_trim("".join(current))
            if item:
                items.append(item)
            current = []
        else:
            current.append(char)
    item = _js_trim("".join(current))
    if item:
        items.append(item)
    return [glob for item in items for glob in _expand_braces(item, budget, warnings)]


def _expand_braces(item: str, budget: list[int], warnings: list[str]) -> list[str]:
    if "{" not in item:
        return [item]
    item_length = _utf16_length(item)
    results: list[str] = []
    stack = [item]
    while stack:
        current = stack.pop()
        match = _BRACE_GROUP.match(current)
        if match is None:
            results.append(current)
            continue
        prefix, alternatives, suffix = match.group(1), match.group(2), match.group(3)
        options = [_js_trim(option) for option in alternatives.split(",")]
        budget[1] -= _utf16_length(current)
        projected = len(results) + len(stack) + len(options)
        if budget[1] < 0 or projected > budget[0] or projected * item_length > budget[1]:
            warnings.append(
                f"brace expansion of {item[:256]!r} exceeds the budget; kept unexpanded"
            )
            return [item]
        stack.extend(prefix + option + suffix for option in reversed(options))
    budget[0] -= len(results)
    budget[1] -= len(results) * item_length
    return results


def _js_trim(text: str) -> str:
    return text.strip(_JS_WHITESPACE)


def _utf16_length(text: str) -> int:
    return len(text) + sum(1 for char in text if ord(char) > 0xFFFF)


# HTML comments --------------------------------------------------------------


def strip_html_comments(body: str) -> str:
    """Remove block-level HTML comments the way Claude Code does."""
    return strip_block_comments(body)
