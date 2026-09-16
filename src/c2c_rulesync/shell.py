"""Find the files a shell command reads or writes, on a best-effort basis.

Codex runs shell commands as one string in the ``Bash`` tool. This module
reads that string the way a POSIX shell would split it, closely enough to
recognize common file-reading commands (``cat``, ``sed``, ``grep`` and similar),
redirections, ``cd``, ``bash -c`` wrappers and ``apply_patch`` heredocs. It
never runs anything and never expands globs, braces, variables or command
output: a word it cannot know is left out.
"""

from __future__ import annotations

import os
import re

from c2c_rulesync.patch import patch_paths

__all__ = ["shell_paths"]

# Nested `bash -c`, `$(...)` and subshells beyond this depth are not examined.
_MAX_DEPTH = 16
# Commands examined per call; a longer script is examined up to this point.
_MAX_COMMANDS = 20_000
# Longer directory names are treated as unknown.
_MAX_PATH = 4096

_OPERATORS = ("&&", "||", "|&", ";;&", ";;", ";&", "|", "&", ";", "(", ")")
_REDIRECTIONS = ("<<<", "<<-", "<<", "<>", "<&", ">&", ">>", ">|", "&>>", "&>", "<", ">")
_WORD_BREAK = frozenset(" \t\n|&;()<>")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]")


class _Word:
    """A shell word after quote removal.

    ``known`` is false when the word depends on something only the shell knows
    at run time: a variable, command output, an unexpandable ``~user``, or an
    unquoted glob or brace expansion.
    """

    __slots__ = ("known", "text")

    def __init__(self, text: str, known: bool) -> None:
        self.text = text
        self.known = known


class _Command:
    """A simple command: its words, redirections, here-documents and substitutions."""

    __slots__ = ("heredocs", "redirections", "separator", "substitutions", "words")

    def __init__(self) -> None:
        self.words: list[_Word] = []
        self.redirections: list[tuple[str, _Word]] = []
        self.heredocs: list[str] = []
        # Commands of the `$(...)`, backquote and `<(...)` substitutions in
        # the command's words, which run before it.
        self.substitutions: list[list[_Command]] = []
        # The operator that ends the command: ";", "&&", "||", "|", "&", "\n",
        # "(" or ")", or "" at the end of the script.
        self.separator = ""


def shell_paths(command: str, cwd: str, home: str | None) -> list[str]:
    """Absolute paths of the files ``command`` reads or writes.

    Args:
        command: The script, as Codex passes it to the shell.
        cwd: The absolute directory the script starts in.
        home: The home directory for ``~``, or ``None`` when unknown.

    Returns:
        Normalized absolute paths without duplicates. Directories that exist
        are left out.
    """
    finder = _PathFinder(home)
    finder.script(command, cwd, 0)
    return finder.paths


class _PathFinder:
    def __init__(self, home: str | None) -> None:
        self.home = home
        self.paths: list[str] = []
        self._seen: set[str] = set()
        self._commands = 0

    # Scripts ------------------------------------------------------------------

    def script(self, text: str, cwd: str | None, depth: int) -> None:
        if depth <= _MAX_DEPTH:
            self.run(_Lexer(text, self.home, depth).commands(), cwd, depth)

    def run(self, commands: list[_Command], cwd: str | None, depth: int) -> None:
        subshells: list[str | None] = []
        directories: list[str | None] = []
        for command in commands:
            self._commands += 1
            if self._commands > _MAX_COMMANDS:
                return
            for substitution in command.substitutions:
                self.run(substitution, cwd, depth + 1)
            next_cwd = self.command(command, cwd, depth, directories)
            if command.separator == "(":
                subshells.append(cwd)
            elif command.separator == ")":
                cwd = subshells.pop() if subshells else cwd
            elif command.separator in (";", "&&", "||", "\n", ""):
                cwd = next_cwd

    def command(
        self, command: _Command, cwd: str | None, depth: int, directories: list[str | None]
    ) -> str | None:
        """Record the paths of one simple command and return the directory after it.

        ``directories`` is the ``pushd`` stack of the script.
        """
        for operator, target in command.redirections:
            if operator in ("<&", ">&") and (target.text.isdigit() or target.text == "-"):
                continue
            self.add(target, cwd, allow_directory=True)
        words = _skip_prefixes(command.words)
        if not words or not words[0].known:
            return cwd
        name = os.path.basename(words[0].text)
        args = words[1:]
        if name == "cd":
            return _cd(args, cwd)
        if name == "pushd":
            directories.append(cwd)
            return _cd(args, cwd)
        if name == "popd":
            return directories.pop() if directories else None
        if name in _SHELLS:
            script = _shell_script(args, command.heredocs)
            if script is not None:
                self.script(script, cwd, depth + 1)
        elif name in ("apply_patch", "applypatch"):
            self._apply_patch(args, command.heredocs, cwd)
        elif name == "git":
            self._git(args, cwd)
        elif name == "jq":
            self.add_all(_jq_files(args), cwd)
        elif name in _READERS:
            operands, _ = _scan(args, _READERS[name])
            if name in ("less", "more"):
                operands = [word for word in operands if not word.text.startswith("+")]
            self.add_all(operands, cwd)
        elif name in _SCRIPTED:
            values, script_options, needs_option = _SCRIPTED[name]
            files = _script_operands(args, values, script_options, needs_option=needs_option)
            if name in ("awk", "gawk", "mawk", "nawk"):
                files = [word for word in files if not _ASSIGNMENT.match(word.text)]
            self.add_all(files, cwd)
        return cwd

    # Commands -----------------------------------------------------------------

    def _apply_patch(self, args: list[_Word], heredocs: list[str], cwd: str | None) -> None:
        if args and args[0].known:
            body = args[0].text
        elif heredocs:
            body = heredocs[0]
        else:
            return
        for path in patch_paths(body):
            self.add(_Word(path, True), cwd, allow_directory=True)

    def _git(self, args: list[_Word], cwd: str | None) -> None:
        index = 0
        while index < len(args) and args[index].text.startswith("-"):
            option = args[index].text
            if option in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path"):
                if index + 1 >= len(args):
                    return
                if option == "-C":
                    cwd = _cd([args[index + 1]], cwd)
                index += 1
            index += 1
        if index >= len(args) or args[index].text not in ("diff", "show", "log", "blame"):
            return
        subcommand = args[index].text
        rest = args[index + 1 :]
        texts = [arg.text for arg in rest]
        if "--" in texts:
            self.add_all(rest[texts.index("--") + 1 :], cwd)
            return
        operands, _ = _scan(rest, _GIT_VALUES[subcommand])
        if subcommand == "blame":
            self.add_all(operands[-1:], cwd)
            return
        for operand in operands:
            if not operand.known or cwd is None:
                continue
            revision, colon, path = operand.text.partition(":")
            if subcommand == "show" and colon and revision and path:
                # `rev:path` names a path from the repository's top level,
                # unless it starts with `./` or `../`.
                base = cwd if path.startswith(("./", "../")) else _repository_root(cwd)
                if base is not None:
                    self.add(_Word(os.path.join(base, path), True), cwd, allow_directory=False)
            elif os.path.isfile(os.path.join(cwd, operand.text)):
                # Without `--`, git accepts a path only when the file exists.
                self.add(operand, cwd, allow_directory=False)

    # Paths ----------------------------------------------------------------------

    def add_all(self, words: list[_Word], cwd: str | None) -> None:
        for word in words:
            self.add(word, cwd, allow_directory=False)

    def add(self, word: _Word, cwd: str | None, *, allow_directory: bool) -> None:
        text = word.text
        if not word.known or text in ("", "-") or "\n" in text or "\0" in text:
            return
        if os.path.isabs(text):
            path = os.path.normpath(text)
        elif cwd is None:
            return
        else:
            path = os.path.normpath(os.path.join(cwd, text))
        if path in self._seen or path.startswith("/dev/") or path == "/dev":
            return
        self._seen.add(path)
        if not allow_directory and os.path.isdir(path):
            return
        self.paths.append(path)


def _cd(args: list[_Word], cwd: str | None) -> str | None:
    """The directory ``cd ARGS`` changes to, or ``None`` when only the shell knows it."""
    operands, _ = _scan(args, frozenset())
    if len(operands) != 1 or not operands[0].known:
        return None
    target = operands[0].text
    if target in ("", "-") or "\0" in target or len(target) > _MAX_PATH:
        return None
    if os.path.isabs(target):
        return os.path.normpath(target)
    if cwd is None or len(cwd) + len(target) > _MAX_PATH:
        return None
    return os.path.normpath(os.path.join(cwd, target))


def _repository_root(cwd: str) -> str | None:
    directory = cwd
    while True:
        if os.path.lexists(os.path.join(directory, ".git")):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


# Command tables -----------------------------------------------------------------

_SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh"))


def _options(names: str) -> frozenset[str]:
    return frozenset(names.split())


# Options that take a separate value, per command. Other options are skipped
# on their own.
_READERS = {
    "cat": _options(""),
    "nl": _options("-b -d -f -h -i -l -n -s -v -w"),
    "less": _options("-b -h -j -k -o -O -p -P -t -T -x -y -z"),
    "more": _options("-n"),
    "head": _options("-n -c --lines --bytes"),
    "tail": _options("-n -c --lines --bytes -s --sleep-interval --pid"),
    "bat": _options(
        "-l --language -H --highlight-line -r --line-range --style --theme -m --map-syntax"
        " --tabs --wrap --color --decorations --paging --pager --file-name --terminal-width"
        " --italic-text"
    ),
    "tee": _options(""),
    "diff": _options(
        "-C -U -F -I -x -X -S -L --label --context --unified --show-function-line"
        " --ignore-matching-lines --exclude --exclude-from --starting-file --horizon-lines"
        " --line-format --color --palette"
    ),
}
# Commands whose first operand is a script or pattern unless an option gives
# it: (options taking a value, options giving the script, whether the command
# reads no files at all without such an option).
_SCRIPTED = {
    "sed": (
        _options("-e -f -l --expression --file --line-length"),
        _options("-e -f --expression --file"),
        False,
    ),
    "awk": (_options("-f -v -F --file --assign --field-separator"), _options("-f --file"), False),
    "gawk": (
        _options("-f -v -F -i -l -E --file --assign --field-separator --include --load --exec"),
        _options("-f -E --file --exec"),
        False,
    ),
    "perl": (_options("-e -E -I -M -m -x"), _options("-e -E"), True),
    "grep": (
        _options(
            "-e -f -m -A -B -C -d -D --regexp --file --max-count --after-context"
            " --before-context --context --directories --devices --label --include --exclude"
            " --exclude-dir --exclude-from --color --colour --binary-files --group-separator"
        ),
        _options("-e -f --regexp --file"),
        False,
    ),
    "rg": (
        _options(
            "-e --regexp -f --file -g --glob --iglob -t --type -T --type-not --type-add"
            " --type-clear -m --max-count -A -B -C --after-context --before-context --context"
            " -M --max-columns --max-depth -d --maxdepth -j --threads -r --replace -E"
            " --encoding --color --colors --sort --sortr --ignore-file --pre --pre-glob"
            " --path-separator --context-separator --field-context-separator"
            " --field-match-separator --max-filesize --engine --hyperlink-format"
            " --dfa-size-limit --regex-size-limit"
        ),
        _options("-e --regexp -f --file --files"),
        False,
    ),
}
_SCRIPTED["gsed"] = _SCRIPTED["sed"]
_SCRIPTED["mawk"] = _SCRIPTED["nawk"] = _SCRIPTED["awk"]
_SCRIPTED["egrep"] = _SCRIPTED["fgrep"] = _SCRIPTED["grep"]
_GIT_VALUES = {
    "diff": _options("-U -M -C -B -l -S -G -O --output --relative"),
    "show": _options("-U -M -C -S -G -O --format --pretty --output"),
    "log": _options(
        "-n -U -M -C -S -G -L --format --pretty --max-count --skip --since --until --author --grep"
    ),
    "blame": _options("-L -S --contents --date --ignore-rev --ignore-revs-file"),
}
_JQ_VALUES = _options("-f --from-file -L --indent")
_JQ_PAIRS = _options("--arg --argjson --slurpfile --rawfile")

# Words that may precede the command itself, with the options of each that
# take a separate value.
_PREFIX_COMMANDS = {
    "command": _options(""),
    "builtin": _options(""),
    "exec": _options("-a"),
    "nohup": _options(""),
    "time": _options(""),
    "nice": _options("-n --adjustment"),
    "timeout": _options("-s --signal -k --kill-after"),
    "stdbuf": _options("-i -o -e --input --output --error"),
    "sudo": _options("-u -g -h -p -C -D -R -T -U --user --group"),
    "env": _options("-u --unset -C --chdir -S --split-string"),
}
_KEYWORDS = _options("! { } if then else elif fi do done while until")


def _skip_prefixes(words: list[_Word]) -> list[_Word]:
    """Drop assignments, keywords and wrapper commands in front of the real command."""
    index = 0
    while index < len(words):
        word = words[index]
        text = word.text
        if _ASSIGNMENT.match(text) or text in _KEYWORDS:
            index += 1
            continue
        name = os.path.basename(text)
        if not word.known or name not in _PREFIX_COMMANDS:
            break
        values = _PREFIX_COMMANDS[name]
        index += 1
        own_options = set()
        while index < len(words) and words[index].text.startswith("-") and words[index].text != "-":
            option = words[index].text
            index += 1
            if option == "--":
                break
            own_options.add(option.split("=")[0])
            if option in values:
                index += 1
        if name == "command" and own_options & {"-v", "-V"}:
            return []
        # `env -C DIR` and `env -S STRING` change what runs where.
        if name == "env" and own_options & {"-C", "--chdir", "-S", "--split-string"}:
            return []
        if name == "timeout" and index < len(words):
            index += 1
        if name == "env":
            while index < len(words) and _ASSIGNMENT.match(words[index].text):
                index += 1
    return words[index:]


def _shell_script(args: list[_Word], heredocs: list[str]) -> str | None:
    """The script a shell runs: ``-c SCRIPT``, or a here-document on its input.

    Returns ``None`` when the shell runs a script file or a script only it knows.
    """
    index = 0
    has_c = False
    while index < len(args):
        text = args[index].text
        if text == "--":
            index += 1
            break
        if text in ("--rcfile", "--init-file"):
            index += 2
            continue
        if text.startswith("--"):
            index += 1
            continue
        if len(text) < 2 or text[0] not in "-+":
            break
        has_c = has_c or "c" in text[1:]
        # `-o NAME` and `-O NAME` take the next word, also at a cluster's end.
        index += 2 if text[-1] in "oO" else 1
    if has_c:
        return args[index].text if index < len(args) and args[index].known else None
    if index >= len(args) and heredocs:
        return heredocs[0]
    return None


def _scan(args: list[_Word], values: frozenset[str]) -> tuple[list[_Word], set[str]]:
    """Split arguments into operands and the options used.

    Options in ``values`` take a value: the rest of a short option cluster
    after them, the text after ``=``, or else the next argument. ``--`` ends
    the options.
    """
    operands = []
    used: set[str] = set()
    index = 0
    while index < len(args):
        text = args[index].text
        index += 1
        if text == "--":
            operands += args[index:]
            break
        if not text.startswith("-") or text == "-":
            operands.append(args[index - 1])
            continue
        if text.startswith("--"):
            name, equals, _ = text.partition("=")
            used.add(name)
            if name in values and not equals:
                index += 1
            continue
        for position, letter in enumerate(text[1:], start=2):
            option = f"-{letter}"
            used.add(option)
            if option in values:
                if position == len(text):
                    index += 1
                break
    return operands, used


def _script_operands(
    args: list[_Word],
    values: frozenset[str],
    script_options: frozenset[str],
    *,
    needs_option: bool = False,
) -> list[_Word]:
    """Operands of a command whose first operand is a script or pattern.

    When an option gives the script, every operand is a file; otherwise the
    first operand is the script, or, with ``needs_option``, a script file to
    run and no operand is read.
    """
    # BSD sed on macOS spells an in-place edit without backup `-i ''`.
    kept = [
        arg
        for index, arg in enumerate(args)
        if not (arg.text == "" and index > 0 and args[index - 1].text == "-i")
    ]
    operands, used = _scan(kept, values)
    if used & script_options:
        return operands
    return [] if needs_option else operands[1:]


def _jq_files(args: list[_Word]) -> list[_Word]:
    operands = []
    from_file = False
    index = 0
    while index < len(args):
        text = args[index].text
        index += 1
        if text in ("--args", "--jsonargs"):
            break
        if text in _JQ_PAIRS:
            index += 2
        elif text.startswith("-") and text != "-":
            from_file = from_file or text in ("-f", "--from-file")
            if text in _JQ_VALUES:
                index += 1
        else:
            operands.append(args[index - 1])
    return operands if from_file else operands[1:]


# Lexer ----------------------------------------------------------------------------


class _Lexer:
    """Split a script into simple commands.

    With ``stop_at_close``, lexing ends at a ``)`` that no ``(`` in the text
    opened, whose index is then ``closed_at``: this finds the end of a
    ``$(...)`` substitution with quotes, comments and here-documents understood.
    """

    def __init__(
        self,
        text: str,
        home: str | None,
        depth: int,
        *,
        start: int = 0,
        stop_at_close: bool = False,
    ) -> None:
        self.text = text
        self.home = home
        self.depth = depth
        self.index = start
        self.stop_at_close = stop_at_close
        self.closed_at: int | None = None
        self._current = _Command()
        self._pending_heredocs: list[tuple[str, bool, _Command]] = []
        self._in_test = False

    def commands(self) -> list[_Command]:
        commands: list[_Command] = []
        text = self.text
        length = len(text)
        parentheses = 0
        while self.index < length:
            char = text[self.index]
            if char == "\n":
                self.index += 1
                self._read_heredocs()
                self._end(commands, "\n")
                continue
            if char in " \t":
                self.index += 1
                continue
            if text.startswith("\\\n", self.index):
                self.index += 2
                continue
            if char == "#":
                end = text.find("\n", self.index)
                self.index = length if end < 0 else end
                continue
            if self._in_test and char in "<>":
                # Inside `[[ ]]`, `<` and `>` compare strings.
                self._current.words.append(_Word(char, True))
                self.index += 1
                continue
            if text.startswith("((", self.index):
                # Arithmetic, where `<` and `>` compare numbers.
                self.index = _matching_close(text, self.index + 2, "))")
                self._current.words.append(_Word("((", False))
                continue
            redirection = self._redirection()
            if redirection is not None:
                self._redirect(redirection)
                continue
            operator = next((op for op in _OPERATORS if text.startswith(op, self.index)), None)
            if operator is not None:
                if operator == ")" and self.stop_at_close and parentheses == 0:
                    self.closed_at = self.index
                    break
                parentheses += {"(": 1, ")": -1}.get(operator, 0)
                self.index += len(operator)
                self._end(commands, operator)
                continue
            word = self._word()
            self._current.words.append(word)
            if word.text in ("[[", "]]"):
                self._in_test = word.text == "[["
        self._end(commands, "")
        return commands

    def _end(self, commands: list[_Command], separator: str) -> None:
        current = self._current
        if (
            current.words
            or current.redirections
            or current.substitutions
            or separator in ("(", ")")
        ):
            current.separator = separator
            commands.append(current)
            self._current = _Command()
        self._in_test = False

    def _redirection(self) -> str | None:
        text = self.text
        index = self.index
        while index < len(text) and text[index].isdigit():
            index += 1
        if index > self.index and not text.startswith(("<", ">"), index):
            return None
        for operator in _REDIRECTIONS:
            if text.startswith(operator, index):
                if operator in ("<", ">") and text.startswith("(", index + 1):
                    return None
                self.index = index + len(operator)
                return operator
        return None

    def _redirect(self, operator: str) -> None:
        self._skip_blanks()
        if self.index >= len(self.text) or self.text[self.index] in "\n|&;()<>":
            return
        command = self._current
        if operator in ("<<", "<<-"):
            start = self.index
            word = self._word()
            raw = self.text[start : self.index]
            quoted = any(char in raw for char in "'\"\\")
            delimiter = word.text if quoted or word.known else raw
            self._pending_heredocs.append((delimiter, operator == "<<-", command))
            return
        word = self._word()
        if operator != "<<<":
            command.redirections.append((operator, word))

    def _read_heredocs(self) -> None:
        text = self.text
        for delimiter, strip_tabs, command in self._pending_heredocs:
            lines = []
            while self.index < len(text):
                end = text.find("\n", self.index)
                end = len(text) if end < 0 else end
                line = text[self.index : end]
                self.index = min(end + 1, len(text))
                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                    break
                lines.append(line)
            command.heredocs.append("\n".join(lines) + "\n")
        self._pending_heredocs = []

    def _skip_blanks(self) -> None:
        while self.index < len(self.text) and self.text[self.index] in " \t":
            self.index += 1

    # Words ------------------------------------------------------------------------

    def _word(self) -> _Word:
        text = self.text
        length = len(text)
        parts: list[str] = []
        known = True
        start = self.index
        brace = ""
        while self.index < length:
            char = text[self.index]
            if char in _WORD_BREAK:
                if char in "<>" and text.startswith("(", self.index + 1):
                    self._substitution(self.index + 2)
                    known = False
                    continue
                break
            if char == "'":
                end = text.find("'", self.index + 1)
                end = length if end < 0 else end
                parts.append(text[self.index + 1 : end])
                self.index = end + 1
            elif char == '"':
                known = self._double_quoted(parts) and known
            elif char == "\\":
                # A backslash before a line break joins the lines.
                if text[self.index + 1 : self.index + 2] != "\n":
                    parts.append(text[self.index + 1 : self.index + 2])
                self.index += 2
            elif char == "$":
                known = self._dollar(parts, quoted=False) and known
            elif char == "`":
                self._backquote()
                known = False
            elif char in "*?[":
                parts.append(char)
                known = False
                self.index += 1
            elif char == "~" and self.index == start:
                known = self._tilde(parts) and known
            else:
                # Unquoted `{a,b}` and `{1..3}` expand to several words.
                if char == "{":
                    brace = "{"
                elif brace and (char == "," or text.startswith("..", self.index)):
                    brace = "{,"
                elif char == "}" and brace == "{,":
                    known = False
                parts.append(char)
                self.index += 1
        return _Word("".join(parts), known)

    def _tilde(self, parts: list[str]) -> bool:
        end = self.index + 1
        if end < len(self.text) and self.text[end] not in _WORD_BREAK and self.text[end] != "/":
            parts.append("~")
            self.index += 1
            return False
        self.index += 1
        if self.home is None:
            return False
        parts.append(self.home)
        return True

    def _double_quoted(self, parts: list[str]) -> bool:
        text = self.text
        known = True
        self.index += 1
        while self.index < len(text):
            char = text[self.index]
            if char == '"':
                self.index += 1
                break
            if char == "\\" and text[self.index + 1 : self.index + 2] in (
                '"',
                "\\",
                "$",
                "`",
                "\n",
            ):
                escaped = text[self.index + 1]
                if escaped != "\n":
                    parts.append(escaped)
                self.index += 2
            elif char == "$":
                known = self._dollar(parts, quoted=True) and known
            elif char == "`":
                self._backquote()
                known = False
            else:
                parts.append(char)
                self.index += 1
        return known

    def _dollar(self, parts: list[str], *, quoted: bool) -> bool:
        """Consume a ``$`` expansion and return whether its value is known."""
        text = self.text
        following = text[self.index + 1 : self.index + 2]
        if text.startswith("$((", self.index):
            self.index = _matching_close(text, self.index + 3, "))")
        elif following == "(":
            self._substitution(self.index + 2)
        elif following == "{":
            self.index = _matching_close(text, self.index + 2, "}")
        elif following == "'" and not quoted:
            self.index = _ansi_c_quoted(text, self.index + 2, parts)
            return True
        elif following == '"' and not quoted:
            self.index += 1
            return self._double_quoted(parts)
        elif following.isalnum() or following == "_":
            match = _NAME.match(text, self.index + 1)
            self.index = match.end() if match else self.index + 2
        elif following and following in "@*#?$!-":
            self.index += 2
        else:
            parts.append("$")
            self.index += 1
            return True
        return False

    def _substitution(self, start: int) -> None:
        """Consume a ``$(...)`` or ``<(...)`` whose script starts at ``start``."""
        if self.depth >= _MAX_DEPTH:
            self.index = _matching_close(self.text, start, ")")
            return
        inner = _Lexer(self.text, self.home, self.depth + 1, start=start, stop_at_close=True)
        commands = inner.commands()
        self._current.substitutions.append(commands)
        self.index = len(self.text) if inner.closed_at is None else inner.closed_at + 1

    def _backquote(self) -> None:
        text = self.text
        end = self.index + 1
        while end < len(text) and text[end] != "`":
            end += 2 if text[end] == "\\" else 1
        if self.depth < _MAX_DEPTH:
            script = text[self.index + 1 : end]
            self._current.substitutions.append(_Lexer(script, self.home, self.depth + 1).commands())
        self.index = min(end + 1, len(text))


def _matching_close(text: str, start: int, close: str) -> int:
    """The index after the ``close`` that balances an opening just before ``start``.

    Quotes are skipped; an unbalanced opening extends to the end of ``text``.
    """
    opening = "(" if close[0] == ")" else "{"
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "'":
            end = text.find("'", index + 1)
            index = len(text) if end < 0 else end + 1
            continue
        if char == '"':
            index += 1
            while index < len(text) and text[index] != '"':
                index += 2 if text[index] == "\\" else 1
            index += 1
            continue
        if char == opening:
            depth += 1
        elif depth == 0 and text.startswith(close, index):
            return index + len(close)
        elif char == close[0]:
            depth -= 1
        index += 1
    return len(text)


_ANSI_C_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "a": "\a"}


def _ansi_c_quoted(text: str, start: int, parts: list[str]) -> int:
    index = start
    while index < len(text) and text[index] != "'":
        if text[index] == "\\" and index + 1 < len(text):
            parts.append(_ANSI_C_ESCAPES.get(text[index + 1], text[index + 1]))
            index += 2
        else:
            parts.append(text[index])
            index += 1
    return index + 1
