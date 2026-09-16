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
# Rules directories nested deeper than this are not walked.
MAX_DIRECTORY_DEPTH = 256
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
            resolves its own working directory; the spelling given also counts
            as the working directory for files read through it, as Claude Code
            treats ``$PWD``.
        user_rules_dir: The user rules directory, or ``None`` to skip user rules.
    """

    def __init__(self, cwd: str, user_rules_dir: str | None) -> None:
        self.cwd = os.path.realpath(cwd)
        spelling = os.path.normpath(os.path.abspath(cwd))
        self._working_dirs = [self.cwd] if spelling == self.cwd else [self.cwd, spelling]
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

        A relative ``file_path`` is taken from the working directory. The path,
        every link it passes through as a file, and its resolved path must all
        lie inside the working directory; otherwise nothing loads.
        """
        if "\0" in file_path:
            return []
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
        if not _is_within(parent, self.cwd):
            parent = _strict_realpath(parent)
        if not _is_within(parent, self.cwd):
            return []
        directories = []
        while parent != self.cwd:
            directories.append(parent)
            parent = os.path.dirname(parent)
        directories.reverse()
        return directories

    def _skipped_by_worktree(self, directory: str) -> bool:
        if self._worktree is None:
            return False
        worktree_root, main_root = self._worktree
        return _is_within_folded(directory, main_root) and not _is_within_folded(
            directory, worktree_root
        )

    def _is_readable_target(self, target: str) -> bool:
        return all(
            any(_is_within(form, directory) for directory in self._working_dirs)
            for form in _link_forms(target)
        )

    # Walking rules directories -----------------------------------------------

    def _matching(
        self, target: str, rules_dir: str, source: str, processed: set[str]
    ) -> list[Rule]:
        rules = self._walk(rules_dir, source, processed, conditional=True)
        base = os.path.dirname(os.path.dirname(rules_dir)) if source == PROJECT else self.cwd
        relative = _relative(base, target)
        if not relative or relative.startswith("..") or os.path.isabs(relative):
            parent = os.path.dirname(target)
            resolved_parent = _strict_realpath(parent)
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
        depth: int = 0,
    ) -> list[Rule]:
        """Load the rules under ``rules_dir`` that have globs, or that have none.

        Links are followed. For project rules, a rules directory that is itself
        a link must resolve inside the working directory, and so must an entry
        that resolves somewhere other than its own place in the directory.
        These containment checks ignore case, as Claude Code's do.
        """
        visited = set() if visited is None else visited
        if rules_dir in visited:
            return []
        if depth > MAX_DIRECTORY_DEPTH:
            self.warnings.append(f"{rules_dir}: nested too deeply; its rules are not loaded")
            return []
        resolved_dir = os.path.realpath(rules_dir)
        visited.add(rules_dir)
        visited.add(resolved_dir)
        include_external = source == USER
        if (
            not include_external
            and os.path.islink(rules_dir)
            and not _is_within_folded(resolved_dir, self.cwd)
        ):
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
        found: list[Rule] = []
        for name in names:
            if not _is_utf8(name):
                # Claude Code's runtime cannot open such names and skips them.
                continue
            entry = os.path.join(rules_dir, name)
            resolved = os.path.realpath(entry)
            try:
                mode = os.stat(entry).st_mode
            except OSError:
                if os.path.islink(entry):
                    self.warnings.append(f"{entry}: a link that cannot be followed; skipped")
                continue
            points_elsewhere = resolved != os.path.join(resolved_dir, name)
            if (
                points_elsewhere
                and not include_external
                and not _is_within_folded(resolved, self.cwd)
            ):
                continue
            if stat.S_ISDIR(mode):
                found += self._walk(
                    resolved,
                    source,
                    processed,
                    conditional=conditional,
                    visited=visited,
                    depth=depth + 1,
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
        loaded: Paths the session already loaded or read, for example from
            saved hook state. The set is updated in place.
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
        """Record that the file at the absolute ``file_path`` was read or written.

        A rule whose resolved path was read counts as loaded: its content is
        already in the conversation. The path is recorded as read, not
        resolved, so reading a rule through a link does not count. Call this
        before ``take`` for the rules the same read loads, as Claude Code
        records a read before it loads the rules the read triggers.
        """
        self.loaded.add(os.path.normpath(file_path))


def _strict_realpath(path: str) -> str:
    """``path`` resolved, or unchanged when it cannot be resolved."""
    try:
        return os.path.realpath(path, strict=True)
    except (OSError, ValueError):
        return path


def _link_forms(path: str) -> list[str]:
    """``path``, each link target it leads to in turn, and its resolved path."""
    forms = [path]
    current = path
    for _ in range(40):
        try:
            target = os.readlink(current)
        except (OSError, ValueError):
            break
        current = os.path.normpath(os.path.join(os.path.dirname(current), target))
        if current in forms:
            break
        forms.append(current)
    resolved = _strict_realpath(path)
    if resolved not in forms:
        forms.append(resolved)
    return forms


def _is_utf8(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _fold(text: str) -> str:
    return text.lower().replace("\U00000131", "i").replace("\U0000017f", "s")


def _is_within_folded(path: str, directory: str) -> bool:
    return _is_within(_fold(path), _fold(directory))


def _is_within(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


def _relative(base: str, path: str) -> str:
    relative = os.path.relpath(path, base)
    return "" if relative == "." else relative.replace(os.sep, "/")


def _nested_worktree(cwd: str) -> tuple[str, str] | None:
    """Find a git worktree nested inside its own main repository.

    Returns (worktree root, main repository root) when ``cwd`` is inside such a
    worktree, else ``None``. As Claude Code does, the worktree must be
    registered in the main repository and point back to itself, and a bare
    repository's directory is its main root.
    """
    directory = cwd
    while True:
        dot_git = os.path.join(directory, ".git")
        if os.path.isdir(dot_git) or os.path.isfile(dot_git):
            break
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent
    content = _read_small(dot_git, follow=True)
    if content is None or not content.strip().startswith("gitdir:"):
        return None
    git_dir = os.path.normpath(os.path.join(directory, content.strip()[len("gitdir:") :].strip()))
    common_text = _read_small(os.path.join(git_dir, "commondir"), follow=False)
    back_text = _read_small(os.path.join(git_dir, "gitdir"), follow=False)
    if common_text is None or back_text is None:
        return None
    common = os.path.normpath(os.path.join(git_dir, common_text.strip()))
    if os.path.dirname(git_dir) != os.path.join(common, "worktrees"):
        return None
    back = _strict_realpath(os.path.normpath(os.path.join(git_dir, back_text.strip())))
    if back != os.path.join(_strict_realpath(directory), ".git"):
        return None
    if os.path.basename(common) == ".git":
        main_root = os.path.dirname(common)
    elif os.path.lexists(os.path.join(common, ".git")):
        return None
    else:
        main_root = common
    main_root = os.path.realpath(main_root)
    worktree_root = os.path.realpath(directory)
    if main_root == worktree_root or not _is_within(worktree_root, main_root):
        return None
    return worktree_root, main_root


def _read_small(path: str, *, follow: bool) -> str | None:
    """The text of a small regular file, or ``None`` for anything else."""
    try:
        mode = (os.stat if follow else os.lstat)(path).st_mode
        if not stat.S_ISREG(mode):
            return None
        with open(path, "rb") as handle:
            return handle.read(64 * 1024).decode("utf-8")
    except (OSError, ValueError):
        return None
