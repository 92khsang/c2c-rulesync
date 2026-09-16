"""The node-ignore 7.0.5 port against answers computed by node-ignore itself.

tests/vectors/node_ignore_7_0_5.json is produced by scripts/gen_ignore_vectors.mjs
running the real library; see that script for how each vector set is built.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from c2c_rulesync.ignore import Matcher, is_valid_pattern, matches

VECTORS_PATH = Path(__file__).parent / "vectors" / "node_ignore_7_0_5.json"
VECTORS: dict[str, Any] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def _mismatch_report(mismatches: list[str]) -> str:
    shown = "\n".join(mismatches[:25])
    more = f"\n... and {len(mismatches) - 25} more" if len(mismatches) > 25 else ""
    return f"{len(mismatches)} mismatches:\n{shown}{more}"


def test_vectors_come_from_the_pinned_node_ignore_release() -> None:
    source = VECTORS["source"]

    assert source["version"] == "7.0.5"
    assert source["commit"] == "84d052ddfe7c326b01b306154e06709d6e7e2ed8"


def test_node_ignore_fixture_cases() -> None:
    mismatches = []
    for vector in VECTORS["fixtures"]:
        if "text" in vector:
            # node-ignore splits a string of patterns into lines.
            patterns = [line for line in re.split(r"\r?\n", vector["text"]) if line]
        else:
            patterns = vector["patterns"]
        actual = Matcher(patterns).ignores(vector["path"])
        if actual != vector["ignored"]:
            mismatches.append(f"{vector['description']}: {vector['path']!r} -> {actual}")

    assert len(VECTORS["fixtures"]) == 302
    assert not mismatches, _mismatch_report(mismatches)


@pytest.mark.parametrize("vector_set", ["curated", "generated"])
def test_vectors_evaluated_by_node_ignore(vector_set: str) -> None:
    mismatches = []
    for vector in VECTORS[vector_set]:
        patterns, path = vector["patterns"], vector["path"]
        validity = [is_valid_pattern(pattern) for pattern in patterns]
        actual = matches(patterns, path)
        if validity != vector["valid"] or actual != vector["ignored"]:
            mismatches.append(
                f"{patterns!r} {path!r}: valid {validity} vs {vector['valid']}, "
                f"ignored {actual} vs {vector['ignored']}"
            )

    assert not mismatches, _mismatch_report(mismatches)


@pytest.mark.parametrize("path", ["", ".", "..", "/a", "./a", "../a"])
def test_paths_node_ignore_rejects_do_not_match(path: str) -> None:
    assert matches(["*"], path) is False


def test_invalid_patterns_are_dropped_individually() -> None:
    assert is_valid_pattern("[~-!]") is False
    assert matches(["[~-!]", "*.md"], "a.md") is True
    assert matches(["[~-!]"], "a") is False


def test_blank_and_comment_patterns_are_not_rules() -> None:
    assert matches(["", "   ", "# comment"], "a") is False
    assert is_valid_pattern("# comment") is True


def test_case_insensitivity_follows_javascript_not_python() -> None:
    # Python's re.IGNORECASE equates these pairs; JavaScript does not.
    assert matches(["s"], "\U0000017f") is False
    assert matches(["k"], "\U0000212a") is False
    assert matches(["i"], "\U00000131") is False
    assert matches(["DOCS/*.MD"], "docs/a.md") is True
    assert matches(["[a-c]"], "B") is True


def test_every_javascript_case_class_matches_within_itself_only() -> None:
    from c2c_rulesync._js_case import CASE_CLASSES

    mismatches = []
    for members in CASE_CLASSES:
        for pattern_char in members:
            for path_char in members:
                if not matches([pattern_char], path_char):
                    mismatches.append(f"{pattern_char!r} should match {path_char!r}")
                if not matches([f"[{pattern_char}]"], path_char):
                    mismatches.append(f"[{pattern_char!r}] should match {path_char!r}")
        outsider = "0"
        if matches([members[0]], outsider):
            mismatches.append(f"{members[0]!r} should not match {outsider!r}")

    assert not mismatches, _mismatch_report(mismatches)
