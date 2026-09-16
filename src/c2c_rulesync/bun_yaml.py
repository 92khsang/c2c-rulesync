"""Predict what ``Bun.YAML.parse`` returns for rule front matter.

Claude Code parses a rule's front matter with Bun's YAML parser, and whether
that parse succeeds decides whether the rule is path-scoped. This module
reproduces Bun's results for the YAML that front matter realistically contains:
block mappings and sequences, flow collections, plain, quoted and block
scalars, anchors, aliases, tags and comments. Bun departs from the YAML 1.2
specification in several places, and the behavior here follows Bun, as
recorded in ``tests/vectors/bun_yaml.json``.

``parse`` raises ``YamlError`` where Bun throws, and ``UnsupportedYaml`` for
constructs whose outcome this module does not model, such as explicit keys or
directives, so callers can tell a confident answer from a guess.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["UnsupportedYaml", "YamlError", "parse"]


class YamlError(ValueError):
    """``Bun.YAML.parse`` throws for this document."""


class UnsupportedYaml(ValueError):
    """The document uses YAML this module does not model."""


_NULLS = frozenset({"", "~", "null", "Null", "NULL"})
_TRUE = frozenset({"true", "True", "TRUE"})
_FALSE = frozenset({"false", "False", "FALSE"})
_DECIMAL = re.compile(r"[-+]?[0-9]+\Z")
_HEX = re.compile(r"([-+]?)0x([0-9a-fA-F]+)\Z")
_OCTAL = re.compile(r"([-+]?)0o([0-7]+)\Z")
_FLOAT = re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?\Z")
_INFINITY = re.compile(r"([-+]?)\.(?:inf|Inf|INF)\Z")
_NAN = re.compile(r"\.(?:nan|NaN|NAN)\Z")
_DOUBLE_QUOTE_ESCAPES = {
    "0": "\x00",
    "a": "\x07",
    "b": "\x08",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\x0b",
    "f": "\x0c",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\U00002028",
    "P": "\U00002029",
}
_HEX_ESCAPE_LENGTHS = {"x": 2, "u": 4, "U": 8}
_MAX_EXACT_DIGITS = 300
_MAX_PREFIXED_INTEGER = 2**64 - 1
# A sequence entry or explicit key indicator.
_BLOCK_INDICATOR = re.compile(r"[-?](?:[ \t]|\Z)")
_FLOW_INDICATORS = frozenset(",[]{}")
_LINE_BREAK = re.compile(r"(\r\n|\r|\n)")


def resolve_plain(text: str) -> Any:
    """Resolve an untagged plain scalar the way Bun does."""
    if text in _NULLS:
        return None
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    if _DECIMAL.match(text):
        # Only equality with zero matters to callers; a float also avoids
        # Python's limit on converting long digit strings to int.
        return int(text) if len(text) <= _MAX_EXACT_DIGITS else float(text)
    if match := _HEX.match(text):
        return _prefixed_integer(text, match, 16)
    if match := _OCTAL.match(text):
        return _prefixed_integer(text, match, 8)
    if _FLOAT.match(text):
        return float(text)
    if match := _INFINITY.match(text):
        return float("-inf") if match.group(1) == "-" else float("inf")
    if _NAN.match(text):
        return float("nan")
    return text


def _prefixed_integer(text: str, match: re.Match[str], base: int) -> int | str:
    """A hexadecimal or octal integer, which Bun keeps as a string past 64 bits."""
    digits = match.group(2).lstrip("0") or "0"
    if len(digits) > 64:
        return text
    value = int(digits, base)
    if value > _MAX_PREFIXED_INTEGER:
        return text
    return -value if match.group(1) == "-" else value


def parse(text: str) -> Any:
    """Return what ``Bun.YAML.parse(text)`` returns.

    Raises:
        YamlError: Bun throws for ``text``.
        UnsupportedYaml: ``text`` uses YAML outside what this module models.
    """
    try:
        return _Parser(text).parse_stream()
    except RecursionError:
        raise UnsupportedYaml("nesting deeper than c2c-rulesync follows") from None


class _Parser:
    def __init__(self, text: str) -> None:
        # Bun breaks lines at a carriage return as well as at a line feed.
        parts = _LINE_BREAK.split(text)
        self.lines: list[str] = parts[::2]
        # Rows whose line break includes a carriage return.
        self.cr_rows: set[int] = set()
        for row, line_break in enumerate(parts[1::2]):
            if "\r" not in line_break:
                continue
            self.cr_rows.add(row)
            line = self.lines[row]
            if line_break == "\r" and "\t" in line and line.strip(" \t") == "":
                # Bun's handling of such lines depends on what precedes them.
                raise UnsupportedYaml("white space with a tab ended by a carriage return")
        self.anchors: dict[str, Any] = {}
        # Whether the node parsed last ended in something other than plain or
        # block scalar text: a quote, bracket, alias, comment or empty node.
        self.closed = False
        # How many values of mapping keys that are not their mapping's first
        # are being parsed.
        self.later_values = 0

    # Line helpers -----------------------------------------------------------

    def indent_of(self, row: int) -> int:
        line = self.lines[row]
        return len(line) - len(line.lstrip(" "))

    def is_blank(self, row: int) -> bool:
        stripped = self.lines[row].strip(" \t")
        return stripped == "" or stripped.startswith("#")

    def next_content_row(self, row: int) -> int | None:
        closed = self.closed
        while row < len(self.lines):
            if not self.is_blank(row):
                self.check_tab_indentation(row)
                return row
            line = self.lines[row]
            # Inside the value of a mapping key that is not its mapping's
            # first, Bun rejects a blank or comment line starting with a tab
            # after anything but plain or block scalar text.
            if self.later_values and closed and line.startswith("\t"):
                raise YamlError("Tab characters cannot be used as indentation")
            closed = closed or line.lstrip(" \t").startswith("#")
            row += 1
        return None

    def check_tab_indentation(self, row: int) -> None:
        line = self.lines[row]
        spaces = self.indent_of(row)
        if line[spaces : spaces + 1] == "\t":
            if _starts_block_entry(line.lstrip(" \t")):
                raise YamlError("Tab characters cannot be used as indentation")
            raise UnsupportedYaml("tab before a scalar or flow node")

    # Stream and documents ---------------------------------------------------

    def parse_stream(self) -> Any:
        documents: list[Any] = []
        row: int | None = 0
        while True:
            row = self.next_content_row(row) if row is not None else None
            if row is None:
                break
            line = self.lines[row]
            if line.startswith("%") and not any(
                later == "---" or later.startswith(("--- ", "---\t")) for later in self.lines[row:]
            ):
                raise YamlError("Unexpected token")
            if line.startswith("%") or line == "---" or line.startswith("--- "):
                raise UnsupportedYaml("directives and document start markers")
            if self._is_document_end(row):
                row += 1
                continue
            node, row = self.block_node(row, -1)
            next_row = self.next_content_row(row) if row < len(self.lines) else None
            documents.append(node)
            if next_row is None:
                break
            if not self._is_document_end(next_row):
                raise YamlError("Unexpected token")
            row = next_row + 1
        if not documents:
            return None
        if len(documents) == 1:
            return documents[0]
        return documents

    def _is_document_end(self, row: int) -> bool:
        line = self.lines[row]
        return line == "..." or line.startswith("... ") or line.startswith("...\t")

    # Block structure --------------------------------------------------------

    def block_node(self, row: int, parent_indent: int) -> tuple[Any, int]:
        """Parse the block node whose first content line is ``row``.

        Returns the node and the row after it.
        """
        indent = self.indent_of(row)
        column = indent
        if self._starts_sequence_entry(row, column):
            return self.block_sequence(row, column)
        if self._key_at(row, column) is not None:
            return self.block_mapping(row, column)
        return self.inline_node(row, column, parent_indent, allow_key=False)

    def _starts_sequence_entry(self, row: int, column: int) -> bool:
        line = self.lines[row]
        return line[column : column + 1] == "-" and line[column + 1 : column + 2] in ("", " ", "\t")

    def block_sequence(self, row: int, column: int) -> tuple[list[Any], int]:
        items: list[Any] = []
        while True:
            item_column = column + 1
            line = self.lines[row]
            while line[item_column : item_column + 1] in (" ", "\t"):
                item_column += 1
            rest = line[item_column:]
            if "\t" in line[column + 1 : item_column] and _starts_block_entry(rest):
                raise YamlError("Tab characters cannot be used as indentation")
            if rest == "" or rest.startswith("#"):
                self.closed = True
                next_row = self.next_content_row(row + 1)
                if next_row is not None and self.indent_of(next_row) > column:
                    item, row = self.block_node(next_row, column)
                else:
                    item, row = None, row + 1
            else:
                item, row = self.inline_node(
                    row, item_column, column, allow_key=True, in_sequence=True
                )
            items.append(item)
            next_row = self.next_content_row(row)
            if next_row is None:
                return items, len(self.lines)
            next_indent = self.indent_of(next_row)
            if next_indent < column or self._is_document_end(next_row):
                return items, next_row
            if next_indent > column:
                raise YamlError("Unexpected token")
            if not self._starts_sequence_entry(next_row, column):
                return items, next_row
            row = next_row

    def block_mapping(self, row: int, column: int) -> tuple[dict[str, Any], int]:
        mapping: dict[str, Any] = {}
        while True:
            found = self._key_at(row, column)
            if found is None:
                raise YamlError("Unexpected token")
            key, value_column = found
            later = int(bool(mapping))
            self.later_values += later
            try:
                value, row = self.mapping_value(row, value_column, column)
                mapping[key] = value
                next_row = self.next_content_row(row)
            finally:
                self.later_values -= later
            if next_row is None:
                return mapping, len(self.lines)
            next_indent = self.indent_of(next_row)
            if next_indent < column or self._is_document_end(next_row):
                return mapping, next_row
            if next_indent > column or self._starts_sequence_entry(next_row, column):
                raise YamlError("Unexpected token")
            row = next_row

    def mapping_value(self, row: int, column: int, mapping_column: int) -> tuple[Any, int]:
        line = self.lines[row]
        while line[column : column + 1] in (" ", "\t"):
            column += 1
        rest = line[column:]
        if rest == "" or rest.startswith("#"):
            self.closed = True
            next_row = self.next_content_row(row + 1)
            if next_row is None:
                return None, len(self.lines)
            next_indent = self.indent_of(next_row)
            if next_indent > mapping_column:
                return self.block_node(next_row, mapping_column)
            if next_indent == mapping_column and self._starts_sequence_entry(next_row, next_indent):
                return self.block_sequence(next_row, next_indent)
            return None, row + 1
        return self.inline_node(row, column, mapping_column, allow_key=False, mapping_value=True)

    def _key_at(self, row: int, column: int) -> tuple[str, int] | None:
        """If a mapping key starts at ``column``, return it and the column after its colon."""
        line = self.lines[row]
        first = line[column : column + 1]
        if first in ('"', "'"):
            end = self._single_line_quoted_end(line, column)
            if end is None:
                return None
            after = end
            while line[after : after + 1] in (" ", "\t"):
                after += 1
            if line[after : after + 1] == ":" and line[after + 1 : after + 2] in ("", " ", "\t"):
                raw = line[column:end]
                return _decode_single_line_quoted(raw), after + 1
            return None
        if first in ("[", "{"):
            if re.search(r"[\]}]\s*:(?:\s|$)", line[column:]):
                raise UnsupportedYaml("flow collection as a mapping key")
            return None
        if first == "?" and line[column + 1 : column + 2] in ("", " ", "\t"):
            raise UnsupportedYaml("explicit mapping key")
        if first == ":" and line[column + 1 : column + 2] in ("", " ", "\t"):
            raise UnsupportedYaml("empty mapping key")
        if first in ("&", "!", "*"):
            if re.search(r":(?:\s|$)", line[column:]):
                raise UnsupportedYaml("properties or alias on a mapping key")
            return None
        index = column
        while index < len(line):
            char = line[index]
            if char == "#" and index > column and line[index - 1] in (" ", "\t"):
                return None
            if char == ":" and line[index + 1 : index + 2] in ("", " ", "\t"):
                key = line[column:index].rstrip(" \t")
                if key == "":
                    return None
                if key[0] in ("@", "`", "|", ">"):
                    raise YamlError("Unexpected token")
                if key[0] == "%":
                    raise UnsupportedYaml("key starting with %")
                return _js_key(resolve_plain(key)), index + 1
            index += 1
        return None

    @staticmethod
    def _single_line_quoted_end(line: str, column: int) -> int | None:
        quote = line[column]
        index = column + 1
        while index < len(line):
            char = line[index]
            if quote == "'" and char == "'":
                if line[index + 1 : index + 2] == "'":
                    index += 2
                    continue
                return index + 1
            if quote == '"':
                if char == "\\":
                    index += 2
                    continue
                if char == '"':
                    return index + 1
            index += 1
        return None

    # Nodes that start in the middle of a line ------------------------------

    def inline_node(
        self,
        row: int,
        column: int,
        parent_indent: int,
        *,
        allow_key: bool,
        in_sequence: bool = False,
        mapping_value: bool = False,
    ) -> tuple[Any, int]:
        line = self.lines[row]
        anchor, tag, column = self._properties(row, column)
        rest = line[column:]
        if (anchor is not None or tag is not None) and (rest == "" or rest.startswith("#")):
            self.closed = True
            next_row = self.next_content_row(row + 1)
            if next_row is not None and self.indent_of(next_row) > parent_indent:
                next_line = self.lines[next_row]
                if tag is not None or next_line[self.indent_of(next_row) :][:1] in ("&", "!"):
                    raise UnsupportedYaml("properties applied to a node on the next line")
                value, end_row = self.block_node(next_row, parent_indent)
            elif (
                mapping_value
                and next_row is not None
                and self.indent_of(next_row) == parent_indent
                and self._starts_sequence_entry(next_row, parent_indent)
            ):
                # A block sequence may sit at its key's indentation; Bun then
                # ignores the tag.
                value, end_row = self.block_sequence(next_row, parent_indent)
                return self._finish_node(value, anchor, None, raw=None), end_row
            elif tag == "!!binary":
                raise UnsupportedYaml(f"empty node with tag {tag}")
            else:
                value, end_row = _empty_value(tag), row + 1
                self.closed = True
            return self._finish_node(value, anchor, tag, raw=None), end_row
        if (
            (anchor is not None or tag is not None)
            and rest[:1] == ":"
            and rest[1:2] in ("", " ", "\t")
        ):
            raise UnsupportedYaml("properties on an empty mapping key")
        if (
            (anchor is not None or tag is not None)
            and rest[:1] == "-"
            and rest[1:2] in ("", " ", "\t")
        ):
            raise YamlError("Unexpected token")

        char = rest[:1]
        if char == "*":
            end = column + 1
            while end < len(line) and line[end] not in " \t":
                end += 1
            name = line[column + 1 : end]
            if name == "":
                raise YamlError("Unexpected EOF")
            if name not in self.anchors:
                raise YamlError("Unresolved alias")
            self._expect_line_end(row, end)
            self.closed = True
            return self.anchors[name], row + 1
        if char in ("|", ">"):
            value, end_row = self.block_scalar(row, column, parent_indent)
            self.closed = False
            return self._finish_node(value, anchor, tag, raw=None), end_row
        if char in ("[", "{"):
            value, end_row, end_column = self.flow_node(row, column, parent_indent)
            if re.match(r"[ \t]*:(?:[ \t]|$)", self.lines[end_row][end_column:]):
                raise UnsupportedYaml("flow collection as a mapping key")
            self._expect_line_end(end_row, end_column)
            self.closed = True
            return self._finish_node(value, anchor, tag, raw=None), end_row + 1
        if char in ('"', "'"):
            value, end_row, end_column = self.quoted_scalar(row, column, parent_indent)
            self._expect_line_end(end_row, end_column, strict_comment=True)
            self.closed = True
            return self._finish_node(value, anchor, tag, raw=None), end_row + 1
        if char == "-" and rest[1:2] in ("", " ", "\t"):
            if in_sequence:
                return self.block_sequence(row, column)
            raise YamlError("Unexpected token")
        if char in ("@", "`", "%", "]", "}"):
            raise YamlError("Unexpected token")
        if char == "?" and rest[1:2] in ("", " ", "\t"):
            if not in_sequence:
                raise YamlError("Unexpected token")
            raise UnsupportedYaml("explicit key")
        if allow_key and self._key_at(row, column) is not None:
            if anchor is not None or tag is not None:
                raise UnsupportedYaml("properties on a compact mapping")
            return self.block_mapping(row, column)
        raw, end_row = self.plain_scalar(row, column, parent_indent)
        return self._finish_node(raw, anchor, tag, raw=raw, plain=True), end_row

    def _properties(self, row: int, column: int) -> tuple[str | None, str | None, int]:
        """Read an optional anchor and tag, in either order, starting at ``column``."""
        line = self.lines[row]
        anchor: str | None = None
        tag: str | None = None
        while line[column : column + 1] in ("&", "!"):
            if line[column] == "&":
                if anchor is not None:
                    raise YamlError("Unexpected token")
                end = column + 1
                while end < len(line) and line[end] not in " \t,[]{}":
                    end += 1
                if end == column + 1:
                    raise YamlError(
                        "Unexpected EOF" if end >= len(line) else "Unexpected character"
                    )
                if end < len(line) and line[end] not in " \t":
                    raise YamlError("Unexpected character")
                anchor = line[column + 1 : end]
            else:
                if tag is not None:
                    raise YamlError("Unexpected token")
                end = _tag_end(line, column)
                tag = line[column:end]
            column = end
            while line[column : column + 1] in (" ", "\t"):
                column += 1
        return anchor, tag, column

    def _finish_node(
        self,
        value: Any,
        anchor: str | None,
        tag: str | None,
        *,
        raw: str | None,
        plain: bool = False,
    ) -> Any:
        if plain:
            assert raw is not None
            value = _plain_with_tag(raw, tag)
        if anchor is not None:
            self.anchors[anchor] = value
        return value

    def _expect_line_end(self, row: int, column: int, *, strict_comment: bool = False) -> None:
        rest = self.lines[row][column:]
        stripped = rest.lstrip(" \t")
        if stripped == "":
            return
        if stripped.startswith("#") and (len(stripped) < len(rest) or not strict_comment):
            if len(stripped) == len(rest):
                raise YamlError("Unexpected character")
            return
        raise YamlError("Unexpected token" if not strict_comment else "Unexpected character")

    # Scalars ----------------------------------------------------------------

    def plain_scalar(self, row: int, column: int, parent_indent: int) -> tuple[str, int]:
        first, ends_in_comment = self._plain_line(self.lines[row][column:])
        parts = [first]
        row += 1
        self.closed = ends_in_comment
        if ends_in_comment:
            return first, row
        breaks = 0
        while row < len(self.lines):
            line = self.lines[row]
            stripped = line.strip(" \t")
            if stripped == "":
                breaks += 1
                row += 1
                continue
            indent = self.indent_of(row)
            if indent <= parent_indent or (indent == 0 and self._is_document_end(row)):
                break
            if stripped.startswith("#"):
                break
            text, ends_in_comment = self._plain_line(stripped)
            parts.append("\n" * breaks if breaks else " ")
            parts.append(text)
            breaks = 0
            row += 1
            if ends_in_comment:
                self.closed = True
                break
        return "".join(parts), row

    @staticmethod
    def _plain_line(text: str) -> tuple[str, bool]:
        comment = re.search(r"[ \t]#", text)
        content = text[: comment.start()] if comment else text
        content = content.rstrip(" \t")
        if re.search(r":(?:[ \t]|$)", content):
            raise YamlError("Unexpected token")
        return content, comment is not None

    def quoted_scalar(self, row: int, column: int, parent_indent: int) -> tuple[str, int, int]:
        """Parse a quoted scalar starting at ``column``.

        Returns the value and the row and column just past the closing quote.
        """
        quote = self.lines[row][column]
        index = column + 1
        # One entry per source line: its text, and whether it ends in an
        # escaped line break (double-quoted only), which joins lines without
        # a space.
        segments: list[tuple[str, bool]] = []
        current: list[str] = []
        crosses_carriage_return = False
        while True:
            line = self.lines[row]
            escaped_break = False
            while index < len(line):
                char = line[index]
                if char == quote and quote == "'" and line[index + 1 : index + 2] == "'":
                    current.append("'")
                    index += 2
                    continue
                if char == quote:
                    if crosses_carriage_return:
                        # Bun folds such breaks differently; errors still match.
                        raise UnsupportedYaml("quoted scalar broken across carriage returns")
                    segments.append(("".join(current), False))
                    return _fold_quoted(segments), row, index + 1
                if char == "\\" and quote == '"':
                    if index + 1 == len(line):
                        escaped_break = True
                        index += 1
                        break
                    decoded, width = _double_quote_escape(line, index)
                    current.append(decoded)
                    index += width
                    continue
                current.append(char)
                index += 1
            segments.append(("".join(current), escaped_break))
            current = []
            crosses_carriage_return = crosses_carriage_return or row in self.cr_rows
            row += 1
            if row >= len(self.lines):
                raise YamlError("Unexpected EOF")
            next_line = self.lines[row]
            indent = self.indent_of(row)
            if next_line.strip(" \t") != "":
                if indent <= parent_indent:
                    raise YamlError("Unexpected character")
                if next_line[indent : indent + 1] == "\t":
                    raise UnsupportedYaml("tab in a quoted scalar continuation")
            index = len(next_line) - len(next_line.lstrip(" \t"))

    def block_scalar(self, row: int, column: int, parent_indent: int) -> tuple[str, int]:
        header = self.lines[row][column:]
        match = re.match(r"([|>])([-+]?)([1-9]?)([-+]?)(?:[ \t]+(?:#.*)?)?\Z", header)
        if match is None or (match.group(2) and match.group(4)):
            raise YamlError("Unexpected character")
        style = match.group(1)
        chomping = match.group(2) or match.group(4)
        base = max(parent_indent, 0)
        content_indent = base + int(match.group(3)) if match.group(3) else None
        lines: list[str] = []
        row += 1
        while row < len(self.lines):
            line = self.lines[row]
            spaces = len(line) - len(line.lstrip(" "))
            if line.strip(" \t") == "":
                if "\t" in line:
                    raise UnsupportedYaml("tab in a blank line of a block scalar")
                lines.append(
                    ""
                    if content_indent is None or spaces <= content_indent
                    else line[content_indent:]
                )
                row += 1
                continue
            if line.startswith("\t") and lines and lines[-1] == "":
                raise UnsupportedYaml("tab line after a blank line in a block scalar")
            if line.startswith("\t") and (content_indent is None or content_indent > 0):
                if content_indent is not None and line.lstrip("\t ").startswith("#"):
                    break
                raise YamlError("Tab characters cannot be used as indentation")
            if content_indent is None:
                if spaces <= parent_indent and line[spaces : spaces + 1] == "\t":
                    if line.strip(" \t").startswith("#"):
                        break
                    raise YamlError("Tab characters cannot be used as indentation")
                if spaces <= parent_indent:
                    break
                content_indent = spaces
            if spaces < content_indent:
                if spaces > parent_indent and not line.strip(" \t").startswith("#"):
                    raise YamlError("Unexpected token")
                break
            lines.append(line[content_indent:])
            row += 1
        # Trailing lines that are empty belong to the chomping, not the content.
        trailing = 0
        while lines and lines[-1] == "":
            lines.pop()
            trailing += 1
        body = "\n".join(lines) if style == "|" else _fold_block(lines)
        if not lines:
            return ("\n" * trailing if chomping == "+" else ""), row
        if chomping == "-":
            return body, row
        if chomping == "+":
            # At the end of the text, the last split element is what follows
            # the final line break, so it is not a blank line of its own.
            breaks = trailing if row >= len(self.lines) else trailing + 1
            return body + "\n" * breaks, row
        return body + "\n", row

    # Flow collections -------------------------------------------------------

    def flow_node(self, row: int, column: int, parent_indent: int) -> tuple[Any, int, int]:
        return _FlowParser(self, parent_indent).parse(row, column)


def _empty_value(tag: str | None) -> Any:
    """The value Bun gives a node with no content under ``tag``."""
    return "" if tag in ("!", "!!str") else None


def _plain_with_tag(raw: str, tag: str | None) -> Any:
    """Resolve a plain scalar's text under an optional tag, as Bun does.

    Raises:
        UnsupportedYaml: a core-schema tag does not fit the text.
    """
    if tag is None:
        return resolve_plain(raw)
    if tag in ("!!str", "!", "!!binary") or not tag.startswith("!!"):
        # Bun keeps the text under a string, binary, non-specific, local or
        # verbatim tag.
        return raw
    if tag in ("!!int", "!!float", "!!bool", "!!null"):
        value = resolve_plain(raw)
        expected = {
            "!!int": (int, float),
            "!!float": (int, float),
            "!!bool": (bool,),
            "!!null": (type(None),),
        }[tag]
        if (isinstance(value, bool) and tag != "!!bool") or not isinstance(value, expected):
            raise UnsupportedYaml(f"{tag} on {raw!r}")
        return value
    raise UnsupportedYaml(f"tag {tag}")


# Characters a tag name may contain: YAML's URI characters without "!" and the
# flow indicators.
_TAG_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-#;/?:@&=+$_.~*'()%"
)


def _tag_end(line: str, column: int) -> int:
    """Return the column after the tag starting at ``column``.

    A tag ends at the first character that cannot belong to it. Content may
    follow without a space only when it starts a block scalar or is not ASCII.

    Raises:
        YamlError: the tag is malformed.
        UnsupportedYaml: the tag is followed directly by ``#``.
    """
    end = column + 1
    if line[end : end + 1] == "<":
        close = line.find(">", end)
        if close < 0:
            raise YamlError("Unexpected EOF")
        end = close + 1
    else:
        if line[end : end + 1] == "!":
            end += 1
            if end >= len(line) or line[end] in " \t":
                raise YamlError("Unexpected EOF")
        if end < len(line) and line[end] not in " \t":
            if line[end] == "#":
                raise UnsupportedYaml("tag followed by #")
            if not (line[end].isascii() and (line[end].isalnum() or line[end] == "-")):
                raise YamlError("Unexpected character")
            while end < len(line) and line[end] in _TAG_CHARACTERS:
                end += 1
    following = line[end : end + 1]
    if following not in ("", " ", "\t", "|", ">") and following.isascii():
        raise YamlError("Unexpected character")
    return end


def _double_quote_escape(line: str, index: int) -> tuple[str, int]:
    """Decode the escape at ``line[index]`` and return it with its width."""
    escape = line[index + 1 : index + 2]
    if escape in _DOUBLE_QUOTE_ESCAPES:
        return _DOUBLE_QUOTE_ESCAPES[escape], 2
    length = _HEX_ESCAPE_LENGTHS.get(escape)
    if length is not None:
        digits = line[index + 2 : index + 2 + length]
        if len(digits) == length and all(c in "0123456789abcdefABCDEF" for c in digits):
            code = int(digits, 16)
            if not 0xD800 <= code <= 0xDFFF:
                return chr(code), 2 + length
            # Only a \u escape of a high surrogate directly followed by a \u
            # escape of a low surrogate forms a character.
            low = line[index + 8 : index + 12]
            if (
                escape == "u"
                and code <= 0xDBFF
                and line[index + 6 : index + 8] == "\\u"
                and len(low) == 4
                and all(c in "0123456789abcdefABCDEF" for c in low)
                and 0xDC00 <= int(low, 16) <= 0xDFFF
            ):
                pair = 0x10000 + ((code - 0xD800) << 10) + (int(low, 16) - 0xDC00)
                return chr(pair), 12
    raise YamlError("Unexpected character")


def _fold_quoted(segments: list[tuple[str, bool]]) -> str:
    """Join the lines of a multi-line quoted scalar.

    A line break becomes a space, each empty line in between becomes a line
    feed, and white space around breaks is dropped, except that an escaped
    break joins its lines directly.
    """
    if len(segments) == 1:
        return segments[0][0]
    parts: list[str] = []
    breaks = 0
    joined = False
    last = len(segments) - 1
    for position, (text, escaped_break) in enumerate(segments):
        if 0 < position < last and not escaped_break and text.strip(" \t") == "":
            breaks += 1
            continue
        if position > 0 and not joined:
            parts.append("\n" * breaks if breaks else " ")
        breaks = 0
        if position < last and not escaped_break:
            text = text.rstrip(" \t")
        parts.append(text)
        joined = escaped_break
    return "".join(parts)


def _fold_block(lines: list[str]) -> str:
    result: list[str] = []
    breaks = 0
    previous_more_indented = False
    started = False
    for line in lines:
        if line == "":
            breaks += 1
            continue
        more_indented = line[0] in (" ", "\t")
        if not started:
            result.append("\n" * breaks)
            started = True
        elif more_indented or previous_more_indented:
            result.append("\n" * (breaks + 1))
        else:
            result.append("\n" * breaks if breaks else " ")
        result.append(line)
        breaks = 0
        previous_more_indented = more_indented
    return "".join(result)


def _decode_single_line_quoted(raw: str) -> str:
    quote = raw[0]
    inner = raw[1:-1]
    if quote == "'":
        return inner.replace("''", "'")
    parser = _Parser(raw)
    value, _, _ = parser.quoted_scalar(0, 0, -1)
    return value


class _FlowParser:
    def __init__(self, parser: _Parser, parent_indent: int) -> None:
        self.parser = parser
        self.lines = parser.lines
        self.parent_indent = parent_indent

    def skip_space(self, row: int, column: int) -> tuple[int, int]:
        while True:
            line = self.lines[row]
            while column < len(line) and line[column] in " \t":
                column += 1
            if (
                column < len(line)
                and line[column] == "#"
                and (column == 0 or line[column - 1] in " \t")
            ):
                column = len(line)
            if column < len(line):
                return row, column
            row += 1
            if row >= len(self.lines):
                raise YamlError("Unexpected EOF")
            column = 0
            next_line = self.lines[row]
            stripped = next_line.lstrip(" \t")
            if stripped == "" or stripped.startswith("#"):
                continue
            indent = len(next_line) - len(stripped)
            if next_line[:indent].count("\t"):
                raise UnsupportedYaml("tab in flow continuation")
            if indent <= self.parent_indent and stripped[0] not in "]}":
                raise YamlError("Unexpected token")

    def parse(self, row: int, column: int) -> tuple[Any, int, int]:
        opener = self.lines[row][column]
        closer = "]" if opener == "[" else "}"
        items: list[Any] = []
        mapping: dict[str, Any] = {}
        row, column = self.skip_space(row, column + 1)
        expecting_item = True
        while True:
            char = self.lines[row][column]
            if char == closer:
                return (items if opener == "[" else mapping), row, column + 1
            if char == ",":
                if expecting_item:
                    raise YamlError("Unexpected token")
                expecting_item = True
                row, column = self.skip_space(row, column + 1)
                continue
            if not expecting_item:
                raise YamlError("Unexpected token")
            if char == ":" and self.lines[row][column + 1 : column + 2] in (
                "",
                " ",
                "\t",
                ",",
                closer,
            ):
                raise UnsupportedYaml("empty key in a flow collection")
            explicit = char == "?" and self.lines[row][column + 1 : column + 2] in (" ", "\t")
            if explicit:
                if opener != "[":
                    raise UnsupportedYaml("explicit key in a flow mapping")
                row, column = self.skip_space(row, column + 1)
                following = self.lines[row][column]
                if following == "?" and self.lines[row][column + 1 : column + 2] in (" ", "\t"):
                    raise YamlError("Unexpected token")
            if explicit and self.lines[row][column] in (",", closer):
                key = None
            else:
                key, row, column = self.node(row, column)
            row, column = self.skip_space(row, column)
            if self.lines[row][column] == ":":
                row, column = self.skip_space(row, column + 1)
                if self.lines[row][column] in (",", closer):
                    value = None
                else:
                    value, row, column = self.node(row, column)
                    row, column = self.skip_space(row, column)
                if opener == "[":
                    items.append({_js_key(key): value})
                else:
                    mapping[_js_key(key)] = value
            elif opener == "[":
                items.append({_js_key(key): None} if explicit else key)
            else:
                mapping[_js_key(key)] = None
            expecting_item = False

    def node_with_properties(self, row: int, column: int) -> tuple[Any, int, int]:
        line = self.lines[row]
        anchor: str | None = None
        tag: str | None = None
        while line[column : column + 1] in ("&", "!"):
            end = column + 1
            while end < len(line) and line[end] not in " \t,[]{}":
                end += 1
            if line[column] == "&":
                if anchor is not None or end == column + 1:
                    raise UnsupportedYaml("malformed anchor inside a flow collection")
                anchor = line[column + 1 : end]
            else:
                if tag is not None:
                    raise UnsupportedYaml("two tags inside a flow collection")
                tag = line[column:end]
                name = tag.removeprefix("!!") if tag.startswith("!!") else tag[1:]
                if name == "" and (tag == "!!" or line[end : end + 1] not in (" ", "\t")):
                    raise YamlError("Unexpected character")
                if name and not (name[0].isascii() and (name[0].isalnum() or name[0] == "-")):
                    raise YamlError("Unexpected character")
                if any(char not in _TAG_CHARACTERS for char in name):
                    raise YamlError("Unexpected character")
            column = end
            while line[column : column + 1] in (" ", "\t"):
                column += 1
        if column >= len(line):
            raise UnsupportedYaml("properties at the end of a flow line")
        if line[column] in ",]}":
            value: Any = _empty_value(tag)
        elif line[column] in "&!*[{\"'" or (
            tag not in (None, "!!str", "!") and tag.startswith("!!")
        ):
            raise UnsupportedYaml("properties on a complex flow node")
        else:
            value, row, column = self.node(row, column)
            if isinstance(value, str) and tag is not None:
                value = _plain_with_tag(value, tag)
            elif tag is not None and not isinstance(value, str):
                raise UnsupportedYaml("tag on a resolved flow scalar")
        if anchor is not None:
            self.parser.anchors[anchor] = value
        return value, row, column

    def node(self, row: int, column: int) -> tuple[Any, int, int]:
        line = self.lines[row]
        char = line[column]
        if char in "[{":
            return self.parse(row, column)
        if char in "\"'":
            return self.parser.quoted_scalar(row, column, self.parent_indent)
        if char == "*":
            end = column + 1
            while end < len(line) and line[end] not in " \t,[]{}":
                end += 1
            name = line[column + 1 : end]
            if name == "":
                raise YamlError("Unexpected EOF")
            if name not in self.parser.anchors:
                raise YamlError("Unresolved alias")
            return self.parser.anchors[name], row, end
        if char in "&!":
            return self.node_with_properties(row, column)
        if char in "@`%|>#":
            raise YamlError("Unexpected token")
        if char == "?" and line[column + 1 : column + 2] in ("", " ", "\t", ",", "]", "}"):
            raise YamlError("Unexpected token")
        if char in ",]}":
            raise YamlError("Unexpected token")
        if char == "-" and line[column + 1 : column + 2] in ("", " ", "\t", ",", "]", "}"):
            raise YamlError("Unexpected token")
        end = column
        while end < len(line):
            current = line[end]
            if current in _FLOW_INDICATORS:
                break
            if current == ":" and line[end + 1 : end + 2] in (
                "",
                " ",
                "\t",
                ",",
                "[",
                "]",
                "{",
                "}",
            ):
                break
            if current == "#" and end > column and line[end - 1] in " \t":
                break
            end += 1
        text = line[column:end].rstrip(" \t")
        if end == len(line) or line[end] == "#":
            next_row, next_column = self.skip_space(row, len(line))
            if self.lines[next_row][next_column] not in ",]}:":
                raise UnsupportedYaml("multi-line plain scalar in a flow collection")
        return resolve_plain(text), row, column + len(text)


def _js_key(key: Any) -> str:
    if isinstance(key, str):
        return key
    if key is None:
        return "null"
    if key is True:
        return "true"
    if key is False:
        return "false"
    if isinstance(key, (list, dict)):
        raise UnsupportedYaml("collection as a flow mapping key")
    if isinstance(key, float):
        if key != key:
            return "NaN"
        if key in (float("inf"), float("-inf")):
            return "Infinity" if key > 0 else "-Infinity"
        if key.is_integer() and abs(key) < 1e21:
            return str(int(key))
        raise UnsupportedYaml("non-integer number as a key")
    return str(key)


def _starts_block_entry(rest: str) -> bool:
    """Whether ``rest``, a line after its indentation, starts a block entry or key."""
    if _BLOCK_INDICATOR.match(rest):
        return True
    if rest[:1] in ("&", "!"):
        # Properties before a key.
        parts = rest.split(None, 1)
        rest = parts[1] if len(parts) > 1 else ""
    if rest[:1] in ("'", '"'):
        quote = rest[0]
        index = 1
        while index < len(rest):
            escaped = rest[index] == "\\" and quote == '"'
            doubled = rest[index] == quote == "'" and rest[index + 1 : index + 2] == "'"
            if escaped or doubled:
                index += 2
            elif rest[index] == quote:
                break
            else:
                index += 1
        return re.match(r"[ \t]*:(?:[ \t]|\Z)", rest[index + 1 :]) is not None
    depth = 0
    for position, char in enumerate(rest):
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "#" and (position == 0 or rest[position - 1] in " \t"):
            return False
        elif char == ":" and depth <= 0 and rest[position + 1 : position + 2] in ("", " ", "\t"):
            return True
    return False
