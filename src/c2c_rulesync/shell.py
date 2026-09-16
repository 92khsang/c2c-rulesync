"""Find the files a shell command reads or writes, on a best-effort basis.

Codex runs shell commands as one string in the ``Bash`` tool. This module
reads that string the way a POSIX shell would split it, closely enough to
recognize common file-reading commands (``cat``, ``sed``, ``grep`` and similar),
redirections, ``cd``, ``bash -c`` wrappers and ``apply_patch`` heredocs. It
never runs anything and never expands globs, variables or command output: a
word it cannot know is left out.
"""

from __future__ import annotations

import os
import re

from c2c_rulesync.patch import patch_paths

__all__ = ["shell_paths"]

# Nested `bash -c`, `$(...)` and subshells beyond this depth are not examined.
_MAX_DEPTH = 16

_OPERATORS = ("&&", "||", "|&", ";;", ";&", "|", "&", ";", "(", ")")
_REDIRECTIONS = ("<<<", "<<-", "<<", "<>", "<&", ">&", ">>", ">|", "&>>", "&>", "<", ">")
_WORD_BREAK = frozenset(" \t\n|&;()<>")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=")


class _Word:
    """A shell word after quote removal.

    ``known`` is false when the word depends on something only the shell knows
    at run time: a variable, command output, an unexpandable ``~user``, or an
    unquoted glob character.
    """

    __slots__ = ("known", "text")

    def __init__(self, text: str, known: bool) -> None:
        self.text = text
        self.known = known


class _Command:
    """A simple command: its words, redirections and here-document bodies."""

    __slots__ = ("heredocs", "redirections", "separator", "words")

    def __init__(self) -> None:
        self.words: list[_Word] = []
        self.redirections: list[tuple[str, _Word]] = []
        self.heredocs: list[str] = []
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
        Normalized absolute paths without duplicates, in the order they appear.
        Directories that exist are left out.
    """
    finder = _PathFinder(home)
    finder.script(command, cwd, 0)
    return list(dict.fromkeys(finder.paths))


class _PathFinder:
    def __init__(self, home: str | None) -> None:
        self.home = home
        self.paths: list[str] = []

    # Scripts ------------------------------------------------------------------

    def script(self, text: str, cwd: str | None, depth: int) -> None:
        if depth > _MAX_DEPTH:
            return
        lexer = _Lexer(text, self.home)
        commands = lexer.commands()
        for substitution in lexer.substitutions:
            self.script(substitution, cwd, depth + 1)
        saved: list[str | None] = []
        for command in commands:
            next_cwd = self.command(command, cwd, depth)
            if command.separator == "(":
                saved.append(cwd)
            elif command.separator == ")":
                cwd = saved.pop() if saved else cwd
            elif command.separator in (";", "&&", "||", "\n", ""):
                cwd = next_cwd

    def command(self, command: _Command, cwd: str | None, depth: int) -> str | None:
        """Record the paths of one simple command and return the directory after it."""
        for operator, target in command.redirections:
            if operator in ("<&", ">&") and (target.text.isdigit() or target.text == "-"):
                continue
            self.add(target, cwd, allow_directory=True)
        words = _skip_prefixes(command.words)
        if not words or not words[0].known:
            return cwd
        name = os.path.basename(words[0].text)
        args = words[1:]
        if name in ("cd", "pushd"):
            return self._cd(args, cwd)
        if name == "popd":
            return None
        if name in _SHELLS:
            script = _shell_script(args)
            if script is not None:
                self.script(script, cwd, depth + 1)
            return cwd
        if name in ("apply_patch", "applypatch"):
            self._apply_patch(args, command.heredocs, cwd)
        elif name in _READERS:
            self.add_all(_operands(args, _READERS[name]), cwd)
        elif name in ("sed", "gsed"):
            self.add_all(_script_operands(args, _SED_VALUES, _SED_SCRIPT_OPTIONS, bsd_i=True), cwd)
        elif name in ("awk", "gawk", "mawk", "nawk"):
            self.add_all(_script_operands(args, _AWK_VALUES, _AWK_SCRIPT_OPTIONS), cwd)
        elif name in ("grep", "egrep", "fgrep"):
            self.add_all(_script_operands(args, _GREP_VALUES, _GREP_SCRIPT_OPTIONS), cwd)
        elif name == "rg":
            if any(arg.text == "--files" for arg in args):
                self.add_all(_operands(args, _RG_VALUES), cwd)
            else:
                self.add_all(_script_operands(args, _RG_VALUES, _RG_SCRIPT_OPTIONS), cwd)
        elif name == "git":
            self._git(args, cwd)
        return cwd

    # Commands -----------------------------------------------------------------

    def _cd(self, args: list[_Word], cwd: str | None) -> str | None:
        operands = _operands(args, frozenset())
        if len(operands) != 1 or not operands[0].known or operands[0].text in ("", "-"):
            return None
        target = operands[0].text
        if os.path.isabs(target):
            return os.path.normpath(target)
        return None if cwd is None else os.path.normpath(os.path.join(cwd, target))

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
                    cwd = self._cd([args[index + 1]], cwd)
                index += 1
            index += 1
        if index >= len(args):
            return
        subcommand = args[index].text
        rest = args[index + 1 :]
        if subcommand not in ("diff", "show", "log", "blame"):
            return
        texts = [arg.text for arg in rest]
        if "--" in texts:
            self.add_all(rest[texts.index("--") + 1 :], cwd)
        elif subcommand == "blame":
            operands = _operands(rest, _GIT_BLAME_VALUES)
            if operands:
                self.add_all(operands[-1:], cwd)

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
        if path.startswith("/dev/") or path == "/dev":
            return
        if not allow_directory and os.path.isdir(path):
            return
        self.paths.append(path)


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
}
_SED_VALUES = _options("-e -f -l --expression --file --line-length")
_SED_SCRIPT_OPTIONS = ("-e", "-f", "--expression", "--file")
_AWK_VALUES = _options("-f -v -F --file --assign --field-separator")
_AWK_SCRIPT_OPTIONS = ("-f", "--file")
_GREP_VALUES = _options(
    "-e -f -m -A -B -C -d -D --regexp --file --max-count --after-context --before-context"
    " --context --directories --devices --label --include --exclude --exclude-dir"
    " --exclude-from --color --colour --binary-files --group-separator"
)
_GREP_SCRIPT_OPTIONS = ("-e", "-f", "--regexp", "--file")
_RG_VALUES = _options(
    "-e --regexp -f --file -g --glob --iglob -t --type -T --type-not --type-add --type-clear"
    " -m --max-count -A -B -C --after-context --before-context --context -M --max-columns"
    " --max-depth -d --maxdepth -j --threads -r --replace -E --encoding --color --colors"
    " --sort --sortr --ignore-file --pre --pre-glob --path-separator --context-separator"
    " --field-context-separator --field-match-separator --max-filesize --engine"
    " --hyperlink-format"
)
_RG_SCRIPT_OPTIONS = ("-e", "--regexp", "-f", "--file")
_GIT_BLAME_VALUES = _options("-L -S --contents --date --ignore-rev --ignore-revs-file")

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
    "env": _options("-u --unset"),
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
        if not word.known or text not in _PREFIX_COMMANDS:
            break
        following = {w.text.split("=")[0] for w in words[index + 1 : index + 4]}
        if text == "command" and following & {"-v", "-V"}:
            return []
        # `env -C DIR` and `env -S STRING` change what runs where.
        if text == "env" and following & {"-C", "--chdir", "-S", "--split-string"}:
            return []
        values = _PREFIX_COMMANDS[text]
        index += 1
        while index < len(words) and words[index].text.startswith("-") and words[index].text != "-":
            if words[index].text == "--":
                index += 1
                break
            if words[index].text in values:
                index += 1
            index += 1
        if text == "timeout" and index < len(words):
            index += 1
        if text == "env":
            while index < len(words) and _ASSIGNMENT.match(words[index].text):
                index += 1
    return words[index:]


def _shell_script(args: list[_Word]) -> str | None:
    """The script of ``sh -c SCRIPT``, or ``None`` when the shell runs a file or stdin."""
    index = 0
    has_c = False
    while index < len(args):
        text = args[index].text
        if text in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            index += 2
            continue
        if text == "--":
            index += 1
            break
        if text.startswith("--"):
            index += 1
            continue
        if len(text) < 2 or text[0] not in "-+":
            break
        has_c = has_c or "c" in text[1:]
        index += 1
    if not has_c or index >= len(args) or not args[index].known:
        return None
    return args[index].text


def _operands(args: list[_Word], values: frozenset[str]) -> list[_Word]:
    operands = []
    index = 0
    while index < len(args):
        text = args[index].text
        if text == "--":
            operands += args[index + 1 :]
            break
        if text.startswith("-") and text != "-" and args[index].known:
            if "=" not in text and text in values:
                index += 1
            index += 1
            continue
        operands.append(args[index])
        index += 1
    return operands


def _script_operands(
    args: list[_Word], values: frozenset[str], script_options: tuple[str, ...], bsd_i: bool = False
) -> list[_Word]:
    """Operands of a command whose first operand is a script or pattern.

    When the script is given by an option such as ``-e``, every operand is a
    file.
    """
    has_script_option = False
    kept = []
    for index, arg in enumerate(args):
        text = arg.text
        if any(text == option or text.startswith(option + "=") for option in script_options):
            has_script_option = True
        elif text.startswith(("-e", "-f")) and len(text) > 2 and not text.startswith("--"):
            has_script_option = has_script_option or text[:2] in script_options
        # BSD sed on macOS spells an in-place edit without backup `-i ''`.
        if bsd_i and index > 0 and args[index - 1].text == "-i" and text == "":
            continue
        kept.append(arg)
    operands = _operands(kept, values)
    return operands if has_script_option else operands[1:]


# Lexer ----------------------------------------------------------------------------


class _Lexer:
    """Split a script into simple commands.

    ``substitutions`` collects the scripts of ``$(...)``, backquotes and
    process substitutions, which run as commands of their own.
    """

    def __init__(self, text: str, home: str | None) -> None:
        self.text = text
        self.home = home
        self.index = 0
        self.substitutions: list[str] = []
        self._pending_heredocs: list[tuple[str, bool, _Command]] = []

    def commands(self) -> list[_Command]:
        commands: list[_Command] = []
        current = _Command()
        text = self.text
        length = len(text)
        while self.index < length:
            char = text[self.index]
            if char == "\n":
                self.index += 1
                self._read_heredocs()
                current = self._end(commands, current, "\n")
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
            redirection = self._redirection()
            if redirection is not None:
                self._redirect(current, redirection)
                continue
            operator = next((op for op in _OPERATORS if text.startswith(op, self.index)), None)
            if operator is not None:
                self.index += len(operator)
                current = self._end(commands, current, operator)
                continue
            current.words.append(self._word())
        self._end(commands, current, "")
        return commands

    def _end(self, commands: list[_Command], current: _Command, separator: str) -> _Command:
        if current.words or current.redirections or separator in ("(", ")"):
            current.separator = separator
            commands.append(current)
            return _Command()
        return current

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

    def _redirect(self, command: _Command, operator: str) -> None:
        self._skip_blanks()
        if self.index >= len(self.text) or self.text[self.index] in "\n|&;()<>":
            return
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
        while self.index < length:
            char = text[self.index]
            if char in _WORD_BREAK:
                if char in "<>" and text.startswith("(", self.index + 1):
                    self._substitution(self.index + 2, ")")
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
                known = self._dollar(parts) and known
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
                known = self._dollar(parts) and known
            elif char == "`":
                self._backquote()
                known = False
            else:
                parts.append(char)
                self.index += 1
        return known

    def _dollar(self, parts: list[str]) -> bool:
        """Consume a ``$`` expansion and return whether its value is known."""
        text = self.text
        following = text[self.index + 1 : self.index + 2]
        if text.startswith("$((", self.index):
            self.index = _matching_close(text, self.index + 3, "))")
        elif following == "(":
            self._substitution(self.index + 2, ")")
        elif following == "{":
            self.index = _matching_close(text, self.index + 2, "}")
        elif following == "'":
            self.index = _ansi_c_quoted(text, self.index + 2, parts)
            return True
        elif following == '"':
            self.index += 1
            return self._double_quoted(parts)
        elif following.isalnum() or following == "_":
            match = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]").match(text, self.index + 1)
            self.index = match.end() if match else self.index + 2
        elif following and following in "@*#?$!-":
            self.index += 2
        else:
            parts.append("$")
            self.index += 1
            return True
        return False

    def _substitution(self, start: int, close: str) -> None:
        end = _matching_close(self.text, start, close)
        self.substitutions.append(self.text[start : end - len(close)])
        self.index = end

    def _backquote(self) -> None:
        text = self.text
        end = self.index + 1
        while end < len(text) and text[end] != "`":
            end += 2 if text[end] == "\\" else 1
        self.substitutions.append(text[self.index + 1 : end])
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
