"""Remove block-level HTML comments from Markdown the way Claude Code does.

Claude Code 2.1.273 tokenizes a rule body with marked (a 15-17 release, GFM
off) when the body contains ``<!--``. For every top-level HTML token whose raw
text starts with ``<!--`` and contains ``-->``, it deletes each ``<!-- ... -->``
from that raw text and keeps what is left only if it is not blank; every other
token is kept verbatim. The result is the concatenation of the raw texts, so
line endings are normalized to ``\\n``.

This module does not reimplement marked. It finds the lines where marked would
start a top-level HTML comment token by tracking the block structures that
decide it: fenced and indented code, block quotes, lists, other HTML blocks and
paragraphs, which an HTML comment may interrupt.
"""

from __future__ import annotations

import re

__all__ = ["strip_block_comments"]

_COMMENT_TOKEN = re.compile(r" {0,3}<!--(?:-?>|[\s\S]*?(?:-->|\Z))[^\n]*(?:\n+|\Z)")
_COMMENT = re.compile(r"<!--[\s\S]*?-->")
_COMMENT_START = re.compile(r" {0,3}<!--")
_FENCE_OPEN = re.compile(r" {0,3}(`{3,}(?=[^`\n]*\Z)|~{3,})")
_BLANK = re.compile(r"[ \t]*\Z")
_INDENTED_CODE = re.compile(r"(?: {4}| {0,3}\t)[^\n]*\S")
_HEADING = re.compile(r" {0,3}#{1,6}(?:[ \t]|\Z)")
_HR = re.compile(r" {0,3}(?:(?:-[\t ]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})\Z")
_BLOCKQUOTE = re.compile(r" {0,3}>")
_BULLET = re.compile(r"( {0,3})([*+-]|\d{1,9}[.)])([ \t][^\n]*|\Z)")
_INTERRUPTING_BULLET = re.compile(r" {0,3}(?:[*+-]|1[.)])[ \t]+\S")
_SETEXT = re.compile(r" {0,3}(?:=+|-+) *\Z")
_HTML_BLOCK_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|"
    "details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|"
    "h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|meta|nav|noframes|"
    "ol|optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|"
    "tr|track|ul"
)
_HTML_RAW_START = re.compile(r" {0,3}<(script|pre|style|textarea)[\s>]", re.IGNORECASE)
_HTML_BLOCK_START = re.compile(rf" {{0,3}}</?(?:{_HTML_BLOCK_TAGS})(?: +|\Z|/?>)", re.IGNORECASE)
# HTML blocks that end at a delimiter rather than at a blank line.
_HTML_DELIMITED = (
    (re.compile(r" {0,3}<\?"), "?>"),
    (re.compile(r" {0,3}<![A-Z]"), ">"),
    (re.compile(r" {0,3}<!\[CDATA\["), "]]>"),
)
_HTML_GENERIC_START = re.compile(
    r" {0,3}(?:<(?!(?:script|pre|style|textarea)\b)[a-z][\w-]*(?:\s[^<>]*)?/?>"
    r"|</(?!(?:script|pre|style|textarea)\b)[a-z][\w-]*\s*>)[ \t]*\Z",
    re.IGNORECASE,
)
# What may interrupt a paragraph. HTML must start at column 0 to do so.
_PARAGRAPH_HTML_INTERRUPT = re.compile(
    rf"</?(?:{_HTML_BLOCK_TAGS})(?: +|\Z|/?>)|<(?:script|pre|style|textarea|!--)", re.IGNORECASE
)
_BULLET_WITH_SPACE = re.compile(r" {0,3}(?:[*+-]|\d{1,9}[.)]) ")
_INDENTED_CODE_START = re.compile(r"(?: {4}| {0,3}\t)\S")
_SINGLE_TAG_LINE = re.compile(r" {0,3}<[^\n>]+>\Z")
_DEFINITION = re.compile(r" {0,3}\[((?:\\.|[^\[\]\\])+)\]: *\S")


def strip_block_comments(body: str) -> str:
    """Return ``body`` without the HTML comments Claude Code strips."""
    if "<!--" not in body:
        return body
    text = re.sub(r"\r\n|\r", "\n", body)
    return _Scanner(text).run()


class _Scanner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.definitions: set[str] = set()
        self.lines = text.split("\n")
        # Offset of each line in text.
        self.offsets: list[int] = []
        offset = 0
        for line in self.lines:
            self.offsets.append(offset)
            offset += len(line) + 1

    def run(self) -> str:
        output: list[str] = []
        row = 0
        copied_until = 0
        while row < len(self.lines):
            line = self.lines[row]
            if _COMMENT_START.match(line):
                start = self.offsets[row]
                token = _COMMENT_TOKEN.match(self.text, start)
                assert token is not None
                output.append(self.text[copied_until:start])
                raw = token.group(0)
                if "-->" in raw.lstrip():
                    remainder = _COMMENT.sub("", raw)
                    if remainder.strip():
                        output.append(remainder)
                else:
                    output.append(raw)
                copied_until = token.end()
                row = self._row_at(token.end())
                continue
            if (definition := _DEFINITION.match(line)) is not None:
                label = re.sub(r"\s+", " ", definition.group(1)).lower()
                if label in self.definitions:
                    # marked keeps the first definition of a label and drops
                    # the raw text of later ones.
                    output.append(self.text[copied_until : self.offsets[row]])
                    row += 1
                    copied_until = self.offsets[row] if row < len(self.lines) else len(self.text)
                    continue
                self.definitions.add(label)
                row += 1
                continue
            row = self._skip_block(row)
        output.append(self.text[copied_until:])
        return "".join(output)

    def _row_at(self, offset: int) -> int:
        if offset >= len(self.text):
            return len(self.lines)
        row = 0
        while row + 1 < len(self.offsets) and self.offsets[row + 1] <= offset:
            row += 1
        return row

    # Each skipper returns the first row after the block starting at ``row``.

    def _skip_block(self, row: int) -> int:
        line = self.lines[row]
        if _BLANK.match(line):
            return row + 1
        if fence := _FENCE_OPEN.match(line):
            return self._skip_fence(row, fence.group(1))
        if _INDENTED_CODE.match(line):
            return self._skip_indented_code(row)
        if _HEADING.match(line) or _HR.match(line):
            return row + 1
        if _BLOCKQUOTE.match(line):
            return self._skip_blockquote(row)
        if bullet := _BULLET.match(line):
            return self._skip_list(row, bullet)
        if raw := _HTML_RAW_START.match(line):
            return self._skip_until(row, re.compile(rf"</{raw.group(1)}>", re.IGNORECASE))
        for start, end in _HTML_DELIMITED:
            if start.match(line):
                return self._skip_until(row, re.compile(re.escape(end)))
        if _HTML_BLOCK_START.match(line) or _HTML_GENERIC_START.match(line):
            return self._skip_html(row)
        return self._skip_paragraph(row)

    def _skip_fence(self, row: int, marker: str) -> int:
        closing = re.compile(rf" {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*\Z")
        row += 1
        while row < len(self.lines):
            if closing.match(self.lines[row]):
                return row + 1
            row += 1
        return row

    def _skip_indented_code(self, row: int) -> int:
        row += 1
        while row < len(self.lines):
            line = self.lines[row]
            if not (_BLANK.match(line) or _INDENTED_CODE.match(line)):
                break
            row += 1
        return row

    def _interrupts_paragraph(self, line: str) -> bool:
        return bool(
            _FENCE_OPEN.match(line)
            or _HEADING.match(line)
            or _HR.match(line)
            or _BLOCKQUOTE.match(line)
            or _INTERRUPTING_BULLET.match(line)
            or _PARAGRAPH_HTML_INTERRUPT.match(line)
        )

    def _skip_until(self, row: int, end: re.Pattern[str]) -> int:
        # The block ends on the line containing the delimiter; following
        # blank lines belong to it too, which does not change the output.
        start = self.offsets[row]
        match = end.search(self.text, start)
        if match is None:
            return len(self.lines)
        return self._row_at(match.end()) + 1

    def _skip_setext_heading(self, row: int) -> int | None:
        """Return the row after a setext heading starting at ``row``, if there is one.

        A setext heading is tried before a paragraph, and inside it only a line
        holding a single HTML tag interrupts the text, not an HTML comment.
        """
        next_row = row + 1
        while next_row < len(self.lines):
            line = self.lines[next_row]
            if _SETEXT.match(line):
                return next_row + 1
            if (
                _BLANK.match(line)
                or _BULLET_WITH_SPACE.match(line)
                or _INDENTED_CODE_START.match(line)
                or _FENCE_OPEN.match(line)
                or _BLOCKQUOTE.match(line)
                or _HEADING.match(line)
                or _SINGLE_TAG_LINE.match(line)
            ):
                return None
            next_row += 1
        return None

    def _skip_paragraph(self, row: int) -> int:
        heading_end = self._skip_setext_heading(row)
        if heading_end is not None:
            return heading_end
        row += 1
        while row < len(self.lines):
            line = self.lines[row]
            if _BLANK.match(line) or (_SETEXT.match(line) and not _HR.match(line)):
                return row + (0 if _BLANK.match(line) else 1)
            if self._interrupts_paragraph(line):
                return row
            row += 1
        return row

    def _skip_blockquote(self, row: int) -> int:
        row += 1
        while row < len(self.lines):
            line = self.lines[row]
            if _BLOCKQUOTE.match(line):
                row += 1
                continue
            if _BLANK.match(line) or self._interrupts_paragraph(line):
                return row
            row += 1
        return row

    @staticmethod
    def _content_indent(bullet: re.Match[str]) -> int:
        marker_end = len(bullet.group(1)) + len(bullet.group(2))
        rest = bullet.group(3)
        spaces = len(rest) - len(rest.lstrip(" "))
        return marker_end + (spaces if 1 <= spaces <= 4 else 1)

    def _skip_list(self, row: int, bullet: re.Match[str]) -> int:
        content_indent = self._content_indent(bullet)
        previous_blank = False
        row += 1
        while row < len(self.lines):
            line = self.lines[row]
            if _BLANK.match(line):
                previous_blank = True
                row += 1
                continue
            indent = len(line) - len(line.lstrip(" "))
            if indent >= content_indent:
                previous_blank = False
                row += 1
                continue
            limit = min(3, content_indent - 1)
            if re.match(rf" {{0,{limit}}}<(?:[a-z].*>|!--)", line, re.IGNORECASE):
                return row
            if (next_bullet := _BULLET.match(line)) is not None and not _HR.match(line):
                content_indent = self._content_indent(next_bullet)
                previous_blank = False
                row += 1
                continue
            if _FENCE_OPEN.match(line) or _HEADING.match(line) or _HR.match(line):
                return row
            if previous_blank:
                return row
            row += 1
        return row

    def _skip_html(self, row: int) -> int:
        row += 1
        while row < len(self.lines):
            if _BLANK.match(self.lines[row]):
                return row
            row += 1
        return row
