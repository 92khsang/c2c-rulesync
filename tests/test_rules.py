"""Rule discovery: which rule files load at session start and when a file is read."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from c2c_rulesync.rules import PROJECT, USER, Rule, RuleFinder, SessionRules


def write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def scoped(*globs: str) -> str:
    items = "".join(f'  - "{glob}"\n' for glob in globs)
    return f"---\npaths:\n{items}---\n\nScoped rule.\n"


UNSCOPED = "Unscoped rule.\n"


def names(rules: list[Rule], root: Path) -> list[str]:
    return [os.path.relpath(rule.path, os.path.realpath(root)) for rule in rules]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    return root


# Session start ----------------------------------------------------------------


def test_session_start_loads_unscoped_rules_from_ancestors_outermost_first(tree: Path) -> None:
    write(
        tree,
        {
            ".claude/rules/outer.md": UNSCOPED,
            ".claude/rules/outer-scoped.md": scoped("*.ts"),
            "proj/.claude/rules/b.md": UNSCOPED,
            "proj/.claude/rules/a.md": UNSCOPED,
            "proj/.claude/rules/sub/c.md": UNSCOPED,
            "proj/pkg/.claude/rules/nested.md": UNSCOPED,
        },
    )
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None)

    assert names(finder.session_start_rules(), tree) == [
        ".claude/rules/outer.md",
        "proj/.claude/rules/a.md",
        "proj/.claude/rules/b.md",
        "proj/.claude/rules/sub/c.md",
    ]


def test_user_rules_load_first_and_win_over_the_same_file_as_a_project_rule(tree: Path) -> None:
    write(tree, {"home/.claude/rules/mine.md": UNSCOPED})
    finder = RuleFinder(str(tree / "home/proj"), user_rules_dir=str(tree / "home/.claude/rules"))
    (tree / "home/proj").mkdir(parents=True)

    rules = finder.session_start_rules()

    assert names(rules, tree) == ["home/.claude/rules/mine.md"]
    assert [rule.source for rule in rules] == [USER]


def test_empty_bodies_and_non_markdown_files_are_ignored(tree: Path) -> None:
    write(
        tree,
        {
            ".claude/rules/empty.md": "---\npaths: a\n---\n \n",
            ".claude/rules/blank.md": "\n\t\n",
            ".claude/rules/notes.txt": UNSCOPED,
            ".claude/rules/UPPER.MD": UNSCOPED,
            ".claude/rules/ok.md": UNSCOPED,
        },
    )
    finder = RuleFinder(str(tree), user_rules_dir=None)

    assert names(finder.session_start_rules(), tree) == [".claude/rules/ok.md"]


# Reading a file -----------------------------------------------------------------


def test_reading_a_file_loads_matching_rules_in_claude_code_order(tree: Path) -> None:
    write(
        tree,
        {
            "user/rules/u.md": scoped("pkg/src/*.ts"),
            ".claude/rules/outer.md": scoped("proj/pkg/**"),
            "proj/.claude/rules/cwd.md": scoped("pkg/src/*.ts"),
            "proj/.claude/rules/cwd-unscoped.md": UNSCOPED,
            "proj/pkg/.claude/rules/pkg.md": scoped("src/*.ts"),
            "proj/pkg/.claude/rules/pkg-unscoped.md": UNSCOPED,
            "proj/pkg/src/.claude/rules/deep.md": UNSCOPED,
            "proj/pkg/src/a.ts": "",
        },
    )
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=str(tree / "user/rules"))

    assert names(finder.trigger_rules("pkg/src/a.ts"), tree) == [
        "user/rules/u.md",
        "proj/pkg/.claude/rules/pkg-unscoped.md",
        "proj/pkg/.claude/rules/pkg.md",
        "proj/pkg/src/.claude/rules/deep.md",
        ".claude/rules/outer.md",
        "proj/.claude/rules/cwd.md",
    ]


def test_globs_match_relative_to_the_directory_holding_the_rules(tree: Path) -> None:
    write(
        tree,
        {
            "apps/journal/.claude/rules/svelte.md": scoped("src/**/*.svelte"),
            "apps/journal/src/App.svelte": "",
            "apps/portfolio/src/App.svelte": "",
        },
    )
    finder = RuleFinder(str(tree), user_rules_dir=None)

    assert names(finder.trigger_rules("apps/journal/src/App.svelte"), tree) == [
        "apps/journal/.claude/rules/svelte.md"
    ]
    assert finder.trigger_rules("apps/portfolio/src/App.svelte") == []


def test_user_rule_globs_match_relative_to_the_working_directory(tree: Path) -> None:
    write(
        tree, {"user/rules/u.md": scoped("src/*.ts"), "proj/pkg/src/a.ts": "", "proj/src/b.ts": ""}
    )
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=str(tree / "user/rules"))

    assert finder.trigger_rules("pkg/src/a.ts") == []
    assert names(finder.trigger_rules("src/b.ts"), tree) == ["user/rules/u.md"]


def test_a_directory_glob_matches_at_any_depth(tree: Path) -> None:
    write(tree, {".claude/rules/src.md": scoped("src/**"), "packages/x/src/a.ts": ""})
    finder = RuleFinder(str(tree), user_rules_dir=None)

    assert names(finder.trigger_rules("packages/x/src/a.ts"), tree) == [".claude/rules/src.md"]


def test_files_outside_the_working_directory_load_nothing(tree: Path) -> None:
    write(tree, {".claude/rules/any.md": scoped("*.ts"), "proj/a.ts": "", "other/b.ts": ""})
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None)

    assert names(finder.trigger_rules("a.ts"), tree) == [".claude/rules/any.md"]
    assert finder.trigger_rules(str(tree / "other/b.ts")) == []
    assert finder.trigger_rules("../other/b.ts") == []


def test_the_same_rule_loads_once_per_read(tree: Path) -> None:
    write(tree, {"proj/.claude/rules/r.md": scoped("*.ts")})
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=str(tree / "proj/.claude/rules"))

    rules = finder.trigger_rules("a.ts")

    assert names(rules, tree) == ["proj/.claude/rules/r.md"]
    assert [rule.source for rule in rules] == [USER]


# One session ------------------------------------------------------------------------


def test_a_session_loads_each_rule_once(tree: Path) -> None:
    write(tree, {".claude/rules/u.md": UNSCOPED, ".claude/rules/ts.md": scoped("src/*.ts")})
    finder = RuleFinder(str(tree), user_rules_dir=None)
    session = SessionRules()

    assert names(session.take(finder.session_start_rules()), tree) == [".claude/rules/u.md"]
    assert session.take(finder.session_start_rules()) == []
    assert names(session.take(finder.trigger_rules("src/a.ts")), tree) == [".claude/rules/ts.md"]
    assert session.take(finder.trigger_rules("src/b.ts")) == []


def test_reading_a_rule_file_counts_as_loading_it(tree: Path) -> None:
    write(tree, {".claude/rules/ts.md": scoped("src/*.ts")})
    finder = RuleFinder(str(tree), user_rules_dir=None)
    session = SessionRules()

    session.mark_read(str(tree / ".claude/rules/ts.md"))

    assert session.take(finder.trigger_rules("src/a.ts")) == []


def test_a_session_continues_from_loaded_paths(tree: Path) -> None:
    write(tree, {".claude/rules/u.md": UNSCOPED, ".claude/rules/v.md": UNSCOPED})
    loaded = {os.path.realpath(tree / ".claude/rules/u.md")}
    session = SessionRules(loaded)

    rules = session.take(RuleFinder(str(tree), user_rules_dir=None).session_start_rules())

    assert names(rules, tree) == [".claude/rules/v.md"]
    assert loaded == {os.path.realpath(tree / ".claude/rules" / name) for name in ("u.md", "v.md")}


# Links ------------------------------------------------------------------------------


def test_links_inside_the_working_directory_are_followed(tree: Path) -> None:
    write(tree, {"shared/style.md": UNSCOPED, "lib/extra/x.md": UNSCOPED})
    rules_dir = tree / ".claude/rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "style.md").symlink_to(tree / "shared/style.md")
    (rules_dir / "extra").symlink_to(tree / "lib/extra")
    finder = RuleFinder(str(tree), user_rules_dir=None)

    assert names(finder.session_start_rules(), tree) == ["lib/extra/x.md", "shared/style.md"]


def test_project_links_leaving_the_working_directory_are_skipped(tree: Path) -> None:
    write(tree, {"outside/out.md": UNSCOPED, "proj/.claude/rules/in.md": UNSCOPED})
    (tree / "proj/.claude/rules/out.md").symlink_to(tree / "outside/out.md")
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None)

    assert names(finder.session_start_rules(), tree) == ["proj/.claude/rules/in.md"]


def test_a_rules_directory_linking_outside_the_working_directory_is_skipped(tree: Path) -> None:
    write(tree, {"outside/rules/out.md": UNSCOPED})
    (tree / "proj/.claude").mkdir(parents=True)
    (tree / "proj/.claude/rules").symlink_to(tree / "outside/rules")
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None)

    assert finder.session_start_rules() == []


def test_a_linked_claude_directory_loads_rules_from_outside_the_working_directory(
    tree: Path,
) -> None:
    write(
        tree,
        {
            "outside/.claude/rules/x.md": UNSCOPED,
            "outside/.claude/rules/y.md": scoped("a.ts"),
            "elsewhere/z.md": UNSCOPED,
            "proj/a.ts": "",
        },
    )
    (tree / "outside/.claude/rules/z.md").symlink_to(tree / "elsewhere/z.md")
    (tree / "proj/.claude").symlink_to(tree / "outside/.claude")
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None)

    assert names(finder.session_start_rules(), tree) == [
        "outside/.claude/rules/x.md",
        "elsewhere/z.md",
    ]
    assert names(finder.trigger_rules("a.ts"), tree) == ["outside/.claude/rules/y.md"]


def test_user_rules_may_link_anywhere(tree: Path) -> None:
    write(tree, {"dotfiles/go.md": UNSCOPED})
    (tree / "user/rules").mkdir(parents=True)
    (tree / "user/rules/go.md").symlink_to(tree / "dotfiles/go.md")
    (tree / "proj").mkdir()
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=str(tree / "user/rules"))

    assert names(finder.session_start_rules(), tree) == ["dotfiles/go.md"]


def test_a_broken_link_is_skipped_with_a_warning(tree: Path) -> None:
    write(tree, {".claude/rules/a.md": UNSCOPED, ".claude/rules/sub/z.md": UNSCOPED})
    (tree / ".claude/rules/b-broken.md").symlink_to(tree / "missing.md")
    (tree / ".claude/rules/sub/a-broken.md").symlink_to(tree / "missing.md")
    finder = RuleFinder(str(tree), user_rules_dir=None)

    assert names(finder.session_start_rules(), tree) == [
        ".claude/rules/a.md",
        ".claude/rules/sub/z.md",
    ]
    assert len([warning for warning in finder.warnings if "cannot be followed" in warning]) == 2


def test_a_link_cycle_terminates(tree: Path) -> None:
    write(tree, {".claude/rules/a.md": UNSCOPED})
    (tree / ".claude/rules/loop").symlink_to(tree / ".claude/rules")
    finder = RuleFinder(str(tree), user_rules_dir=None)

    assert names(finder.session_start_rules(), tree) == [".claude/rules/a.md"]


# Worktrees --------------------------------------------------------------------------


def git(*args: str, cwd: Path) -> None:
    identity = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com"}
    identity |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    environment = {**os.environ, **identity}
    subprocess.run(["git", *args], cwd=cwd, env=environment, check=True, capture_output=True)


def test_a_worktree_nested_in_its_repository_skips_the_repository_rules(tree: Path) -> None:
    main = tree / "main"
    write(
        tree,
        {".claude/rules/parent.md": UNSCOPED, "main/.claude/rules/main.md": UNSCOPED, "main/f": ""},
    )
    git("init", "-q", cwd=main)
    git("add", ".", cwd=main)
    git("commit", "-q", "-m", "init", cwd=main)
    git("worktree", "add", "-q", ".claude/worktrees/w1", cwd=main)
    worktree = main / ".claude/worktrees/w1"

    rules = names(RuleFinder(str(worktree), user_rules_dir=None).session_start_rules(), tree)

    assert rules == [".claude/rules/parent.md", "main/.claude/worktrees/w1/.claude/rules/main.md"]


def test_rule_source_is_project_for_project_directories(tree: Path) -> None:
    write(tree, {".claude/rules/p.md": UNSCOPED})

    assert [r.source for r in RuleFinder(str(tree), None).session_start_rules()] == [PROJECT]
