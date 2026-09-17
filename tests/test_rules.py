"""Rule discovery: which rule files load at session start and when a file is read."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from c2c_rulesync.excludes import Excludes
from c2c_rulesync.rules import LOCAL, PROJECT, USER, Rule, RuleFinder, SessionRules


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
    write(tree, {".claude/rules/ts.md": scoped("src/*.ts"), "shared/s.md": scoped("lib/*.ts")})
    (tree / ".claude/rules/alias.md").symlink_to(tree / "shared/s.md")
    finder = RuleFinder(str(tree), user_rules_dir=None)
    session = SessionRules()

    session.mark_read(str(tree / ".claude/rules/ts.md"))
    session.mark_read(str(tree / ".claude/rules/alias.md"))

    assert session.take(finder.trigger_rules("src/a.ts")) == []
    # A rule read through a link is recorded by the path read, not its target.
    assert names(session.take(finder.trigger_rules("lib/a.ts")), tree) == ["shared/s.md"]


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


def test_a_linked_claude_directory_loads_its_own_rules_but_checks_their_links(
    tree: Path,
) -> None:
    write(
        tree,
        {
            "outside/.claude/rules/x.md": UNSCOPED,
            "outside/.claude/rules/y.md": scoped("a.ts"),
            "elsewhere/z.md": UNSCOPED,
            "proj/shared/in.md": UNSCOPED,
            "proj/a.ts": "",
        },
    )
    (tree / "outside/.claude/rules/z.md").symlink_to(tree / "elsewhere/z.md")
    (tree / "outside/.claude/rules/w.md").symlink_to(tree / "proj/shared/in.md")
    (tree / "proj/.claude").symlink_to(tree / "outside/.claude")
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None)

    assert names(finder.session_start_rules(), tree) == [
        "proj/shared/in.md",
        "outside/.claude/rules/x.md",
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


def test_reads_through_links_must_stay_inside_the_working_directory(tree: Path) -> None:
    write(
        tree,
        {
            "proj/.claude/rules/src.md": scoped("src/*.ts"),
            "proj/.claude/rules/ext.md": scoped("ext/*.ts"),
            "outside/.claude/rules/n.md": UNSCOPED,
            "outside/a.ts": "",
            "proj/src/in.ts": "",
        },
    )
    (tree / "proj/ext").symlink_to(tree / "outside")
    (tree / "proj/src/out.ts").symlink_to(tree / "outside/a.ts")
    (tree / "alias").symlink_to(tree / "proj")
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None)

    assert finder.trigger_rules("ext/a.ts") == []
    assert finder.trigger_rules("src/out.ts") == []
    assert names(finder.trigger_rules("src/in.ts"), tree) == ["proj/.claude/rules/src.md"]
    assert finder.trigger_rules(str(tree / "alias/src/in.ts")) == []
    assert finder.trigger_rules("src/in\0.ts") == []


def test_the_working_directory_spelling_counts_for_reads(tree: Path) -> None:
    write(tree, {"proj/.claude/rules/src.md": scoped("src/*.ts"), "proj/src/a.ts": ""})
    (tree / "alias").symlink_to(tree / "proj")
    finder = RuleFinder(str(tree / "alias"), user_rules_dir=None)

    assert names(finder.trigger_rules(str(tree / "alias/src/a.ts")), tree) == [
        "proj/.claude/rules/src.md"
    ]


def test_links_leaving_the_working_directory_are_checked_ignoring_case(tree: Path) -> None:
    (tree / "probe").mkdir()
    if (tree / "PROBE").exists():
        pytest.skip("needs a case-sensitive file system")
    write(tree, {"proj/shared.md": UNSCOPED, "Proj/.claude/rules/keep.md": UNSCOPED})
    (tree / "Proj/.claude/rules/l.md").symlink_to(tree / "proj/shared.md")
    finder = RuleFinder(str(tree / "Proj"), user_rules_dir=None)

    assert names(finder.session_start_rules(), tree) == [
        "Proj/.claude/rules/keep.md",
        "proj/shared.md",
    ]


@pytest.mark.skipif(sys.platform == "darwin", reason="APFS requires UTF-8 names")
def test_names_that_are_not_utf8_are_skipped(tree: Path) -> None:
    write(tree, {".claude/rules/ok.md": UNSCOPED})
    rules = os.fsencode(tree / ".claude/rules")
    with open(rules + b"/\xff.md", "w") as handle:
        handle.write(UNSCOPED)
    os.mkdir(rules + b"/d\xfe")
    with open(rules + b"/d\xfe/in.md", "w") as handle:
        handle.write(UNSCOPED)
    finder = RuleFinder(str(tree), user_rules_dir=None)

    assert names(finder.session_start_rules(), tree) == [".claude/rules/ok.md"]


def test_deeply_nested_rules_directories_do_not_raise(tree: Path) -> None:
    deep = tree / ".claude/rules"
    for _ in range(300):
        deep = deep / "d"
    deep.mkdir(parents=True)
    (deep / "deep.md").write_text(UNSCOPED)
    finder = RuleFinder(str(tree), user_rules_dir=None)

    assert finder.session_start_rules() == []
    assert any("nested too deeply" in warning for warning in finder.warnings)


# Worktrees --------------------------------------------------------------------------


def test_odd_git_files_do_not_raise_or_block(tree: Path) -> None:
    write(tree, {".claude/rules/r.md": UNSCOPED})
    (tree / "w").mkdir()
    (tree / "w/.git").write_bytes(b"gitdir: \xff\x00")
    assert names(RuleFinder(str(tree / "w"), None).session_start_rules(), tree) == [
        ".claude/rules/r.md"
    ]
    (tree / "w/.git").write_text(f"gitdir: {tree}/g")
    (tree / "g").mkdir()
    os.mkfifo(tree / "g/commondir")
    (tree / "g/gitdir").write_text(f"{tree}/w/.git")
    assert names(RuleFinder(str(tree / "w"), None).session_start_rules(), tree) == [
        ".claude/rules/r.md"
    ]


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


def test_a_worktree_of_a_bare_repository_skips_the_repository_directory(tree: Path) -> None:
    write(tree, {".claude/rules/parent.md": UNSCOPED, "src/f": ""})
    git("init", "-q", cwd=tree / "src")
    git("add", ".", cwd=tree / "src")
    git("commit", "-q", "-m", "init", cwd=tree / "src")
    git("clone", "-q", "--bare", "src", "bare", cwd=tree)
    git("worktree", "add", "-q", "wt", cwd=tree / "bare")
    write(tree, {"bare/.claude/rules/b.md": UNSCOPED})

    rules = RuleFinder(str(tree / "bare/wt"), user_rules_dir=None).session_start_rules()

    assert names(rules, tree) == [".claude/rules/parent.md"]


def test_a_worktree_that_does_not_point_back_is_not_skipped(tree: Path) -> None:
    main = tree / "main"
    write(tree, {"main/.claude/rules/main.md": UNSCOPED, "main/f": ""})
    git("init", "-q", cwd=main)
    git("add", ".", cwd=main)
    git("commit", "-q", "-m", "init", cwd=main)
    git("worktree", "add", "-q", ".claude/worktrees/w1", cwd=main)
    (main / ".git/worktrees/w1/gitdir").write_text("/nonexistent/w1/.git\n")

    rules = RuleFinder(str(main / ".claude/worktrees/w1"), user_rules_dir=None)

    assert names(rules.session_start_rules(), tree) == [
        "main/.claude/rules/main.md",
        "main/.claude/worktrees/w1/.claude/rules/main.md",
    ]


# Local instructions (CLAUDE.local.md) -----------------------------------------------

LOCAL_TREE = {
    "CLAUDE.local.md": "Outer local.\n",
    ".claude/rules/outer.md": UNSCOPED,
    "proj/CLAUDE.local.md": "Project local.\n",
    "proj/.claude/CLAUDE.local.md": "Not a place Claude Code loads from.\n",
    "proj/.claude/rules/a.md": UNSCOPED,
    "proj/pkg/CLAUDE.local.md": "Nested local.\n",
    "proj/pkg/sub/CLAUDE.local.md": "Deeper local.\n",
    "proj/pkg/sub/x.ts": "",
    "other/CLAUDE.local.md": "Outside local.\n",
    "other/y.ts": "",
}


def test_local_instructions_are_off_by_default(tree: Path) -> None:
    write(tree, LOCAL_TREE)
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None)

    assert names(finder.session_start_rules(), tree) == [
        ".claude/rules/outer.md",
        "proj/.claude/rules/a.md",
    ]
    assert finder.trigger_rules(str(tree / "proj/pkg/sub/x.ts")) == []


def test_session_start_loads_local_instructions_after_each_directorys_rules(tree: Path) -> None:
    write(tree, LOCAL_TREE)
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None, local_instructions=True)

    rules = finder.session_start_rules()

    assert names(rules, tree) == [
        ".claude/rules/outer.md",
        "CLAUDE.local.md",
        "proj/.claude/rules/a.md",
        "proj/CLAUDE.local.md",
    ]
    assert [rule.source for rule in rules] == [PROJECT, LOCAL, PROJECT, LOCAL]


def test_a_read_loads_local_instructions_of_every_directory_it_passes(tree: Path) -> None:
    write(tree, LOCAL_TREE)
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None, local_instructions=True)
    session = SessionRules()

    assert names(finder.trigger_rules(str(tree / "proj/pkg/sub/x.ts")), tree) == [
        "proj/pkg/CLAUDE.local.md",
        "proj/pkg/sub/CLAUDE.local.md",
    ]
    assert finder.trigger_rules(str(tree / "other/y.ts")) == []
    session.mark_read(str(tree / "proj/pkg/CLAUDE.local.md"))
    assert names(session.take(finder.trigger_rules(str(tree / "proj/pkg/a.ts"))), tree) == []


def test_local_instructions_load_whatever_their_paths(tree: Path) -> None:
    write(
        tree,
        {
            "proj/CLAUDE.local.md": '---\npaths: "src/**"\n---\nProject local.\n',
            "proj/pkg/CLAUDE.local.md": '---\npaths: "*.ts"\n---\nNested local.\n',
            "proj/pkg/a.txt": "",
        },
    )
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None, local_instructions=True)

    start = finder.session_start_rules()
    nested = finder.trigger_rules(str(tree / "proj/pkg/a.txt"))

    assert names(start, tree) == ["proj/CLAUDE.local.md"]
    assert start[0].body == "Project local.\n"
    assert names(nested, tree) == ["proj/pkg/CLAUDE.local.md"]
    # Claude Code reports the globs of a nested file it loads.
    assert nested[0].globs == ("*.ts",)


def test_empty_local_instructions_do_not_load(tree: Path) -> None:
    write(tree, {"CLAUDE.local.md": "<!-- a note -->\n", "proj/CLAUDE.local.md": "\n  \n"})
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None, local_instructions=True)

    assert finder.session_start_rules() == []


@pytest.mark.skipif(sys.platform == "win32", reason="needs symbolic links")
def test_links_to_local_instructions_are_followed_anywhere(tree: Path) -> None:
    write(
        tree,
        {
            "outside/project.md": "Linked from the project.\n",
            "outside/nested.md": "Linked from a nested directory.\n",
            "proj/pkg/a.ts": "",
            "proj/broken/a.ts": "",
        },
    )
    (tree / "proj/CLAUDE.local.md").symlink_to(tree / "outside/project.md")
    (tree / "proj/pkg/CLAUDE.local.md").symlink_to(tree / "outside/nested.md")
    (tree / "proj/broken/CLAUDE.local.md").symlink_to(tree / "missing.md")
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None, local_instructions=True)

    assert names(finder.session_start_rules(), tree) == ["proj/CLAUDE.local.md"]
    assert names(finder.trigger_rules(str(tree / "proj/pkg/a.ts")), tree) == [
        "proj/pkg/CLAUDE.local.md"
    ]
    assert finder.trigger_rules(str(tree / "proj/broken/a.ts")) == []
    assert [warning for warning in finder.warnings if "cannot be followed" in warning] == [
        f"{tree / 'proj/broken/CLAUDE.local.md'}: a link that cannot be followed; skipped"
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="needs symbolic links")
def test_reading_a_linked_local_instructions_file_by_its_link_counts_as_loading_it(
    tree: Path,
) -> None:
    write(tree, {"proj/shared/lib.md": "Linked.\n", "proj/lib/a.ts": ""})
    (tree / "proj/lib/CLAUDE.local.md").symlink_to(tree / "proj/shared/lib.md")
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None, local_instructions=True)
    session = SessionRules()

    session.mark_read(str(tree / "proj/lib/CLAUDE.local.md"))

    assert session.take(finder.trigger_rules(str(tree / "proj/lib/a.ts"))) == []


def test_a_local_instructions_path_that_is_not_a_file_is_skipped(tree: Path) -> None:
    (tree / "proj/CLAUDE.local.md").mkdir(parents=True)
    if hasattr(os, "mkfifo"):
        os.mkfifo(tree / "CLAUDE.local.md")
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None, local_instructions=True)

    assert finder.session_start_rules() == []


def test_local_instructions_over_4_mib_are_skipped_with_a_warning(tree: Path) -> None:
    write(tree, {"CLAUDE.local.md": "x" * (4 * 1024 * 1024 + 1)})
    finder = RuleFinder(str(tree), user_rules_dir=None, local_instructions=True)

    assert finder.session_start_rules() == []
    assert finder.warnings == [
        f"{tree / 'CLAUDE.local.md'}: larger than 4 MiB; Claude Code skips it"
    ]


def test_a_worktree_nested_in_its_repository_keeps_local_instructions(tree: Path) -> None:
    main = tree / "main"
    write(
        tree,
        {
            "CLAUDE.local.md": "Above the repository.\n",
            "main/.gitignore": "CLAUDE.local.md\n",
            "main/CLAUDE.local.md": "Main repository.\n",
            "main/.claude/rules/main.md": UNSCOPED,
            "main/f": "",
        },
    )
    git("init", "-q", cwd=main)
    git("add", ".", cwd=main)
    git("commit", "-q", "-m", "init", cwd=main)
    git("worktree", "add", "-q", ".claude/worktrees/w1", cwd=main)
    worktree = main / ".claude/worktrees/w1"
    write(worktree, {"CLAUDE.local.md": "Worktree.\n"})

    rules = RuleFinder(str(worktree), user_rules_dir=None, local_instructions=True)

    assert names(rules.session_start_rules(), tree) == [
        "CLAUDE.local.md",
        "main/CLAUDE.local.md",
        "main/.claude/worktrees/w1/.claude/rules/main.md",
        "main/.claude/worktrees/w1/CLAUDE.local.md",
    ]


def test_imports_in_local_instructions_are_kept_and_warned_about(tree: Path) -> None:
    body = "See @AGENTS.md and @~/.claude/a.md, @b.md, @c.md.\n```\n@fenced.md\n```\n"
    write(
        tree,
        {
            "CLAUDE.local.md": body,
            "AGENTS.md": "",
            "home/.claude/a.md": "",
            "b.md": "",
            "c.md": "",
            "proj/CLAUDE.local.md": "```\n@fenced.md\n```\n",
            "proj/fenced.md": "",
            "proj/.claude/rules/r.md": "See @AGENTS.md.\n",
            "proj/AGENTS.md": "",
        },
    )
    finder = RuleFinder(
        str(tree / "proj"), user_rules_dir=None, local_instructions=True, home=str(tree / "home")
    )

    rules = finder.session_start_rules()

    assert [rule.body for rule in rules if rule.source == LOCAL] == [body, "```\n@fenced.md\n```\n"]
    assert finder.warnings == [
        f"{tree / 'CLAUDE.local.md'}: @path imports are not expanded; Codex receives this text "
        "without the files they name (@AGENTS.md, @~/.claude/a.md, @b.md, and 1 more)"
    ]


def test_imports_that_name_no_file_are_not_warned_about(tree: Path) -> None:
    write(
        tree,
        {
            "CLAUDE.local.md": (
                "Ping @alice, use @Override and @types/node, see @docs and @~/x.md.\n"
            ),
            "docs/a.md": "",
            "x.md": "",
        },
    )
    finder = RuleFinder(str(tree), user_rules_dir=None, local_instructions=True)

    assert names(finder.session_start_rules(), tree) == ["CLAUDE.local.md"]
    assert finder.warnings == []


@pytest.mark.skipif(sys.platform == "win32", reason="needs symbolic links")
def test_imports_of_a_linked_file_are_found_beside_the_link_or_its_target(tree: Path) -> None:
    write(
        tree,
        {
            "notes/local.md": "See @beside-target.md and @beside-link.md and @nowhere.md.\n",
            "notes/beside-target.md": "",
            "proj/beside-link.md": "",
        },
    )
    (tree / "proj/CLAUDE.local.md").symlink_to(tree / "notes/local.md")
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None, local_instructions=True)

    finder.session_start_rules()

    assert len(finder.warnings) == 1
    assert "(@beside-target.md, @beside-link.md)" in finder.warnings[0]


def test_front_matter_of_local_instructions_raises_no_warning(tree: Path) -> None:
    write(tree, {"CLAUDE.local.md": "---\npaths:\n  - **/*.ts\n---\nLocal.\n"})
    finder = RuleFinder(str(tree), user_rules_dir=None, local_instructions=True)

    assert names(finder.session_start_rules(), tree) == ["CLAUDE.local.md"]
    assert finder.warnings == []


# claudeMdExcludes -----------------------------------------------------------------


def excluding(tree: Path, *patterns: str) -> Excludes:
    root = os.path.realpath(tree)
    return Excludes([pattern.replace("{root}", root) for pattern in patterns], "settings.json")


def test_excluded_rules_load_neither_at_session_start_nor_after_a_read(tree: Path) -> None:
    write(
        tree,
        {
            ".claude/rules/outer.md": UNSCOPED,
            "proj/.claude/rules/cwd.md": UNSCOPED,
            "proj/.claude/rules/kept.md": UNSCOPED,
            "proj/.claude/rules/scoped.md": scoped("src/*.ts"),
            "proj/.claude/rules/scoped-kept.md": scoped("src/*.ts"),
            "proj/src/.claude/rules/nested.md": UNSCOPED,
            "proj/src/.claude/rules/nested-scoped.md": scoped("*.ts"),
            "proj/src/a.ts": "",
            "user/mine.md": UNSCOPED,
            "user/mine-scoped.md": scoped("src/*.ts"),
        },
    )
    excludes = excluding(
        tree,
        "{root}/.claude/rules/*.md",
        "{root}/proj/.claude/rules/{cwd,scoped}.md",
        "**/src/.claude/rules/**",
        "{root}/user/mine*.md",
    )
    finder = RuleFinder(str(tree / "proj"), str(tree / "user"), excludes=excludes)

    assert names(finder.session_start_rules(), tree) == ["proj/.claude/rules/kept.md"]
    assert names(finder.trigger_rules(str(tree / "proj/src/a.ts")), tree) == [
        "proj/.claude/rules/scoped-kept.md"
    ]
    assert finder.warnings == []


def test_excluded_local_instructions_do_not_load(tree: Path) -> None:
    write(tree, LOCAL_TREE)
    excludes = excluding(tree, "{root}/CLAUDE.local.md", "{root}/proj/pkg/CLAUDE.local.md")
    finder = RuleFinder(
        str(tree / "proj"), user_rules_dir=None, local_instructions=True, excludes=excludes
    )

    assert names(finder.session_start_rules(), tree) == [
        ".claude/rules/outer.md",
        "proj/.claude/rules/a.md",
        "proj/CLAUDE.local.md",
    ]
    assert names(finder.trigger_rules(str(tree / "proj/pkg/sub/x.ts")), tree) == [
        "proj/pkg/sub/CLAUDE.local.md"
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="needs symbolic links")
def test_a_linked_rule_is_excluded_by_its_path_under_the_rules_directory_or_its_target(
    tree: Path,
) -> None:
    write(
        tree,
        {
            "proj/.claude/rules/kept.md": UNSCOPED,
            "proj/shared/by-link.md": UNSCOPED,
            "proj/shared/by-target.md": UNSCOPED,
            "proj/shared-dir/in-dir.md": UNSCOPED,
            "proj/shared-dir/in-dir-kept.md": UNSCOPED,
            "linked/rules-real/through-link.md": UNSCOPED,
            "linked/rules-real/through-link-kept.md": UNSCOPED,
        },
    )
    rules = tree / "proj/.claude/rules"
    (rules / "l-by-link.md").symlink_to("../../shared/by-link.md")
    (rules / "l-by-target.md").symlink_to("../../shared/by-target.md")
    (rules / "l-dir").symlink_to("../../shared-dir")
    (tree / "linked/.claude").mkdir()
    (tree / "linked/.claude/rules").symlink_to("../rules-real")
    excludes = excluding(
        tree,
        "{root}/proj/.claude/rules/l-by-link.md",
        "{root}/proj/shared/by-target.md",
        "{root}/proj/.claude/rules/l-dir/in-dir.md",
        "{root}/linked/.claude/rules/through-link.md",
    )

    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None, excludes=excludes)
    assert names(finder.session_start_rules(), tree) == [
        "proj/.claude/rules/kept.md",
        "proj/shared-dir/in-dir-kept.md",
    ]
    finder = RuleFinder(str(tree / "linked"), user_rules_dir=None, excludes=excludes)
    assert names(finder.session_start_rules(), tree) == ["linked/rules-real/through-link-kept.md"]


@pytest.mark.skipif(sys.platform == "win32", reason="needs symbolic links")
def test_a_linked_local_instructions_file_is_excluded_only_by_its_link(tree: Path) -> None:
    write(tree, {"outer-target.md": "Outer.\n", "proj-target.md": "Project.\n"})
    (tree / "proj").mkdir()
    (tree / "CLAUDE.local.md").symlink_to("outer-target.md")
    (tree / "proj/CLAUDE.local.md").symlink_to("../proj-target.md")
    excludes = excluding(tree, "{root}/CLAUDE.local.md", "{root}/proj-target.md")
    finder = RuleFinder(
        str(tree / "proj"), user_rules_dir=None, local_instructions=True, excludes=excludes
    )

    assert names(finder.session_start_rules(), tree) == ["proj/CLAUDE.local.md"]


@pytest.mark.skipif(sys.platform == "win32", reason="needs symbolic links")
def test_an_excluded_link_leaves_the_file_it_leads_to_loadable(tree: Path) -> None:
    write(tree, {"proj/.claude/rules/shadow.md": scoped("pkg/src/*.ts"), "proj/pkg/src/x.ts": ""})
    (tree / "proj/pkg/.claude/rules").mkdir(parents=True)
    (tree / "proj/pkg/.claude/rules/l.md").symlink_to("../../../.claude/rules/shadow.md")
    read = str(tree / "proj/pkg/src/x.ts")

    # Reached first through the nested link, whose globs do not match, the rule
    # counts as processed and the working directory's copy does not load.
    assert RuleFinder(str(tree / "proj"), user_rules_dir=None).trigger_rules(read) == []
    excludes = excluding(tree, "{root}/proj/pkg/.claude/rules/l.md")
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None, excludes=excludes)
    assert names(finder.trigger_rules(read), tree) == ["proj/.claude/rules/shadow.md"]


@pytest.mark.skipif(sys.platform == "win32", reason="needs symbolic links")
def test_excluded_files_raise_no_warnings(tree: Path) -> None:
    write(
        tree,
        {
            "proj/.claude/rules/bad-yaml.md": "---\n%YAML 1.2\npaths: a.md\n---\nBad.\n",
            "proj/CLAUDE.local.md": "See @notes.md\n",
            "proj/notes.md": "Notes.\n",
            "proj/pkg/a.txt": "",
        },
    )
    (tree / "proj/.claude/rules/big.md").write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    (tree / "proj/.claude/rules/bad-utf8.md").write_bytes(b"\xff\n")
    (tree / "proj/.claude/rules/broken.md").symlink_to("missing.md")
    (tree / "proj/.claude/rules/sub").mkdir()
    (tree / "proj/.claude/rules/sub/dangling.md").symlink_to("../../../gone/target.md")
    (tree / "proj/pkg/CLAUDE.local.md").symlink_to("missing.md")

    def warnings(excludes: Excludes | None) -> list[str]:
        finder = RuleFinder(
            str(tree / "proj"), user_rules_dir=None, local_instructions=True, excludes=excludes
        )
        finder.session_start_rules()
        finder.trigger_rules(str(tree / "proj/pkg/a.txt"))
        return finder.warnings

    unexcluded = " ".join(warnings(None))
    for name in (
        "bad-yaml.md",
        "big.md",
        "bad-utf8.md",
        "broken.md",
        "dangling.md",
        "pkg/CLAUDE.local.md",
    ):
        assert name in unexcluded
    assert "@notes.md" in unexcluded
    excludes = excluding(
        tree,
        "{root}/proj/.claude/rules/*.md",
        "{root}/proj/gone/target.md",
        "{root}/proj/**/CLAUDE.local.md",
    )
    assert warnings(excludes) == []


def test_the_warnings_of_the_patterns_come_first(tree: Path) -> None:
    write(tree, {"proj/.claude/rules/bad-yaml.md": "---\n%YAML 1.2\npaths: a.md\n---\nBad.\n"})
    excludes = Excludes(["relative.md"], "settings.json")
    finder = RuleFinder(str(tree / "proj"), user_rules_dir=None, excludes=excludes)

    finder.session_start_rules()

    assert finder.warnings[0] == excludes.warnings[0]
    assert len(finder.warnings) == 2
