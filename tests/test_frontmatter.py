"""Reading rule files: front matter, the YAML retry, glob normalization, bodies."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from c2c_rulesync.frontmatter import normalize_globs, parse_rule_text
from c2c_rulesync.markdown_blocks import strip_block_comments

COMMENT_VECTORS = json.loads(
    (Path(__file__).parent / "vectors" / "marked_comments.json").read_text(encoding="utf-8")
)["vectors"]

# Random bodies on which strip_block_comments is known to differ from marked
# 15.0.12. There are none: keep it that way unless a difference is documented in
# docs/behavior.md.
KNOWN_COMMENT_DIVERGENCES: set[str] = set()


# Front matter detection ------------------------------------------------------


def test_a_file_without_front_matter_is_an_unconditional_body() -> None:
    rule = parse_rule_text("\U0000feff# Rule\n\nText\n")

    assert rule.globs is None
    assert rule.body == "\U0000feff# Rule\n\nText\n"
    assert rule.warnings == ()


def test_front_matter_paths_become_globs_and_are_removed_from_the_body() -> None:
    rule = parse_rule_text('---\npaths:\n  - "src/**/*.ts"\n---\n\n# Rule\n')

    assert rule.globs == ("src/**/*.ts",)
    assert rule.body == "# Rule\n"


def test_a_byte_order_mark_before_front_matter_is_ignored() -> None:
    rule = parse_rule_text("\U0000feff---\npaths: a.md\n---\nBody")

    assert rule.globs == ("a.md",)
    assert rule.body == "Body"


def test_unclosed_front_matter_leaves_the_whole_file_as_an_unconditional_body() -> None:
    text = "---\npaths: src/**\nBody"
    rule = parse_rule_text(text)

    assert rule.globs is None
    assert rule.body == text


def test_front_matter_closes_at_the_first_dashes_even_mid_line() -> None:
    rule = parse_rule_text('---\npaths: "x---y"\n---\nBody')

    # The YAML is `paths: "x`, which fails even after the retry.
    assert rule.globs is None
    assert rule.body == 'y"\n---\nBody'
    assert len(rule.warnings) == 1


def test_four_dashes_do_not_open_front_matter() -> None:
    assert parse_rule_text("----\npaths: a\n---\nBody").globs is None


# The YAML retry ----------------------------------------------------------------


def test_an_unquoted_star_glob_parses_after_the_retry_quotes_it() -> None:
    rule = parse_rule_text("---\npaths: *.ts\n---\nBody")

    assert rule.globs == ("*.ts",)
    assert rule.warnings == ()


def test_an_unquoted_star_list_item_is_not_retried_and_the_rule_is_unconditional() -> None:
    rule = parse_rule_text("---\npaths:\n  - **/*.ts\n---\nBody")

    assert rule.globs is None
    assert "loads this rule for every file" in rule.warnings[0]


def test_an_unquoted_flow_list_is_retried_as_one_literal_glob() -> None:
    assert parse_rule_text("---\npaths: [**/*.ts]\n---\nBody").globs == ("[**/*.ts]",)


def test_the_retry_quotes_every_value_with_an_indicator_including_comments() -> None:
    rule = parse_rule_text("---\ndescription: *x\npaths: zz.txt # note\n---\nBody")

    assert rule.globs == ("zz.txt # note",)


def test_a_line_ending_in_carriage_return_is_not_retried() -> None:
    assert parse_rule_text("---\r\npaths: *.ts\r\n---\r\nBody").globs is None


def test_the_retry_replaces_leading_tabs() -> None:
    assert parse_rule_text("---\npaths:\n\t- a.md\n---\nBody").globs == ("a.md",)


def test_front_matter_the_yaml_parser_does_not_model_is_read_line_by_line() -> None:
    rule = parse_rule_text("---\npaths:\n  - src/**\n%weird: 1\n---\nBody")

    assert rule.globs == ("src",)
    assert "read line by line" in rule.warnings[0]


BACKSLASH = "\\"


def test_surrogate_escapes_form_one_character_only_as_a_pair() -> None:
    pair = f'---\npaths: "{BACKSLASH}ud83d{BACKSLASH}ude00.md"\n---\nBody'
    lone = f'---\npaths: "{BACKSLASH}ud83d.md"\n---\nBody'

    assert parse_rule_text(pair).globs == ("\U0001f600.md",)
    assert parse_rule_text(lone).globs is None


@pytest.mark.parametrize(
    "front_matter",
    ["paths:\n  \t- a.md", "%YAML 1.2\npaths: a.md", "paths: a.md\rb.md"],
)
def test_documents_bun_rejects_even_after_the_retry_leave_the_rule_unconditional(
    front_matter: str,
) -> None:
    rule = parse_rule_text(f"---\n{front_matter}\n---\nBody")

    assert rule.globs is None
    assert "not valid YAML" in rule.warnings[0]


def test_pathological_front_matter_neither_raises_nor_hangs() -> None:
    started = time.monotonic()
    deep = "[" * 7000 + "a" + "]" * 7000

    assert parse_rule_text("---" + "\n" * 200_000).globs is None
    assert parse_rule_text("---\nx:" + " " * 200_000 + "\r\npaths: *.ts\n---\nB").globs == ("*.ts",)
    assert parse_rule_text("---\nv: " + "1" * 5000 + "\npaths: a.md\n---\nB").globs == ("a.md",)
    assert parse_rule_text(f"---\npaths: {deep}\n---\nB").warnings
    assert time.monotonic() - started < 5


def test_deeply_nested_lists_are_flattened() -> None:
    value: object = "a"
    for _ in range(5000):
        value = [value]

    assert normalize_globs(value) == ["a"]


# Which values scope a rule -----------------------------------------------------


@pytest.mark.parametrize(
    "paths",
    ["", "null", "''", "0", "false", "[]", "'**'", "['**', '**/**']", "123", "{a: b}", "[5, true]"],
)
def test_values_that_leave_a_rule_unconditional(paths: str) -> None:
    assert parse_rule_text(f"---\npaths: {paths}\n---\nBody").globs is None


def test_non_string_list_items_are_ignored_and_nested_lists_flattened() -> None:
    assert parse_rule_text("---\npaths: [a, 5, [b, [c]]]\n---\nBody").globs == ("a", "b", "c")


def test_other_keys_are_ignored() -> None:
    rule = parse_rule_text("---\nglobs: src/**\nPaths: src/**\n---\nBody")

    assert rule.globs is None


# Glob normalization -------------------------------------------------------------


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        ("src/**, docs/*.md", ["src/**", "docs/*.md"]),
        (["a, b", "c"], ["a", "b", "c"]),
        ("{a,b}.ts, c", ["a.ts", "b.ts", "c"]),
        ("q}, x", ["q}, x"]),
        ("x{,.bak}", ["x", "x.bak"]),
        ("{x}", ["x"]),
        ("{a, b }", ["a", "b"]),
        ("{a,b}{1,2}", ["a1", "a2", "b1", "b2"]),
        ("{a{b}c}", ["abc"]),
        ("{}", ["{}"]),
        ("a\n{b,c}", ["a\nb", "a\nc"]),
        ("{b,c}\nd", ["{b,c}\nd"]),
        ("  \U00003000spaced\U000000a0 ", ["spaced"]),
    ],
)
def test_normalize_globs(paths: object, expected: list[str]) -> None:
    assert normalize_globs(paths) == expected


def test_brace_expansion_over_budget_keeps_the_item_unexpanded() -> None:
    pattern = "{a,b}" * 11
    warnings: list[str] = []

    assert normalize_globs(pattern, warnings) == [pattern]
    assert warnings


def test_a_trailing_double_star_is_stripped_once() -> None:
    rule = parse_rule_text("---\npaths: [src/**, lib/**/**, '**']\n---\nBody")

    assert rule.globs == ("src", "lib/**", "**")


# Bodies ----------------------------------------------------------------------------


def test_block_comments_are_stripped_from_the_body() -> None:
    rule = parse_rule_text("---\npaths: a\n---\nRule\n<!-- why -->\nMore\n")

    assert rule.body == "Rule\nMore\n"


@pytest.mark.parametrize(
    "vector",
    [v for v in COMMENT_VECTORS if v["set"] == "curated"],
    ids=lambda v: repr(v["body"])[:40],
)
def test_comment_stripping_matches_marked_on_curated_bodies(vector: dict[str, str]) -> None:
    assert strip_block_comments(vector["body"]) == vector["content"]


def test_comment_stripping_matches_marked_on_random_bodies() -> None:
    mismatches = {
        vector["body"]
        for vector in COMMENT_VECTORS
        if vector["set"] == "random" and strip_block_comments(vector["body"]) != vector["content"]
    }

    assert len([v for v in COMMENT_VECTORS if v["set"] == "random"]) == 3000
    assert mismatches <= KNOWN_COMMENT_DIVERGENCES, sorted(mismatches - KNOWN_COMMENT_DIVERGENCES)


@pytest.mark.parametrize(
    "unit",
    [
        "<!-- note -->\n",
        "<div>\n\n<!-- note -->\n",
        "- item\n<!-- note -->\n",
        "Paragraph text.\n<!-- note -->\n\n",
        # marked scans ahead for a setext underline at each of these paragraphs.
        "text\n<!-- note -->x\n",
        "> quote\nlazy\n",
    ],
)
def test_comment_stripping_takes_linear_time(unit: str) -> None:
    body = "<!-- note -->\n" + unit * (256 * 1024 // len(unit))

    start = time.perf_counter()
    strip_block_comments(body)

    # About 0.1 s when linear; a quadratic pass over 256 KiB takes seconds.
    assert time.perf_counter() - start < 2.0


def test_block_quotes_nested_past_the_recursion_limit_leave_the_body_unchanged() -> None:
    body = "<!-- note -->\n" + "> " * sys.getrecursionlimit() + "quote\n"

    assert strip_block_comments(body) == body
