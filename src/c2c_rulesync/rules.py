"""Find the rules Claude Code 2.1.273 would load, and in what order.

Claude Code loads rules at two moments:

- At session start, rules without globs from the user rules directory and from
  the ``.claude/rules`` of the working directory and each of its ancestors.
- When a file is read, rules whose globs match it from the same directories,
  plus every rule, with or without globs, of the ``.claude/rules`` directories
  strictly between the working directory and the file.

``RuleFinder`` reproduces both, and ``SessionRules`` keeps a session from
loading a rule twice. Behavior and its evidence are documented in
``docs/behavior.md``.
"""

from __future__ import annotations

import os
import stat

from c2c_rulesync.frontmatter import js_trim, parse_rule_text
from c2c_rulesync.ignore import Matcher

__all__ = ["PROJECT", "USER", "Rule", "RuleFinder", "SessionRules"]

USER = "user"
PROJECT = "project"

# Claude Code skips a memory file larger than this.
MAX_RULE_BYTES = 4 * 1024 * 1024
_RULES_DIR = (".claude", "rules")


class Rule:
    """One rule file as Claude Code would load it.

    Attributes:
        path: The rule file's resolved absolute path, which identifies it.
        source: ``USER`` or ``PROJECT``.
        globs: The rule's globs, or ``None`` for an unconditional rule.
        body: The text to inject.
        warnings: Notes about how the file was interpreted.
    """

    __slots__ = ("body", "globs", "path", "source", "warnings")

    def __init__(
        self,
        path: str,
        source: str,
        globs: tuple[str, ...] | None,
        body: str,
        warnings: tuple[str, ...],
    ) -> None:
        self.path = path
        self.source = source
        self.globs = globs
        self.body = body
        self.warnings = warnings

    def __repr__(self) -> str:
        return f"Rule({self.path!r}, source={self.source!r}, globs={self.globs!r})"


class RuleFinder:
    """Discover rules for one working directory.

    Args:
        cwd: The session's working directory. It is resolved, as Claude Code
            resolves its own working directory.
        user_rules_dir: The user rules directory, or ``None`` to skip user rules.
    """

    def __init__(self, cwd: str, user_rules_dir: str | None) -> None:
        self.cwd = os.path.realpath(cwd)
        self.user_rules_dir = user_rules_dir
        self.warnings: list[str] = []
        self._rule_cache: dict[str, Rule | None] = {}
        self._worktree = _nested_worktree(self.cwd)

    # Public API -------------------------------------------------------------

    def session_start_rules(self) -> list[Rule]:
        """Rules without globs that load when a session starts, in load order."""
        processed: set[str] = set()
        rules: list[Rule] = []
        if self.user_rules_dir is not None:
            rules += self._walk(self.user_rules_dir, USER, processed, conditional=False)
        for directory in self._cwd_level_dirs():
            rules_dir = os.path.join(directory, *_RULES_DIR)
            rules += self._walk(rules_dir, PROJECT, processed, conditional=False)
        return rules

    def trigger_rules(self, file_path: str) -> list[Rule]:
        """Rules that load when ``file_path`` is read, in load order.

        ``file_path`` is resolved against the working directory. Files outside
        it load nothing.
        """
        target = os.path.normpath(os.path.join(self.cwd, file_path))
        if not self._is_readable_target(target):
            return []
        processed: set[str] = set()
        rules: list[Rule] = []
        if self.user_rules_dir is not None:
            rules += self._matching(target, self.user_rules_dir, USER, processed)
        for directory in self._nested_dirs(target):
            rules_dir = os.path.join(directory, *_RULES_DIR)
            # Claude Code walks a nested directory twice: once for rules without
            # globs, with its own record of processed files, and once for rules
            # whose globs match.
            unconditional_processed = set(processed)
            rules += self._walk(rules_dir, PROJECT, unconditional_processed, conditional=False)
            rules += self._matching(target, rules_dir, PROJECT, processed)
            processed |= unconditional_processed
        for directory in self._cwd_level_dirs():
            rules_dir = os.path.join(directory, *_RULES_DIR)
            rules += self._matching(target, rules_dir, PROJECT, processed)
        return rules

    # Directories --------------------------------------------------------------

    def _cwd_level_dirs(self) -> list[str]:
        """The working directory and its ancestors, filesystem root excluded, outermost first."""
        directories = []
        directory = self.cwd
        while directory != os.path.dirname(directory):
            directories.append(directory)
            directory = os.path.dirname(directory)
        directories.reverse()
        return [d for d in directories if not self._skipped_by_worktree(d)]

    def _nested_dirs(self, target: str) -> list[str]:
        """Directories between the working directory and ``target``, outermost first."""
        parent = os.path.dirname(target)
        if not parent.startswith(self.cwd):
            resolved = os.path.realpath(parent)
            if resolved.startswith(self.cwd):
                parent = resolved
        directories = []
        directory = parent
        while directory not in (self.cwd, os.path.dirname(directory)):
            # A plain string prefix, as Claude Code checks it: with a working
            # directory /a/b, a file under /a/bc counts as nested.
            if directory.startswith(self.cwd):
                directories.append(directory)
            directory = os.path.dirname(directory)
        directories.reverse()
        return directories

    def _skipped_by_worktree(self, directory: str) -> bool:
        if self._worktree is None:
            return False
        worktree_root, main_root = self._worktree
        return _is_within(directory, main_root) and not _is_within(directory, worktree_root)

    def _is_readable_target(self, target: str) -> bool:
        return _is_within(target, self.cwd) or _is_within(os.path.realpath(target), self.cwd)

    # Walking rules directories -----------------------------------------------

    def _matching(
        self, target: str, rules_dir: str, source: str, processed: set[str]
    ) -> list[Rule]:
        rules = self._walk(rules_dir, source, processed, conditional=True)
        base = os.path.dirname(os.path.dirname(rules_dir)) if source == PROJECT else self.cwd
        relative = _relative(base, target)
        if not relative or relative.startswith("..") or os.path.isabs(relative):
            parent = os.path.dirname(target)
            resolved_parent = os.path.realpath(parent)
            if resolved_parent != parent:
                relative = _relative(base, os.path.join(resolved_parent, os.path.basename(target)))
        if not relative or relative.startswith("..") or os.path.isabs(relative):
            return []
        return [rule for rule in rules if rule.globs and Matcher(rule.globs).ignores(relative)]

    def _walk(
        self,
        rules_dir: str,
        source: str,
        processed: set[str],
        *,
        conditional: bool,
        visited: set[str] | None = None,
    ) -> list[Rule]:
        """Load the rules under ``rules_dir`` that have globs, or that have none.

        Links are followed. For project rules, a rules directory that is itself
        a link must resolve inside the working directory, and so must an entry
        that resolves somewhere other than its own place in the directory. As
        in Claude Code, entries are not checked when ``rules_dir`` is reached
        through a linked parent, such as a linked ``.claude``.
        """
        visited = set() if visited is None else visited
        if rules_dir in visited:
            return []
        resolved_dir = os.path.realpath(rules_dir)
        is_link = os.path.islink(rules_dir)
        visited.add(rules_dir)
        if is_link:
            visited.add(resolved_dir)
        include_external = source == USER
        if not include_external and is_link and not _is_within(resolved_dir, self.cwd):
            return []
        try:
            names = sorted(os.listdir(resolved_dir))
        except (FileNotFoundError, NotADirectoryError):
            return []
        except OSError as error:
            self.warnings.append(
                f"{rules_dir}: {error.strerror or error}; its rules are not loaded"
            )
            return []
        canonical = resolved_dir == rules_dir
        found: list[Rule] = []
        for name in names:
            entry = os.path.join(rules_dir, name)
            resolved = os.path.realpath(entry)
            try:
                mode = os.stat(entry).st_mode
            except OSError:
                if os.path.islink(entry):
                    self.warnings.append(f"{entry}: a link that cannot be followed; skipped")
                continue
            points_elsewhere = canonical and resolved != os.path.join(resolved_dir, name)
            if points_elsewhere and not include_external and not _is_within(resolved, self.cwd):
                continue
            if stat.S_ISDIR(mode):
                found += self._walk(
                    resolved, source, processed, conditional=conditional, visited=visited
                )
            elif stat.S_ISREG(mode) and name.endswith(".md"):
                rule = self._load(resolved, source, processed)
                if rule is not None and (rule.globs is not None) == conditional:
                    found.append(rule)
        return found

    def _load(self, path: str, source: str, processed: set[str]) -> Rule | None:
        normalized = os.path.normpath(path)
        if normalized in processed:
            return None
        resolved = os.path.realpath(path)
        if resolved != normalized:
            if resolved in processed:
                return None
            processed.add(resolved)
        processed.add(normalized)
        rule = self._read_rule(normalized, source)
        if rule is None or js_trim(rule.body) == "":
            return None
        return rule

    def _read_rule(self, path: str, source: str) -> Rule | None:
        key = f"{source}\0{path}"
        if key in self._rule_cache:
            return self._rule_cache[key]
        rule = None
        try:
            size = os.path.getsize(path)
            if size > MAX_RULE_BYTES:
                self.warnings.append(f"{path}: larger than 4 MiB; Claude Code skips it")
            else:
                with open(path, "rb") as handle:
                    data = handle.read(MAX_RULE_BYTES + 1)
                text = data.decode("utf-8", errors="replace")
                parsed = parse_rule_text(text)
                warnings = parsed.warnings
                if "\U0000fffd" in text and b"\xef\xbf\xbd" not in data:
                    warnings += ("not valid UTF-8; invalid bytes were replaced",)
                rule = Rule(path, source, parsed.globs, parsed.body, warnings)
                self.warnings.extend(f"{path}: {warning}" for warning in warnings)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            rule = None
        except OSError as error:
            self.warnings.append(f"{path}: {error.strerror or error}; the rule is not loaded")
        self._rule_cache[key] = rule
        return rule


class SessionRules:
    """The rule files one session has loaded, so that each loads at most once.

    Args:
        loaded: Resolved paths of files the session already loaded, for example
            from saved hook state. The set is updated in place.
    """

    def __init__(self, loaded: set[str] | None = None) -> None:
        self.loaded = set() if loaded is None else loaded

    def take(self, rules: list[Rule]) -> list[Rule]:
        """Return the rules not loaded yet, in order, and record them as loaded."""
        fresh = []
        for rule in rules:
            if rule.path not in self.loaded:
                self.loaded.add(rule.path)
                fresh.append(rule)
        return fresh

    def mark_read(self, file_path: str) -> None:
        """Record that the file at the absolute ``file_path`` was read.

        Reading a rule file counts as loading it: its content is already in the
        conversation, so a later match does not load it again.
        """
        self.loaded.add(os.path.realpath(file_path))


def _is_within(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


def _relative(base: str, path: str) -> str:
    relative = os.path.relpath(path, base)
    return "" if relative == "." else relative.replace(os.sep, "/")


def _nested_worktree(cwd: str) -> tuple[str, str] | None:
    """Find a git worktree nested inside its own main repository.

    Returns (worktree root, main repository root) when ``cwd`` is inside such a
    worktree, else ``None``.
    """
    directory = cwd
    while True:
        dot_git = os.path.join(directory, ".git")
        if os.path.lexists(dot_git):
            break
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent
    if not os.path.isfile(dot_git):
        return None
    try:
        with open(dot_git, encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    git_dir = os.path.normpath(os.path.join(directory, content[len("gitdir:") :].strip()))
    try:
        with open(os.path.join(git_dir, "commondir"), encoding="utf-8") as handle:
            common = os.path.normpath(os.path.join(git_dir, handle.read().strip()))
    except OSError:
        return None
    if os.path.basename(common) != ".git":
        return None
    main_root = os.path.realpath(os.path.dirname(common))
    worktree_root = os.path.realpath(directory)
    if main_root == worktree_root or not _is_within(worktree_root, main_root):
        return None
    return worktree_root, main_root
