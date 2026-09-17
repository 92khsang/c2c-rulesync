"""claudeMdExcludes: reading the setting, and which paths its patterns match."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from c2c_rulesync.excludes import MAX_SETTINGS_BYTES, Excludes, load_excludes

# A directory that does not exist, so that no link changes what a pattern names.
R = "/c2c-rulesync-absent/proj/.claude/rules"


def matches(pattern: str, path: str) -> bool:
    excludes = Excludes([pattern], "settings.json")
    assert excludes.warnings == []
    return excludes.matches(path)


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        (f"{R}/a.md", f"{R}/a.md", True),
        (f"{R}/a.md", f"{R}/b.md", False),
        (f"{R}/CASE.md", f"{R}/case.md", False),
        (f"{R}/star/*", f"{R}/star/a.md", True),
        (f"{R}/star/*", f"{R}/star/sub/a.md", False),
        ("/c2c-rulesync-absent/proj/*/rules/a.md", f"{R}/a.md", True),
        ("/c2c-rulesync-absent/**/a.md", f"{R}/a.md", True),
        (f"{R}/**/a.md", f"{R}/a.md", True),
        ("**/a.md", f"{R}/a.md", True),
        ("**/proj/**", f"{R}/a.md", True),
        (f"{R}/tail/**", f"{R}/tail/deeper/a.md", True),
        (f"{R}/dbl**.md", f"{R}/dbl-xy.md", True),
        (f"{R}/dbl**.md", f"{R}/dbl-dir/deep.md", False),
        (f"{R}/dir", f"{R}/dir/a.md", False),
        (f"{R}/dir/", f"{R}/dir/a.md", False),
        (f"{R}/q?.md", f"{R}/q1.md", True),
        (f"{R}/q?.md", f"{R}/q12.md", False),
        (f"{R}/brace-{{a,b}}.md", f"{R}/brace-b.md", True),
        (f"{R}/brace-{{a,b}}.md", f"{R}/brace-c.md", False),
        (f"{R}/nb-{{x,{{y,z}}}}.md", f"{R}/nb-z.md", True),
        (f"{R}/nb-{{x,{{y,z}}}}.md", f"{R}/nb-w.md", False),
        (f"{R}/rg-{{1..3}}.md", f"{R}/rg-2.md", True),
        (f"{R}/rg-{{1..3}}.md", f"{R}/rg-5.md", False),
        (f"{R}/rg-{{3..-1}}.md", f"{R}/rg--1.md", True),
        (f"{R}/cls-[ab].md", f"{R}/cls-a.md", True),
        (f"{R}/cls-[ab].md", f"{R}/cls-c.md", False),
        (f"{R}/rng-[a-c].md", f"{R}/rng-b.md", True),
        (f"{R}/rng-[a-c].md", f"{R}/rng-d.md", False),
        (f"{R}/caret-[^a].md", f"{R}/caret-b.md", True),
        (f"{R}/caret-[^a].md", f"{R}/caret-a.md", False),
        (f"{R}/neg-[!a].md", f"{R}/neg-a.md", True),
        (f"{R}/neg-[!a].md", f"{R}/neg-!.md", True),
        (f"{R}/neg-[!a].md", f"{R}/neg-b.md", False),
        (f"{R}/*.md", f"{R}/.hidden.md", True),
        (f"{R}/a.md", "relative/a.md", False),
    ],
)
def test_patterns_match_as_claude_code_was_recorded(
    pattern: str, path: str, expected: bool
) -> None:
    assert matches(pattern, path) is expected


@pytest.mark.parametrize(
    "pattern", ["a.md", ".claude/rules/a.md", "~/.claude/rules/**", "*/proj/a.md", "{a,b}/x.md"]
)
def test_a_pattern_that_is_not_absolute_matches_nothing_with_a_warning(pattern: str) -> None:
    excludes = Excludes([pattern], "settings.json")

    assert not excludes.active
    assert excludes.warnings == [
        f"settings.json: claudeMdExcludes pattern {json.dumps(pattern)} matches no absolute path"
        + ("; ~ is not expanded" if pattern.startswith("~") else "")
        + "; start it with / or **/"
    ]
    assert not excludes.matches(f"/home/me/{pattern.lstrip('~/')}")


def test_a_brace_alternative_that_is_not_absolute_leaves_the_others() -> None:
    excludes = Excludes([f"{{{R}/a.md,b.md}}"], "settings.json")

    assert excludes.matches(f"{R}/a.md")
    assert len(excludes.warnings) == 1


@pytest.mark.parametrize(
    ("pattern", "problem"),
    [
        ("", "an empty pattern"),
        (f"!{R}/a.md", "negation"),
        (f"{R}/\\*.md", "a backslash"),
        (f"{R}/@(a|b).md", "parentheses or |"),
        (f"{R}/a (1).md", "parentheses or |"),
        (f"{R}/[[:alpha:]].md", "a bracket expression"),
        (f"{R}/[a.md", "a bracket expression"),
        (f"{R}/a].md", "a bracket expression"),
        (f"{R}/[].md", "a bracket expression"),
        (f"{R}/[^].md", "a bracket expression"),
        (f"{R}/[c-a].md", "a bracket expression"),
        (f"{R}/[a,b].md", "a bracket expression"),
        (f"{R}/{{a}}.md", "braces without a comma or an integer range"),
        (f"{R}/{{01..03}}.md", "braces without a comma or an integer range"),
        (f"{R}/{{a..c}}.md", "braces without a comma or an integer range"),
        (f"{R}/{{1..9..2}}.md", "braces without a comma or an integer range"),
        (f"{R}/{{a,}}.md", "an empty brace alternative"),
        (f"{R}/{{a.md", "an unmatched brace"),
        (f"{R}/a}}.md", "an unmatched brace"),
        (f"{R}/./a.md", "a . or .. segment"),
        (f"{R}/../a.md", "a . or .. segment"),
        (f"{R}/" + "a" * 4096, "more than 4096 characters"),
        (f"{R}/{{1..1001}}.md", "more than 1000 brace alternatives"),
        (f"{R}/{{1..100}}{{1..11}}.md", "more than 1000 brace alternatives"),
        (f"{R}/" + "{a," * 17 + "b" + "}" * 17, "braces nested more than 16 deep"),
    ],
)
def test_unsupported_syntax_is_not_applied_and_warned_about(pattern: str, problem: str) -> None:
    excludes = Excludes([pattern, f"{R}/kept.md"], "settings.json")

    shown = pattern if len(pattern) <= 100 else pattern[:100] + "..."
    assert excludes.warnings == [
        f"settings.json: claudeMdExcludes pattern {json.dumps(shown)} uses {problem}, "
        "which c2c-rulesync does not support; the pattern is not applied"
    ]
    assert excludes.matches(f"{R}/kept.md")
    assert not excludes.matches(f"{R}/a.md")


def test_patterns_beyond_1000_alternatives_are_not_applied() -> None:
    patterns = [f"{R}/{{1..500}}.md", f"{R}/{{501..1000}}.md", f"{R}/late.md", f"{R}/later.md"]
    excludes = Excludes(patterns, "settings.json")

    assert excludes.matches(f"{R}/1000.md")
    assert not excludes.matches(f"{R}/late.md")
    assert not excludes.matches(f"{R}/later.md")
    assert excludes.warnings == [
        "settings.json: claudeMdExcludes expands to more than 1000 patterns; "
        f'"{R}/late.md" and the patterns after it are not applied'
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="needs symbolic links")
def test_leading_directories_of_a_pattern_also_match_through_links(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / ".claude" / "rules").mkdir(parents=True)
    (real / ".claude" / "rules" / "a.md").write_text("A.\n")
    (real / ".claude" / "rules" / "l.md").symlink_to("a.md")
    (tmp_path / "alias").symlink_to("real")
    rule = str(real / ".claude" / "rules" / "a.md")

    assert Excludes([f"{tmp_path}/alias/.claude/rules/a.md"], "s").matches(rule)
    assert Excludes([f"{tmp_path}/alias/**/a.md"], "s").matches(rule)
    assert Excludes([f"{tmp_path}/alias/.claude/rules/a.md"], "s").matches(
        f"{tmp_path}/alias/.claude/rules/a.md"
    )
    # A pattern naming a linked file names the link, not the file it leads to.
    assert not Excludes([f"{real}/.claude/rules/l.md"], "s").matches(rule)


@pytest.mark.parametrize(
    ("pattern", "path"),
    [
        ("/" + "*a" * 1000 + "b", "/" + "a" * 3000),
        ("/" + "*a*" * 500 + "b", "/" + "ab" * 1500),
        ("/" + "**/a/" * 500 + "b", "/" + "/".join(["a"] * 3000)),
        ("/" + "/".join(["**"] * 1000) + "/b", "/" + "/".join(["a"] * 3000)),
    ],
)
def test_matching_takes_bounded_time(pattern: str, path: str) -> None:
    started = time.monotonic()
    excludes = Excludes([pattern], "s")
    excludes.matches(path)

    assert time.monotonic() - started < 1


# Reading the setting ------------------------------------------------------------


def write_settings(tmp_path: Path, content: str | bytes) -> str:
    path = tmp_path / "settings.json"
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return str(path)


def test_the_patterns_of_a_settings_file_are_applied(tmp_path: Path) -> None:
    settings = {"model": "x", "claudeMdExcludes": [f"{R}/a.md"]}
    excludes = load_excludes(write_settings(tmp_path, json.dumps(settings)))

    assert excludes is not None
    assert excludes.warnings == []
    assert excludes.matches(f"{R}/a.md")


def test_no_file_or_no_setting_gives_nothing(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("")

    assert load_excludes(str(tmp_path / "absent.json")) is None
    assert load_excludes(str(tmp_path / "file" / "settings.json")) is None
    assert load_excludes(str(tmp_path / "bad\0name.json")) is None
    assert load_excludes(write_settings(tmp_path, '{"model": "x"}')) is None


def test_an_empty_list_applies_nothing_silently(tmp_path: Path) -> None:
    excludes = load_excludes(write_settings(tmp_path, '{"claudeMdExcludes": []}'))

    assert excludes is not None
    assert not excludes.active
    assert excludes.warnings == []


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ('{"claudeMdExcludes": ["/a.md"],}', "not valid JSON"),
        ('// comment\n{"claudeMdExcludes": ["/a.md"]}', "not valid JSON"),
        ('{"claudeMdExcludes": ["/a.md"], "n": NaN}', "not valid JSON"),
        (b'\xef\xbb\xbf{"claudeMdExcludes": ["/a.md"]}', "not valid JSON"),
        (b'{"claudeMdExcludes": ["/a\xff.md"]}', "not valid JSON"),
        ("[" * 100_000, "not valid JSON"),
        ('["/a.md"]', "not a JSON object"),
        ('{"claudeMdExcludes": "/a.md"}', "claudeMdExcludes is not a list"),
        ('{"claudeMdExcludes": null}', "claudeMdExcludes is not a list"),
        (
            '{"claudeMdExcludes": ["/a.md", 5]}',
            "claudeMdExcludes has an entry that is not a string",
        ),
    ],
)
def test_settings_that_cannot_apply_give_a_warning(
    tmp_path: Path, content: str | bytes, reason: str
) -> None:
    path = write_settings(tmp_path, content)
    excludes = load_excludes(path)

    assert excludes is not None
    assert not excludes.active
    assert excludes.warnings == [f"{path}: {reason}; its claudeMdExcludes are not applied"]
    assert not excludes.matches("/a.md")


def test_settings_over_2_mib_are_not_applied(tmp_path: Path) -> None:
    padding = " " * MAX_SETTINGS_BYTES
    path = write_settings(tmp_path, '{"claudeMdExcludes": ["/a.md"]}' + padding)
    excludes = load_excludes(path)

    assert excludes is not None
    assert excludes.warnings == [f"{path}: larger than 2 MiB; its claudeMdExcludes are not applied"]


def test_a_settings_path_that_is_not_a_file_gives_a_warning(tmp_path: Path) -> None:
    (tmp_path / "settings.json").mkdir()
    excludes = load_excludes(str(tmp_path / "settings.json"))

    assert excludes is not None
    assert excludes.warnings == [
        f"{tmp_path}/settings.json: not a regular file; its claudeMdExcludes are not applied"
    ]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_a_named_pipe_does_not_block(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "settings.json")
    started = time.monotonic()

    excludes = load_excludes(str(tmp_path / "settings.json"))

    assert excludes is not None and not excludes.active
    assert time.monotonic() - started < 1


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0, reason="needs file permissions that apply"
)
def test_an_unreadable_settings_file_gives_a_warning(tmp_path: Path) -> None:
    path = write_settings(tmp_path, '{"claudeMdExcludes": ["/a.md"]}')
    os.chmod(path, 0)
    try:
        excludes = load_excludes(path)
    finally:
        os.chmod(path, 0o600)

    assert excludes is not None
    assert excludes.warnings == [f"{path}: Permission denied; its claudeMdExcludes are not applied"]
