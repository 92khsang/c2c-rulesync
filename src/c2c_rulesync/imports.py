"""Find the ``@path`` imports a ``CLAUDE.local.md`` file names.

Claude Code expands these imports, and c2c-rulesync does not: it only warns that
the files they name are missing. The documented rule is that import parsing
skips Markdown code spans and fenced code blocks
(https://code.claude.com/docs/en/memory#import-additional-files). The rest of
what counts as an import here is c2c-rulesync's approximation, so a warning can
name text that Claude Code would not import.
"""

from __future__ import annotations

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
    """The ``@path`` tokens of ``body``, once each, in order of appearance.

    A token is ``@`` at the start of a line or after white space, and the
    characters up to the next white space, without the sentence punctuation
    that ends it (``.``, ``,``, ``;``, ``:``, ``!`` or ``?``). Tokens inside a fenced code block, or
    inside a code span on one line, are not imports. A fence opens with three or
    more backticks or tildes indented by at most three spaces, closes with at
    least as many of the same character, and without a closing line runs to the
    end of the body.
    """
    references: dict[str, None] = {}
    fence = ""
    for line in _LINE_BREAK.split(body):
        if fence:
            if _closes(line, fence):
                fence = ""
            continue
        opening = _FENCE_OPENING.fullmatch(line)
        # A backtick fence's info string cannot contain a backtick.
        if opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            fence = opening.group(1)
            continue
        for token in _REFERENCE.findall(_without_code_spans(line)):
            token = token.replace(_SPAN, "").rstrip(".,;:!?")
            if token != "@":
                references.setdefault(token)
    return tuple(references)


def _closes(line: str, fence: str) -> bool:
    stripped = line.lstrip(" ")
    marker = stripped.rstrip(" \t")
    return (
        len(line) - len(stripped) <= 3
        and len(marker) >= len(fence)
        and marker == fence[0] * len(marker)
    )


def _without_code_spans(line: str) -> str:
    # A code span runs from a string of backticks to the next string of the same
    # length; a string without such a partner is literal text.
    kept = []
    position = 0
    search = 0
    while opening := _BACKTICKS.search(line, search):
        closing = next(
            (
                candidate
                for candidate in _BACKTICKS.finditer(line, opening.end())
                if len(candidate.group()) == len(opening.group())
            ),
            None,
        )
        if closing is None:
            search = opening.end()
            continue
        kept += [line[position : opening.start()], _SPAN]
        position = search = closing.end()
    kept.append(line[position:])
    return "".join(kept)
