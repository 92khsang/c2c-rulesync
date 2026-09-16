"""Remove block-level HTML comments from Markdown the way Claude Code does.

Claude Code 2.1.273 tokenizes a rule body containing ``<!--`` with a marked
release that behaves as marked 15.0.12, GFM off. For every top-level HTML token
whose raw text, ignoring leading white space, starts with ``<!--`` and contains
``-->``, it deletes each ``<!-- ... -->`` from that raw text and keeps what is
left only if JavaScript's ``trim`` does not empty it; every other top-level
token's raw text is kept. The result is the concatenation of the raw texts.

The raw texts come from a port of marked 15.0.12's block lexer
(``Lexer.blockTokens``, the block tokenizers it calls and their rules), which is
MIT licensed; see ``THIRD_PARTY_NOTICES.md``. Only what decides the raw text of
a top-level token is ported: inline tokens, the tokens inside list items and
token fields other than ``raw`` are not computed. marked's quirks stay, for
example:

- a link reference definition's raw text disappears unless it follows a
  paragraph;
- a space or tab left at the end of the body becomes a line break of the
  token before it;
- a block quote continued lazily by a list or a nested block quote rebuilds its
  raw text from lengths measured in different strings.

The port spells out JavaScript semantics where Python's differ: ``\\s`` and
``trim`` use JavaScript's white space, ``.`` stops at line terminators, ``i``
folds ASCII letters only, and lengths count UTF-16 code units. It also avoids
marked's super-linear work without changing the tokens: marked scans ahead for a
setext underline at every paragraph, and the port remembers how far a failed
scan reached; raw texts are built by appending instead of copying. A body whose
block quotes nest too deeply for Python's recursion limit is returned unchanged.
"""

from __future__ import annotations

import re
from functools import lru_cache

__all__ = ["strip_block_comments"]

# JavaScript's white space and line terminators, as matched by \s and removed
# by String.prototype.trim.
_JS_WHITESPACE = (
    "\t\n\x0b\x0c\r \xa0\U00001680"
    + "".join(map(chr, range(0x2000, 0x200B)))
    + "\U00002028\U00002029\U0000202f\U0000205f\U00003000\U0000feff"
)
_WS = (
    r"\t\n\x0b\x0c\r \xa0\U00001680\U00002000-\U0000200a\U00002028\U00002029"
    r"\U0000202f\U0000205f\U00003000\U0000feff"
)
_S = f"[{_WS}]"
_NOT_S = f"[^{_WS}]"
# JavaScript's `.` without the s flag.
_DOT = r"[^\n\r\U00002028\U00002029]"

_TAGS = (
    "address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|"
    "details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|"
    "h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|meta|nav|noframes|"
    "ol|optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|"
    "tr|track|ul"
)
_BULLET = r"(?:[*+-]|[0-9]{1,9}[.)])"
_HR_SOURCE = r" {0,3}(?:(?:-[\t ]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})(?:\n+|\Z)"

# marked 15.0.12's block rules without GFM, anchored by ``Pattern.match``, with
# JavaScript's `$` written `\Z` and its `\d` and `\w` limited to ASCII.
_NEWLINE = re.compile(r"(?:[ \t]*(?:\n|\Z))+")
_CODE = re.compile(r"(?:(?: {4}| {0,3}\t)[^\n]+(?:\n(?:[ \t]*(?:\n|\Z))*)?)+")
_FENCES = re.compile(
    r" {0,3}(`{3,}(?=[^`\n]*(?:\n|\Z))|~{3,})[^\n]*(?:\n|\Z)"
    r"(?:|[\s\S]*?(?:\n|\Z))(?: {0,3}\1[~`]* *(?=\n|\Z)|\Z)"
)
_HEADING = re.compile(rf" {{0,3}}#{{1,6}}(?={_S}|\Z){_DOT}*(?:\n+|\Z)")
_HR = re.compile(_HR_SOURCE)
_LIST = re.compile(rf"( {{0,3}}{_BULLET})(?:[ \t][^\n]+?)?(?:\n|\Z)")
_HTML = re.compile(
    r" {0,3}(?:"
    rf"<(script|pre|style|textarea)[{_WS}>][\s\S]*?(?:</\1>[^\n]*\n+|\Z)"
    r"|<!--(?:-?>|[\s\S]*?(?:-->|\Z))[^\n]*(?:\n+|\Z)"
    r"|<\?[\s\S]*?(?:\?>\n*|\Z)"
    r"|<![A-Z][\s\S]*?(?:>\n*|\Z)"
    r"|<!\[CDATA\[[\s\S]*?(?:\]\]>\n*|\Z)"
    rf"|</?(?:{_TAGS})(?: +|\n|/?>)[\s\S]*?(?:(?:\n[ \t]*)+\n|\Z)"
    r"|<(?!script|pre|style|textarea)[a-z][\w-]*"
    rf"(?: +[a-zA-Z:_][\w.:-]*(?: *= *\"[^\"\n]*\"| *= *'[^'\n]*'| *= *[^{_WS}\"'=<>`]+)?)*?"
    r" */?>(?=[ \t]*(?:\n|\Z))[\s\S]*?(?:(?:\n[ \t]*)+\n|\Z)"
    rf"|</(?!script|pre|style|textarea)[a-z][\w-]*{_S}*>(?=[ \t]*(?:\n|\Z))"
    r"[\s\S]*?(?:(?:\n[ \t]*)+\n|\Z)"
    r")",
    re.IGNORECASE | re.ASCII,
)
_DEF = re.compile(
    rf" {{0,3}}\[(?!{_S}*\])(?:\\{_DOT}|[^\[\]\\])+\]: *(?:\n[ \t]*)?"
    rf"(?:[^<{_WS}]{_NOT_S}*|<{_DOT}*?>)"
    r"(?:(?: +(?:\n[ \t]*)?| *\n[ \t]*)"
    r"(?:\"(?:\\\"?|[^\"\\])*\"|'[^'\n]*(?:\n[^'\n]+)*\n?'|\([^()]*\)))? *(?:\n+|\Z)"
)
_PARAGRAPH_SOURCE = (
    r"[^\n]+(?:\n(?!"
    + _HR_SOURCE
    + rf"| {{0,3}}#{{1,6}}(?:{_S}|\Z)"
    + r"| {0,3}>"
    + r"| {0,3}(?:`{3,}(?=[^`\n]*\n)|~{3,})[^\n]*\n"
    + r"| {0,3}(?:[*+-]|1[.)]) "
    + rf"|</?(?:{_TAGS})(?: +|\n|/?>)"
    + r"|<(?:script|pre|style|textarea|!--)"
    + r"| +\n"
    + r")[^\n]+)*"
)
_PARAGRAPH = re.compile(_PARAGRAPH_SOURCE)
_BLOCKQUOTE = re.compile(rf"(?: {{0,3}}> ?(?:{_PARAGRAPH_SOURCE}|[^\n]*)(?:\n|\Z))+")

# The setext heading rule, taken apart so that it can be scanned line by line.
_LHEADING_BLOCK = (
    rf"{_BULLET} |(?: {{4}}| {{0,3}}\t)| {{0,3}}(?:`{{3,}}|~{{3,}})| {{0,3}}>"
    r"| {0,3}#{1,6}| {0,3}<[^\n>]+>\n"
)
_LHEADING_START = re.compile(_LHEADING_BLOCK)
_LHEADING_STOP = re.compile(rf"{_S}*?\n|{_LHEADING_BLOCK}")
_LHEADING_UNDERLINE = re.compile(r"\n {0,3}(?:=+|-+) *(?:\n+|\Z)")
# What JavaScript's `.` does not match.
_LINE_TERMINATOR = re.compile(r"[\n\r\U00002028\U00002029]")

_BLOCKQUOTE_START = re.compile(r" {0,3}>")
_BLOCKQUOTE_SETEXT = re.compile(r"\n {0,3}((?:=+|-+) *)(?=\n|\Z)")
_BLOCKQUOTE_MARKER = re.compile(r"(?:\A|(?<=[\n\r\U00002028\U00002029])) {0,3}>[ \t]?")

_LEADING_TABS = re.compile(r"\t+")
_BLANK_LINE = re.compile(r"[ \t]*\Z")
_COMMENT = re.compile(r"<!--[\s\S]*?-->")
_ASTRAL = re.compile("[\U00010000-\U0010ffff]")
# Every rule tried before a setext heading starts with one of these characters.
_BLOCK_START = frozenset(" \t\n`~#-_*+>0123456789<[")


def strip_block_comments(body: str) -> str:
    """Return ``body`` without the HTML comments Claude Code strips."""
    if "<!--" not in body:
        return body
    text = re.sub(r"\r\n|\r", "\n", body)
    astral = _ASTRAL.search(text) is not None
    if astral:
        text = _ASTRAL.sub(_surrogate_pair, text)
    tokens: list[_Token] = []
    try:
        _block_tokens(text, tokens, False)
    except RecursionError:
        return body
    output: list[str] = []
    for token in tokens:
        raw = token.value()
        if token.kind == "html":
            trimmed = raw.lstrip(_JS_WHITESPACE)
            if trimmed.startswith("<!--") and "-->" in trimmed:
                rest = _COMMENT.sub("", raw)
                if rest.strip(_JS_WHITESPACE):
                    output.append(rest)
                continue
        output.append(raw)
    content = "".join(output)
    if astral:
        content = content.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    return content


def _surrogate_pair(match: re.Match[str]) -> str:
    code = ord(match.group()) - 0x10000
    return chr(0xD800 + (code >> 10)) + chr(0xDC00 + (code & 0x3FF))


class _Rope:
    """A string built by appending, without copying what is already there."""

    __slots__ = ("_parts", "length")

    def __init__(self, text: str = "") -> None:
        self._parts = [text]
        self.length = len(text)

    def append(self, text: str) -> None:
        self._parts.append(text)
        self.length += len(text)

    def value(self) -> str:
        if len(self._parts) != 1:
            self._parts = ["".join(self._parts)]
        return self._parts[0]


class _Token(_Rope):
    """A block token: its type and its raw text."""

    __slots__ = ("kind",)

    def __init__(self, kind: str, raw: str) -> None:
        super().__init__(raw)
        self.kind = kind


def _block_tokens(src: str, tokens: list[_Token], last_paragraph_clipped: bool) -> None:
    """Append the tokens of ``src`` to ``tokens`` as ``Lexer.blockTokens`` does."""
    pos = 0
    end = len(src)
    # A setext heading scan that failed from ``failed_from`` also fails from
    # every later position up to ``failed_to``.
    failed_from = failed_to = -1
    while pos < end:
        if src[pos] in _BLOCK_START and (block_end := _block(src, pos, tokens)) is not None:
            pos = block_end
            continue
        if not failed_from < pos <= failed_to:
            heading_end, stopped_at = _lheading(src, pos)
            if heading_end is not None:
                tokens.append(_Token("heading", src[pos:heading_end]))
                pos = heading_end
                continue
            failed_from, failed_to = pos, stopped_at
        # Every other line is a paragraph: blank lines were taken as space.
        match = _PARAGRAPH.match(src, pos)
        assert match is not None
        if last_paragraph_clipped and tokens and tokens[-1].kind == "paragraph":
            tokens[-1].append("\n" + match.group())
        else:
            tokens.append(_Token("paragraph", match.group()))
        last_paragraph_clipped = False
        pos = match.end()


def _block(src: str, pos: int, tokens: list[_Token]) -> int | None:
    """Try the rules marked tries before a setext heading; return where the match ends."""
    if (match := _NEWLINE.match(src, pos)) is not None and match.end() > pos:
        if match.end() - pos == 1 and tokens:
            tokens[-1].append("\n")
        else:
            tokens.append(_Token("space", match.group()))
        return match.end()
    if (match := _CODE.match(src, pos)) is not None:
        if tokens and tokens[-1].kind == "paragraph":
            tokens[-1].append("\n" + match.group())
        else:
            tokens.append(_Token("code", match.group()))
        return match.end()
    if (match := _FENCES.match(src, pos)) is not None:
        tokens.append(_Token("code", match.group()))
        return match.end()
    if (match := _HEADING.match(src, pos)) is not None:
        tokens.append(_Token("heading", match.group()))
        return match.end()
    if (match := _HR.match(src, pos)) is not None:
        raw = match.group().rstrip("\n")
        tokens.append(_Token("hr", raw))
        return pos + len(raw)
    if (token := _blockquote(src, pos) or _list(src, pos)) is not None:
        tokens.append(token)
        return pos + token.length
    if (match := _HTML.match(src, pos)) is not None:
        tokens.append(_Token("html", match.group()))
        return match.end()
    if (match := _DEF.match(src, pos)) is not None:
        # A definition that does not continue a paragraph becomes a link
        # target, and its raw text is dropped.
        if tokens and tokens[-1].kind == "paragraph":
            tokens[-1].append("\n" + match.group())
        return match.end()
    return None


def _lheading(src: str, pos: int) -> tuple[int | None, int]:
    """Match the setext heading rule at ``pos``.

    Returns the end of the heading, or ``None`` and the position where the scan
    stopped, up to which the rule fails from every later start as well.
    """
    if _LHEADING_START.match(src, pos):
        return None, pos
    cursor = pos
    while True:
        if (terminator := _LINE_TERMINATOR.search(src, cursor)) is None:
            return None, len(src)
        line_end = terminator.start()
        if src[line_end] != "\n":
            return None, line_end
        if line_end > pos and (underline := _LHEADING_UNDERLINE.match(src, line_end)):
            return underline.end(), line_end
        if _LHEADING_STOP.match(src, line_end + 1):
            return None, line_end
        cursor = line_end + 1


def _blockquote(src: str, pos: int) -> _Token | None:
    """Match a block quote as ``Tokenizer.blockquote`` does."""
    match = _BLOCKQUOTE.match(src, pos)
    if match is None:
        return None
    # A block quote continued by a nested block quote ends with that quote
    # re-read from the nested raw text; the prefixes kept before it pile up.
    # marked also tracks the quote's inner text, which never reaches a raw text.
    raw_prefix: list[str] = []
    while True:
        lines = match.group().rstrip("\n").split("\n")
        first = 0
        raw = _Rope()
        tokens: list[_Token] = []
        nested: str | None = None
        while first < len(lines):
            in_blockquote = False
            current = first
            while current < len(lines):
                if _BLOCKQUOTE_START.match(lines[current]):
                    in_blockquote = True
                elif in_blockquote:
                    break
                current += 1
            current_raw = "\n".join(lines[first:current])
            first = current
            current_text = _BLOCKQUOTE_MARKER.sub(
                "", _BLOCKQUOTE_SETEXT.sub(lambda m: "\n    " + m.group(1), current_raw)
            )
            raw.append(f"\n{current_raw}" if raw.length else current_raw)
            _block_tokens(current_text, tokens, True)
            if first == len(lines) or not tokens:
                continue
            last = tokens[-1]
            if last.kind == "code":
                break
            if last.kind == "blockquote":
                raw_prefix.append(raw.value()[: max(0, raw.length - last.length)])
                nested = last.value() + "\n" + "\n".join(lines[first:])
                break
            if last.kind == "list":
                continued = last.value() + "\n" + "\n".join(lines[first:])
                items = _list(continued, 0)
                assert items is not None
                tokens[-1] = items
                raw = _Rope(raw.value()[: max(0, raw.length - last.length)] + items.value())
                lines = continued[items.length :].split("\n")
                first = 0
        if nested is None:
            return _Token("blockquote", "".join(raw_prefix) + raw.value())
        match = _BLOCKQUOTE.match(nested)
        assert match is not None


def _list(src: str, pos: int) -> _Token | None:
    """Match a list's raw text as ``Tokenizer.list`` does."""
    match = _LIST.match(src, pos)
    if match is None:
        return None
    bullet = match.group(1).strip(_JS_WHITESPACE)
    item_rule = _list_item_rule(bullet[-1], len(bullet) > 1)
    end = len(src)
    raw = _Rope()
    while pos < end:
        item = item_rule.match(src, pos)
        if item is None or _HR.match(src, pos):
            break
        raw.append(item.group())
        pos = item.end()
        marker, rest = item.group(1), item.group(2)
        line = rest.split("\n", 1)[0]
        if tabs := _LEADING_TABS.match(line):
            line = "   " * tabs.end() + line[tabs.end() :]
        blank_line = not line.strip(_JS_WHITESPACE)
        if blank_line:
            indent = len(marker) + 1
        else:
            indent = _first_non_space(rest)
            indent = (1 if indent > 4 else indent) + len(marker)
        newline = src.find("\n", pos)
        next_line = src[pos:] if newline == -1 else src[pos:newline]
        if blank_line and _BLANK_LINE.match(next_line):
            raw.append(next_line + "\n")
            pos += len(next_line) + 1
            continue
        breaks = _list_breaks(indent)
        while pos < end:
            newline = src.find("\n", pos)
            next_line = src[pos:] if newline == -1 else src[pos:newline]
            without_tabs = next_line.replace("\t", "    ")
            if breaks.next_line.match(next_line):
                break
            if not (
                _first_non_space(without_tabs) >= indent or not next_line.strip(_JS_WHITESPACE)
            ) and (
                blank_line
                or _first_non_space(line.replace("\t", "    ")) >= 4
                or breaks.previous_line.match(line)
            ):
                break
            if not next_line.strip(_JS_WHITESPACE):
                blank_line = True
            raw.append(next_line + "\n")
            pos += len(next_line) + 1
            line = without_tabs[indent:]
    if not raw.length:
        return None
    return _Token("list", raw.value().rstrip(_JS_WHITESPACE))


def _first_non_space(text: str) -> int:
    """Return the index of the first character that is not a space, or -1."""
    stripped = text.lstrip(" ")
    return len(text) - len(stripped) if stripped else -1


@lru_cache(maxsize=16)
def _list_item_rule(bullet: str, ordered: bool) -> re.Pattern[str]:
    marker = rf"[0-9]{{1,9}}\{bullet}" if ordered else re.escape(bullet)
    return re.compile(rf"( {{0,3}}{marker})((?:[\t ][^\n]*)?(?:\n|\Z))")


class _ListBreaks:
    """The rules, for one content indent, that end a list item."""

    def __init__(self, indent: int) -> None:
        spaces = f" {{0,{min(3, indent - 1)}}}"
        hr = r"(?:(?:- *){3,}|(?:_ *){3,}|(?:\* *){3,})\Z"
        # The next line starts a fence, a heading, HTML, a bullet or a break.
        self.next_line = re.compile(
            rf"{spaces}(?:```|~~~|#|<(?:[a-z]{_DOT}*>|!--)|{_BULLET}(?:[ \t][^\n]*)?\Z|{hr})",
            re.IGNORECASE | re.ASCII,
        )
        # The previous line was a fence, a heading or a break, so an unindented
        # line does not continue the item lazily.
        self.previous_line = re.compile(rf"{spaces}(?:```|~~~|#|{hr})")


@lru_cache(maxsize=64)
def _list_breaks(indent: int) -> _ListBreaks:
    return _ListBreaks(indent)
