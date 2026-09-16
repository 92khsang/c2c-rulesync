"""Trigger paths: the files a Codex tool call reads or changes."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from c2c_rulesync.patch import patch_paths
from c2c_rulesync.shell import shell_paths
from c2c_rulesync.touched import touched_paths

CWD = "/repo"
HOME = "/home/me"


def paths(command: str, cwd: str = CWD) -> list[str]:
    return shell_paths(command, cwd, HOME)


# Tool calls --------------------------------------------------------------------


def test_apply_patch_headers_are_trigger_paths() -> None:
    patch = (
        "*** Begin Patch\n"
        "*** Add File: src/new.ts\n+x\n"
        "*** Update File: src/old.ts\n*** Move to: apps/web/src/moved.ts\n@@\n-a\n+b\n"
        "*** Delete File: /repo/gone.ts\n"
        "*** End Patch\n"
    )

    assert touched_paths("apply_patch", {"command": patch}, CWD, HOME) == [
        "/repo/src/new.ts",
        "/repo/src/old.ts",
        "/repo/apps/web/src/moved.ts",
        "/repo/gone.ts",
    ]


def test_bash_commands_are_read() -> None:
    assert touched_paths("Bash", {"command": "cat src/a.ts"}, CWD, HOME) == ["/repo/src/a.ts"]


@pytest.mark.parametrize("tool_name", ["view_image", "mcp__fs__read_file", "Bash"])
def test_path_fields_are_trigger_paths_for_any_tool(tool_name: str) -> None:
    tool_input = {
        "path": "img/a.png",
        "file_path": "/abs/b.md",
        "filePath": "c.md",
        "other": "d.md",
    }

    assert touched_paths(tool_name, tool_input, CWD, HOME) == [
        "/repo/img/a.png",
        "/abs/b.md",
        "/repo/c.md",
    ]


@pytest.mark.parametrize("tool_input", ["cat a.ts", None, ["cat a.ts"], {"command": ["cat", "a"]}])
def test_unusable_tool_input_yields_nothing(tool_input: object) -> None:
    assert touched_paths("Bash", tool_input, CWD, HOME) == []


def test_other_tools_do_not_read_command() -> None:
    assert touched_paths("update_plan", {"command": "cat a.ts"}, CWD, HOME) == []


# Patches -----------------------------------------------------------------------------


def test_patch_headers_are_read_after_trimming() -> None:
    patch = "  *** Begin Patch\n  *** Update File: src/a.css \n@@\n-a\n+b\n*** End Patch"

    assert patch_paths(patch) == ["src/a.css"]


def test_header_like_lines_outside_a_patch_are_ignored() -> None:
    assert patch_paths("*** Update File: a.ts\n--- a/b.ts\n+++ b/c.ts\n") == []


# Shell: the upstream codex-path-rules cases ------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat src/app.ts", ["src/app.ts"]),
        ("sed -n '1,20p' src/styles/stage.css", ["src/styles/stage.css"]),
        ("FOO=bar cat src/app.ts", ["src/app.ts"]),
        ("bash -c 'cat src/app.ts'", ["src/app.ts"]),
        (
            "cd apps/journal && cat src/components/App.svelte",
            ["apps/journal/src/components/App.svelte"],
        ),
        ("cd apps/journal | cat src/components/App.svelte", ["src/components/App.svelte"]),
        ("cat a.ts | grep x b.ts", ["a.ts", "b.ts"]),
        ("grep -e foo a.ts", ["a.ts"]),
        ("rg -e foo src/file.ts", ["src/file.ts"]),
        ("echo hello", []),
        ("git diff main -- src/lib.rs README.md", ["src/lib.rs", "README.md"]),
        ("git diff -- RULE=name", ["RULE=name"]),
        ("git show HEAD -- src/touched.rs", ["src/touched.rs"]),
        ("git log -- README.md", ["README.md"]),
        ("git blame HEAD -- src/lib.rs", ["src/lib.rs"]),
        ("git diff main src/lib.rs", []),
        ("git show HEAD:README.md", []),
        ("git status -- src/lib.rs", []),
    ],
)
def test_upstream_cases(command: str, expected: list[str]) -> None:
    assert paths(command) == [f"/repo/{path}" for path in expected]


def test_directories_are_not_trigger_paths(tmp_path: Path) -> None:
    (tmp_path / "src" / "styles").mkdir(parents=True)

    assert shell_paths("rg --files src/styles", str(tmp_path), HOME) == []
    assert shell_paths("grep -rn TODO src README.md", str(tmp_path), HOME) == [
        f"{tmp_path}/README.md"
    ]


def test_find_is_not_read() -> None:
    assert paths("find src tests -type f") == []


# Shell: separators, heredocs and quoting ---------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cd apps/journal\ncat src/App.svelte", ["apps/journal/src/App.svelte"]),
        ("echo start\ncat src/a.ts", ["src/a.ts"]),
        ("cd sub & cat a.ts", ["a.ts"]),
        ("cat > notes.md <<'EOF'\ndon't do this\nEOF\ncat src/a.ts", ["notes.md", "src/a.ts"]),
        ("cat <<-EOF > out.txt\n\tcat hidden.ts\n\tEOF\ncat b.ts", ["out.txt", "b.ts"]),
        ("(cd apps/journal && cat src/App.svelte)", ["apps/journal/src/App.svelte"]),
        ("(cd apps/journal) && cat a.ts", ["a.ts"]),
        ("cd a || exit 1; cat b.ts", ["a/b.ts"]),
        ("cat a.ts # see b.ts", ["a.ts"]),
        (
            "cat 'my file.ts' \"other file.ts\" esc\\ aped.ts",
            ["my file.ts", "other file.ts", "esc aped.ts"],
        ),
        ("cat $'tab\\there.ts'", ["tab\there.ts"]),
        ("cat a\\\n.ts", ["a.ts"]),
        ("if true; then cat a.ts; fi", ["a.ts"]),
        ("while read l; do echo $l; done < list.txt", ["list.txt"]),
        ("{ cat a.ts; }", ["a.ts"]),
    ],
)
def test_script_structure(command: str, expected: list[str]) -> None:
    assert paths(command) == [f"/repo/{path}" for path in expected]


# Shell: wrappers and prefixes ------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("bash scripts/x.sh && grep -c TODO src/a.ts", ["src/a.ts"]),
        ("bash -lc 'cat a.ts' && cat src/b.css", ["a.ts", "src/b.css"]),
        ("bash -euc 'cat a.ts'", ["a.ts"]),
        ("bash --noprofile --norc -o pipefail -c 'cat a.ts'", ["a.ts"]),
        ('/bin/zsh -lc "cd web && sed -n 1,5p index.html"', ["web/index.html"]),
        ("timeout 5 cat src/a.ts", ["src/a.ts"]),
        ("env X=1 cat a", ["a"]),
        ("sudo -u root nice -n 5 cat a.ts", ["a.ts"]),
        ("command cat a.ts", ["a.ts"]),
        ("command -v cat", []),
        ("env -C other cat a.ts", []),
        ("git --no-pager diff -- src/a.ts", ["src/a.ts"]),
        ("git -C sub -c core.pager=cat log -- a.ts", ["sub/a.ts"]),
        ("git blame -L 1,20 src/a.ts", ["src/a.ts"]),
    ],
)
def test_wrappers(command: str, expected: list[str]) -> None:
    assert paths(command) == [f"/repo/{path}" for path in expected]


# Shell: arguments ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("rg '' src/a.ts", ["src/a.ts"]),
        ("sed --expression=1,20p src/a.ts", ["src/a.ts"]),
        ("sed -e 1p -e 2p a.ts b.ts", ["a.ts", "b.ts"]),
        ("sed -i '' 's/a/b/' a.ts", ["a.ts"]),
        ("sed -i.bak 's/a/b/' a.ts", ["a.ts"]),
        ("awk -F: -v n=1 'NR<=n' a.csv", ["a.csv"]),
        ("head -n 20 a.ts b.ts", ["a.ts", "b.ts"]),
        ("tail -20 a.log", ["a.log"]),
        ("nl -ba a.ts", ["a.ts"]),
        ("grep -m 1 -A 2 pattern a.ts", ["a.ts"]),
        ("rg -g '*.ts' -t ts foo src/a.ts", ["src/a.ts"]),
        ("cat -- -dash.md", ["-dash.md"]),
        ("cat -n -", []),
    ],
)
def test_arguments(command: str, expected: list[str]) -> None:
    assert paths(command) == [f"/repo/{path}" for path in expected]


# Shell: redirections ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat<src/a.ts", ["src/a.ts"]),
        ("cat src/a.ts 2>/dev/null", ["src/a.ts"]),
        ("echo x > out.md 2>&1", ["out.md"]),
        ("echo x >> log.md", ["log.md"]),
        ("printf x &> both.md", ["both.md"]),
        ("wc -l < in.txt > out.txt", ["in.txt", "out.txt"]),
        ("exec 3< fd.txt", ["fd.txt"]),
        ("cat <<< 'here string'", []),
        ("echo x | tee -a copy.md", ["copy.md"]),
    ],
)
def test_redirections(command: str, expected: list[str]) -> None:
    assert paths(command) == [f"/repo/{path}" for path in expected]


# Shell: values only the shell knows ------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("cat ~/notes.md", ["/home/me/notes.md"]),
        ("cat ~other/notes.md", []),
        ("cd ~/other && cat src/a.ts", ["/home/me/other/src/a.ts"]),
        ("cd $HOME && cat a.ts /abs.ts", ["/abs.ts"]),
        ("cd - && cat a.ts", []),
        ("cd && cat a.ts", []),
        ('cd "$(git rev-parse --show-toplevel)" && cat a.ts', []),
        ("popd; cat a.ts", []),
        ('cat "$f" ${g} `h` $(i)', []),
        ("cat src/*.ts 'src/[x].ts'", ["/repo/src/[x].ts"]),
        ('cat "$(cat inner.md)"', ["/repo/inner.md"]),
        ("diff <(cat a.ts) b.ts", ["/repo/a.ts", "/repo/b.ts"]),
        ("echo `cat c.md`", ["/repo/c.md"]),
    ],
)
def test_unknown_values(command: str, expected: list[str]) -> None:
    assert paths(command) == expected


# Shell: apply_patch run through the shell -------------------------------------------------------


def test_apply_patch_heredoc() -> None:
    command = (
        "cd apps/web && apply_patch <<'EOF'\n"
        "*** Begin Patch\n*** Update File: src/app.css\n@@\n-a\n+b\n*** End Patch\n"
        "EOF\n"
        "cat src/app.css"
    )

    assert paths(command) == ["/repo/apps/web/src/app.css"]


def test_apply_patch_argument() -> None:
    command = "apply_patch '*** Begin Patch\n*** Add File: a.md\n+x\n*** End Patch'"

    assert paths(command) == ["/repo/a.md"]


def test_apply_patch_inside_bash_lc() -> None:
    patch = "*** Begin Patch\n*** Delete File: x.md\n*** End Patch"
    command = f"bash -lc \"apply_patch <<'EOF'\n{patch}\nEOF\n\""

    assert paths(command) == ["/repo/x.md"]


# Robustness ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "",
        "'unterminated",
        '"unterminated $(',
        "cat <<EOF",
        "$((1 + (2)",
        "((((((((",
        "))))",
        "`",
        "<(",
        "bash -c",
        "git -C",
        "\\",
        "cd a && " * 200 + "cat b",
        "$(" * 100 + "cat a.ts",
    ],
)
def test_malformed_commands_do_not_raise(command: str) -> None:
    shell_paths(command, CWD, HOME)


def test_no_home_leaves_tilde_paths_out() -> None:
    assert shell_paths("cat ~/a.md b.md", CWD, None) == ["/repo/b.md"]


# Shell: cases found in review ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('grep -c "^$" a.ts', ["a.ts"]),
        ('rg -n "def main$" src/a.py && cat b.ts', ["src/a.py", "b.ts"]),
        ('grep "it$\'s" a.ts; cat b.ts', ["a.ts", "b.ts"]),
        ("git commit -m \"$(cat <<'EOF'\nfix: don't crash\nEOF\n)\" && cat a.ts", ["a.ts"]),
        ("echo $(echo x # it's\n) ; cat b.ts", ["b.ts"]),
        ("echo $(cat a.ts", ["a.ts"]),
        ('cd sub && echo "$(cat a.ts)"', ["sub/a.ts"]),
        ("pushd sub && cat a && popd && cat b", ["sub/a", "b"]),
        ("grep -nC 3 foo a.ts", ["a.ts"]),
        ("rg -nt ts foo a.ts", ["a.ts"]),
        ("head -qn 5 a.ts", ["a.ts"]),
        ("tail -fn 50 app.log", ["app.log"]),
        ("grep --include=*.ts -rn foo src/a.ts", ["src/a.ts"]),
        ("rg --dfa-size-limit 1G foo a.ts", ["a.ts"]),
        ("sed -ne 's/x/y/p' a.ts", ["a.ts"]),
        ("grep -rne foo a.ts", ["a.ts"]),
        ("cat src/{a,b}.ts a{1..3}.ts c{d}.ts", ["c{d}.ts"]),
        ("perl -pi -e 's/a/b/' src/a.ts", ["src/a.ts"]),
        ("perl -0pi -e 's/a/b/' src/a.ts", ["src/a.ts"]),
        ("perl script.pl data.txt", []),
        ("/usr/bin/env bash -lc 'cat a.ts'", ["a.ts"]),
        ("command grep -v foo a.ts", ["a.ts"]),
        ("env LC_ALL=C grep -C 2 foo a.ts", ["a.ts"]),
        ("bash -euo pipefail -c 'cat a.ts'", ["a.ts"]),
        ("bash <<'EOF'\ncat a.ts\nEOF", ["a.ts"]),
        ("sh -s <<'EOF'\ncat a.ts\nEOF", ["a.ts"]),
        ("(( n > 10 ))", []),
        ("for ((i=0; i<3; i++)); do cat a.ts; done", ["a.ts"]),
        ("[[ $a > b ]] && cat c.ts", ["c.ts"]),
        ("(( x = 1 << 2 ))\ncat a.ts", ["a.ts"]),
        ("less +G a.log", ["a.log"]),
        ("awk '{print}' FS=, a.csv", ["a.csv"]),
        ("gawk -i inplace '{print}' a.csv", ["a.csv"]),
        ("jq . package.json", ["package.json"]),
        ("jq -r --arg name x '.a' f.json", ["f.json"]),
        ("jq -n '1'", []),
        ("diff -u a.ts b.ts", ["a.ts", "b.ts"]),
    ],
)
def test_review_cases(command: str, expected: list[str]) -> None:
    assert paths(command) == [f"/repo/{path}" for path in expected]


def test_git_reads_existing_files_without_a_separator(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("")
    (tmp_path / "README.md").write_text("")
    cwd = str(tmp_path / "src")

    assert shell_paths("git diff main a.ts", cwd, HOME) == [f"{tmp_path}/src/a.ts"]
    assert shell_paths("git log -p --follow a.ts missing.ts", cwd, HOME) == [f"{tmp_path}/src/a.ts"]
    assert shell_paths("git show HEAD:README.md", cwd, HOME) == [f"{tmp_path}/README.md"]
    assert shell_paths("git show HEAD:./a.ts", cwd, HOME) == [f"{tmp_path}/src/a.ts"]
    assert shell_paths("git show HEAD", cwd, HOME) == []


def test_patch_headers_inside_an_update_section_are_only_right_trimmed() -> None:
    patch = (
        "*** Begin Patch\n*** Update File: a.ts\n@@\n *** End Patch\n *** Update File: fake.ts\n"
        "-x\n+y\n*** Update File: b.ts\n*** Move to: c.ts\n@@\n-x\n+y\n*** End Patch"
    )

    assert patch_paths(patch) == ["a.ts", "b.ts", "c.ts"]


def test_patch_lines_split_only_at_line_feeds() -> None:
    assert patch_paths("*** Begin Patch\n*** Add File: a\x0cb.md\n+x\n*** End Patch") == [
        "a\x0cb.md"
    ]


def test_directory_path_fields_are_not_trigger_paths(tmp_path: Path) -> None:
    (tmp_path / "images").mkdir()

    assert touched_paths("view_image", {"path": "images"}, str(tmp_path), HOME) == []


@pytest.mark.parametrize(
    "command",
    [
        pytest.param('"' + '$"' * 2000, id="dollar-quotes"),
        pytest.param("cd a; " * 60_000, id="long-cd-chain"),
        pytest.param("cat a\n" * 300_000, id="many-commands"),
        pytest.param("cd a\x00b && cat c.ts", id="nul"),
        pytest.param("$(" * 3000 + "cat a.ts", id="nested-substitutions"),
        pytest.param("`" + "\\`" * 5000, id="escaped-backquotes"),
    ],
)
def test_large_or_odd_commands_stay_fast(command: str) -> None:
    started = time.monotonic()

    shell_paths(command, CWD, HOME)

    assert time.monotonic() - started < 3
