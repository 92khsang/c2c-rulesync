"""Find the ``@path`` imports a ``CLAUDE.local.md`` file names.

Claude Code expands these imports, and c2c-rulesync does not: it only warns that
the text of the files they name is not delivered. The documented rule is that
import parsing skips Markdown code spans and fenced code blocks
(https://code.claude.com/docs/en/memory#import-additional-files). The rest of
what counts as an import here is c2c-rulesync's approximation, so a warning can
name text that Claude Code would not import.
"""

from __future__ import annotations

import bisect
import re

__all__ = ["import_references"]

_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_FENCE_OPENING = re.compile(r" {0,3}(`{3,}|~{3,})(.*)")
_BACKTICKS = re.compile(r"`+")
_REFERENCE = re.compile(r"(?<!\S)@\S+")
# Stands in for a removed code span: not white space, so text attached to the
# span stays attached, and removed from the tokens it ends up in.
_SPAN = "\0"


def import_references(body: str) -> tuple[str, ...]:
    """The ``@path`` tokens of ``body``, once each.

    A token is ``@`` at the start of a line or after white space, and the
    characters up to the next white space, without the sentence punctuation
    that ends it (``.``, ``,``, ``;``, ``:``, ``!`` or ``?``). Tokens inside a
    fenced code block or a code span are not imports. A fence opens with three
    or more backticks or tildes indented by at most three spaces, closes with at
    least as many of the same character, and without a closing line runs to the
    end of the body. A code span can continue onto the next lines of a
    paragraph; because its extent is approximated, a token counts when either a
    line or its paragraph shows it outside a code span.
    """
    references: dict[str, None] = {}
    paragraph: list[str] = []

    def scan(text: str) -> None:
        for token in _REFERENCE.findall(_without_code_spans(text)):
            token = token.replace(_SPAN, "").rstrip(".,;:!?")
            if token != "@":
                references.setdefault(token)

    def end_paragraph() -> None:
        if len(paragraph) > 1:
            scan("\n".join(paragraph))
        paragraph.clear()

    fence = ""
    for line in _LINE_BREAK.split(body):
        if fence:
            if _closes(line, fence):
                fence = ""
            continue
        opening = _FENCE_OPENING.fullmatch(line)
        # A backtick fence's info string cannot contain a backtick.
        if opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            end_paragraph()
            fence = opening.group(1)
            continue
        scan(line)
        if line.strip():
            paragraph.append(line)
        else:
            end_paragraph()
    end_paragraph()
    return tuple(references)


def _closes(line: str, fence: str) -> bool:
    stripped = line.lstrip(" ")
    marker = stripped.rstrip(" \t")
    return (
        len(line) - len(stripped) <= 3
        and len(marker) >= len(fence)
        and marker == fence[0] * len(marker)
    )


def _without_code_spans(text: str) -> str:
    # A code span runs from a string of backticks to the next string of the same
    # length; a string without such a partner is literal text. The strings are
    # indexed by length once, so many unmatched strings do not take quadratic time.
    runs = [(match.start(), match.end()) for match in _BACKTICKS.finditer(text)]
    starts_by_length: dict[int, list[int]] = {}
    for start, end in runs:
        starts_by_length.setdefault(end - start, []).append(start)
    kept = []
    position = 0
    for start, end in runs:
        if start < position:
            continue
        same_length = starts_by_length[end - start]
        partner = bisect.bisect_right(same_length, start)
        if partner == len(same_length):
            continue
        kept += [text[position:start], _SPAN]
        position = same_length[partner] + end - start
    kept.append(text[position:])
    return "".join(kept)
