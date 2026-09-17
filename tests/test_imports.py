"""The ``@path`` imports a CLAUDE.local.md file names, for the warning about them."""

from __future__ import annotations

import time

import pytest

from c2c_rulesync.imports import import_references


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("", ()),
        ("@AGENTS.md\n", ("@AGENTS.md",)),
        ("See @README for more and @docs/git.md.\n", ("@README", "@docs/git.md")),
        ("@a.md, @b.md; @c.md: @d.md! @e.md? @..\n", ("@a.md", "@b.md", "@c.md", "@d.md", "@e.md")),
        ("- @~/.claude/my-project-instructions.md\n", ("@~/.claude/my-project-instructions.md",)),
        ("\t@tabbed.md\n", ("@tabbed.md",)),
        ("mail@example.com (@paren.md)\n", ()),
        ("@a.md @b.md @a.md\n", ("@a.md", "@b.md")),
        ("@a.md\r\n@b.md\r@c.md", ("@a.md", "@b.md", "@c.md")),
        ("@\n", ()),
    ],
    ids=[
        "empty",
        "line-start",
        "mid-line",
        "trailing-punctuation",
        "home",
        "after-tab",
        "attached",
        "duplicates",
        "line-breaks",
        "bare-at",
    ],
)
def test_import_tokens_start_after_white_space(body: str, expected: tuple[str, ...]) -> None:
    assert import_references(body) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("```\n@fenced.md\n```\n@after.md\n", ("@after.md",)),
        ("~~~text\n@fenced.md\n~~~\n@after.md\n", ("@after.md",)),
        ("   ```\n@fenced.md\n   ```\n@after.md\n", ("@after.md",)),
        ("    ```\n@not-fenced.md\n", ("@not-fenced.md",)),
        ("````\n@fenced.md\n```\n@still-fenced.md\n`````\n@after.md\n", ("@after.md",)),
        ("```\n@fenced.md\n~~~\n@still-fenced.md\n", ()),
        ("``` a`b\n@not-fenced.md\n", ("@not-fenced.md",)),
        ("~~~ a`b\n@fenced.md\n", ()),
        ("```\n@fenced.md\n```  \t\n@after.md\n", ("@after.md",)),
        ("```\n@fenced.md\n``` x\n@still-fenced.md\n", ()),
        ("    @indented.md\n", ("@indented.md",)),
    ],
    ids=[
        "backticks",
        "tildes",
        "indented-fence",
        "four-spaces-is-not-a-fence",
        "closing-needs-as-many",
        "closing-needs-the-same-character",
        "backtick-info-with-backtick",
        "tilde-info-with-backtick",
        "closing-with-trailing-space",
        "closing-with-text",
        "indented-code-still-warns",
    ],
)
def test_fenced_code_blocks_hold_no_imports(body: str, expected: tuple[str, ...]) -> None:
    assert import_references(body) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Literal `@README` here, but @real.md\n", ("@real.md",)),
        ("``a ` @inside.md`` @outside.md\n", ("@outside.md",)),
        ("An unmatched ` @literal.md\n", ("@literal.md",)),
        ("``one` @between.md `two``\n", ()),
        ("`@a.md`@b.md\n", ()),
        ("`a` @b.md `c`\n", ("@b.md",)),
        ("@x`span` @y.md\n", ("@x", "@y.md")),
        ("@`span`\n", ()),
        ("Run `uv run\npytest` and follow @docs/testing.md for `ruff`.\n", ("@docs/testing.md",)),
        ("A `span\n@inside.md` here.\n", ("@inside.md`",)),
        ("A `span\n\n@after-blank.md` here.\n", ("@after-blank.md`",)),
    ],
    ids=[
        "span",
        "double-backticks",
        "unmatched",
        "lengths-differ",
        "attached-to-span",
        "between",
        "span-inside-token",
        "only-a-span-after-at",
        "span-across-lines",
        "line-scan-still-counts",
        "blank-line-ends-a-paragraph",
    ],
)
def test_code_spans_hold_no_imports(body: str, expected: tuple[str, ...]) -> None:
    assert import_references(body) == expected


def test_many_unmatched_backtick_strings_take_linear_time() -> None:
    body = " ".join("`" * length for length in range(2, 500)) + " `x`" * 50_000 + " @a.md\n"

    started = time.monotonic()
    assert import_references(body) == ("@a.md",)
    assert time.monotonic() - started < 1.0
