"""Gitignore-style path matching that reproduces node-ignore 7.0.5.

Claude Code 2.1.273 decides whether a rule's ``paths:`` globs match a file with
node-ignore 7.0.5 (https://github.com/kaelzhang/node-ignore, commit
84d052ddfe7c326b01b306154e06709d6e7e2ed8, MIT License). This module ports that
library's ``ignores()`` so a rule loads in Codex exactly when it loads in Claude
Code, including where 7.0.5 differs from git: a trailing ``\\*`` is still a
wildcard, ``[!a]`` is not a negated class, ``**/**/foo`` does not match ``foo``,
and backslash escapes such as ``\\d`` reach the regular expression engine.

Matching happens in two stages. The first applies node-ignore's replacer chain
to produce the JavaScript regular expression source node-ignore would compile.
The second parses that source the way JavaScript does without the ``u`` flag
and emits an equivalent Python pattern. Patterns and paths are converted to
UTF-16 code units first, because such a JavaScript expression matches code
units rather than code points.

Case-insensitive matching does not use ``re.IGNORECASE``: Python folds some
characters differently from JavaScript (it equates ``k`` with U+212A KELVIN
SIGN, for example). Characters are instead expanded to the equivalence classes
JavaScript uses, generated from Bun into ``_js_case``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from functools import lru_cache, partial

from c2c_rulesync._js_case import CASE_CLASSES

__all__ = ["InvalidPatternError", "Matcher", "is_valid_pattern", "matches"]

# JavaScript's \s: the WhiteSpace and LineTerminator code points.
_JS_WHITESPACE = (
    "\t\n\x0b\x0c\r \xa0\U00001680\U00002000-\U0000200a"
    "\U00002028\U00002029\U0000202f\U0000205f\U00003000\U0000feff"
)
_JS_WHITESPACE_CLASS = f"[{_JS_WHITESPACE}]"
# JavaScript's `.` without the `s` flag excludes the line terminators.
_JS_DOT = "[^\n\r\U00002028\U00002029]"
_ANY = r"[\s\S]"
_NEVER = "(?!)"


class InvalidPatternError(ValueError):
    """The pattern compiles to a regular expression JavaScript rejects."""


# Stage 1: node-ignore's replacer chain -------------------------------------

# A replacer receives the match and the pattern body the chain started from.
_Replacer = Callable[[re.Match[str], str], str]


def _even_backslashes(backslashes: str) -> str:
    return backslashes[: len(backslashes) - len(backslashes) % 2]


def _sanitize_range(range_text: str) -> str:
    # Reversed ranges are harmless in gitignore but fatal in JavaScript, so
    # node-ignore deletes the ones whose endpoints both lie within 0-z.
    return re.sub(
        "([0-z])-([0-z])",
        lambda m: m.group(0) if m.group(1) <= m.group(2) else "",
        range_text,
    )


def _constant(value: str) -> _Replacer:
    return lambda match, body: value


def _trailing_whitespace(match: re.Match[str], body: str) -> str:
    return match.group(1) + (" " if match.group(2).startswith("\\") else "")


def _escaped_whitespace(match: re.Match[str], body: str) -> str:
    return _even_backslashes(match.group(1)) + " "


def _escape_metacharacter(match: re.Match[str], body: str) -> str:
    return "\\" + match.group(0)


def _starting(match: re.Match[str], body: str) -> str:
    # A slash anywhere but at the very end anchors the pattern to the base
    # directory; otherwise the pattern may match at any depth.
    return "^" if re.search(r"/(?!\Z)", body) else "(?:^|\\/)"


def _two_globstars(match: re.Match[str], body: str) -> str:
    # `/**/` spans zero or more directories; a final `/**` matches everything
    # inside but not the directory itself.
    return "(?:\\/[^\\/]+)*" if match.start() + 6 < len(match.string) else "\\/.+"


def _intermediate_wildcard(match: re.Match[str], body: str) -> str:
    return match.group(1) + "[^\\/]*"


def _bracket(match: re.Match[str], body: str) -> str:
    lead, range_text, end_escape, close = match.group(1, 2, 3, 4)
    if lead == "\\":
        return f"\\[{range_text}{_even_backslashes(end_escape)}{close}"
    if close == "]" and len(end_escape) % 2 == 0:
        return f"[{_sanitize_range(range_text)}{end_escape}]"
    return "[]"


def _ending(match: re.Match[str], body: str) -> str:
    last = match.group(0)
    return f"{last}$" if last == "/" else f"{last}(?=$|\\/$)"


# Each entry mirrors one node-ignore replacer: (regex, is the JavaScript regex
# global, replacement). The regexes are the JavaScript ones with `$` written as
# `\Z` and `\s` / `.` spelled out as the JavaScript character sets.
_REPLACERS: tuple[tuple[re.Pattern[str], bool, _Replacer], ...] = (
    (re.compile("^\U0000feff"), False, _constant("")),
    (
        re.compile(rf"((?:\\\\)*?)(\\?{_JS_WHITESPACE_CLASS}+)\Z"),
        False,
        _trailing_whitespace,
    ),
    (re.compile(rf"(\\+?){_JS_WHITESPACE_CLASS}"), True, _escaped_whitespace),
    (re.compile(r"[\\$.|*+(){^]"), True, _escape_metacharacter),
    (re.compile(r"(?!\\)\?"), True, _constant("[^/]")),
    (re.compile("^/"), False, _constant("^")),
    (re.compile("/"), True, _constant("\\/")),
    (re.compile(r"^\^*\\\*\\\*\\/"), False, _constant("^(?:.*\\/)?")),
    (re.compile("^(?=[^^])"), False, _starting),
    (re.compile(r"\\/\\\*\\\*(?=\\/|\Z)"), True, _two_globstars),
    (re.compile(rf"(^|[^\\]+)(\\\*)+(?={_JS_DOT}+)"), True, _intermediate_wildcard),
    (re.compile(r"\\\\\\(?=[$.|*+(){^])"), True, _constant("\\")),
    (re.compile(r"\\\\"), True, _constant("\\")),
    (re.compile(r"(\\)?\[([^\]/]*?)(\\*)(\Z|\])"), True, _bracket),
    (re.compile(r"(?:[^*])\Z"), False, _ending),
)

_TRAILING_WILDCARD = re.compile(r"(^|\\/)?\\\*\Z")


def _call_replacer(replacer: _Replacer, body: str, match: re.Match[str]) -> str:
    return replacer(match, body)


def _javascript_source(body: str) -> str:
    """The regular expression source node-ignore builds for ``ignores()``."""
    source = body
    for regex, is_global, replacer in _REPLACERS:
        source = regex.sub(
            partial(_call_replacer, replacer, body), source, count=0 if is_global else 1
        )

    def trailing_wildcard(match: re.Match[str]) -> str:
        return f"{match.group(1)}[^/]+(?=$|\\/$)" if match.group(1) else "[^/]*(?=$|\\/$)"

    return _TRAILING_WILDCARD.sub(trailing_wildcard, source, count=1)


# Stage 2: JavaScript (non-unicode) regular expression -> Python --------------

_WORD = "A-Za-z0-9_"
_CLASS_ESCAPES = {"d": "0-9", "w": _WORD, "s": _JS_WHITESPACE}
_CONTROL_ESCAPES = {"t": "\t", "n": "\n", "v": "\x0b", "f": "\x0c", "r": "\r"}
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_WORD_BOUNDARY = f"(?:(?<=[{_WORD}])(?![{_WORD}])|(?<![{_WORD}])(?=[{_WORD}]))"
_NOT_WORD_BOUNDARY = f"(?:(?<=[{_WORD}])(?=[{_WORD}])|(?<![{_WORD}])(?![{_WORD}]))"


# Every character that has case-insensitive equivalents, mapped to its class.
_CASE_EQUIVALENTS: dict[str, str] = {char: members for members in CASE_CLASSES for char in members}


def _class_member(char: str) -> str:
    return f"\\U{ord(char):08x}"


def _literal(char: str) -> str:
    """A Python pattern matching ``char`` as a case-insensitive JavaScript regex does."""
    equivalents = _CASE_EQUIVALENTS.get(char)
    if equivalents is None:
        return re.escape(char)
    return "[" + "".join(map(_class_member, equivalents)) + "]"


def _case_equivalents_in_range(start: str, end: str) -> Iterable[str]:
    for members in CASE_CLASSES:
        if any(start <= member <= end for member in members):
            yield from members


# What a translated term is, which decides whether a quantifier may follow it.
# Plain constants rather than an Enum: this module is on the hook's hot path.
_ATOM = 0
_ASSERTION = 1
_LOOKAROUND = 2
_QUANTIFIED = 3
_LAZY = 4


class _ClassAtom:
    __slots__ = ("char", "is_hyphen", "text")

    def __init__(self, text: str, char: str | None, *, is_hyphen: bool = False) -> None:
        self.text = text
        # The single character the atom denotes, or None for a class escape.
        self.char = char
        # A literal, unescaped hyphen, which may form a range.
        self.is_hyphen = is_hyphen


class _Translator:
    """Translate the JavaScript regular expressions node-ignore produces.

    The input holds the constructs node-ignore emits plus whatever a pattern's
    backslash escapes leave behind: groups, anchors, alternation, quantifiers,
    `.`, character classes and escapes. Anything JavaScript would reject, such
    as an unmatched parenthesis or a quantifier with nothing to repeat, raises
    InvalidPatternError.
    """

    def __init__(self, source: str) -> None:
        self._source = source
        self._index = 0

    def translate(self) -> str:
        translated = self._disjunction()
        if self._index < len(self._source):
            raise InvalidPatternError("unmatched ')'")
        return translated

    def _peek(self, offset: int = 0) -> str:
        position = self._index + offset
        return self._source[position] if position < len(self._source) else ""

    def _disjunction(self) -> str:
        alternatives = [self._alternative()]
        while self._peek() == "|":
            self._index += 1
            alternatives.append(self._alternative())
        return "|".join(alternatives)

    def _alternative(self) -> str:
        terms: list[str] = []
        # What the last term is, which decides whether a quantifier may follow.
        last: int | None = None
        while self._index < len(self._source) and self._peek() not in ("|", ")"):
            char = self._peek()
            if char not in ("*", "+", "?"):
                text, last = self._term()
                terms.append(text)
                continue
            self._index += 1
            if char == "?" and last is _QUANTIFIED:
                terms[-1] += "?"
                last = _LAZY
            elif last is _ATOM:
                terms[-1] += char
                last = _QUANTIFIED
            elif last is _LOOKAROUND:
                terms[-1] = f"(?:{terms[-1]}){char}"
                last = _QUANTIFIED
            else:
                raise InvalidPatternError("nothing to repeat")
        return "".join(terms)

    def _term(self) -> tuple[str, int]:
        char = self._source[self._index]
        if char == "\\":
            return self._escape_outside_class()
        self._index += 1
        if char == "[":
            return self._character_class(), _ATOM
        if char == ".":
            return _JS_DOT, _ATOM
        if char == "^":
            return "^", _ASSERTION
        if char == "$":
            return r"\Z", _ASSERTION
        if char == "(":
            return self._group()
        return _literal(char), _ATOM

    def _group(self) -> tuple[str, int]:
        kind = _ATOM
        opener = "(?:"
        if self._peek() == "?":
            marker = self._peek(1)
            if marker not in (":", "=", "!"):
                raise InvalidPatternError("invalid group")
            self._index += 2
            opener = "(?" + marker
            if marker != ":":
                kind = _LOOKAROUND
        inner = self._disjunction()
        if self._peek() != ")":
            raise InvalidPatternError("unterminated group")
        self._index += 1
        return f"{opener}{inner})", kind

    def _escape_outside_class(self) -> tuple[str, int]:
        self._index += 1
        letter = self._peek()
        if letter in _CLASS_ESCAPES:
            self._index += 1
            return f"[{_CLASS_ESCAPES[letter]}]", _ATOM
        if letter.lower() in _CLASS_ESCAPES:
            self._index += 1
            return f"[^{_CLASS_ESCAPES[letter.lower()]}]", _ATOM
        if letter == "b":
            self._index += 1
            return _WORD_BOUNDARY, _ASSERTION
        if letter == "B":
            self._index += 1
            return _NOT_WORD_BOUNDARY, _ASSERTION
        return _literal(self._character_escape(in_class=False)), _ATOM

    def _character_escape(self, *, in_class: bool) -> str:
        """Consume an escape that denotes one character and return it.

        ``self._index`` points just past the backslash.
        """
        if self._index >= len(self._source):
            raise InvalidPatternError("\\ at end of pattern")
        letter = self._source[self._index]
        self._index += 1
        if letter in _CONTROL_ESCAPES:
            return _CONTROL_ESCAPES[letter]
        if letter == "b" and in_class:
            return "\b"
        if letter == "c":
            control = self._peek()
            if (control.isascii() and control.isalpha()) or (
                in_class and (control.isdigit() or control == "_")
            ):
                self._index += 1
                return chr(ord(control) % 32)
            # A `\c` without a control letter is a literal backslash; the `c`
            # is read again as an ordinary character.
            self._index -= 1
            return "\\"
        if letter == "x" and all(self._peek(i) in _HEX_DIGITS for i in range(2)):
            return self._hex(2)
        if letter == "u" and all(self._peek(i) in _HEX_DIGITS for i in range(4)):
            return self._hex(4)
        if letter in "01234567":
            return self._legacy_octal(letter)
        return letter

    def _hex(self, digits: int) -> str:
        value = self._source[self._index : self._index + digits]
        self._index += digits
        return chr(int(value, 16))

    def _legacy_octal(self, first: str) -> str:
        # The expressions have no capturing groups, so a decimal escape is a
        # legacy octal escape of up to three digits with a value up to 0o377.
        digits = first
        while len(digits) < 3 and self._peek() in tuple("01234567"):
            if int(digits + self._peek(), 8) > 0o377:
                break
            digits += self._peek()
            self._index += 1
        return chr(int(digits, 8))

    def _character_class(self) -> str:
        negated = self._peek() == "^"
        if negated:
            self._index += 1
        atoms: list[_ClassAtom] = []
        complements: list[str] = []
        while True:
            if self._index >= len(self._source):
                raise InvalidPatternError("unterminated character class")
            char = self._source[self._index]
            if char == "]":
                self._index += 1
                break
            if char != "\\":
                self._index += 1
                atoms.append(_ClassAtom(_class_member(char), char, is_hyphen=char == "-"))
                continue
            self._index += 1
            letter = self._peek()
            if letter in _CLASS_ESCAPES:
                self._index += 1
                atoms.append(_ClassAtom(_CLASS_ESCAPES[letter], None))
            elif letter.lower() in _CLASS_ESCAPES:
                # \D, \W and \S cannot nest inside a Python class.
                self._index += 1
                complements.append(_CLASS_ESCAPES[letter.lower()])
            else:
                value = self._character_escape(in_class=True)
                atoms.append(_ClassAtom(_class_member(value), value))
        members = "".join(self._class_ranges(atoms))
        if not complements:
            if negated:
                return f"[^{members}]" if members else _ANY
            return f"[{members}]" if members else _NEVER
        alternatives = ([f"[{members}]"] if members else []) + [f"[^{c}]" for c in complements]
        union = "|".join(alternatives)
        return f"(?:(?!{union}){_ANY})" if negated else f"(?:{union})"

    @staticmethod
    def _class_ranges(atoms: list[_ClassAtom]) -> Iterable[str]:
        index = 0
        while index < len(atoms):
            atom = atoms[index]
            if index + 2 < len(atoms) and atoms[index + 1].is_hyphen:
                end = atoms[index + 2]
                if atom.char is not None and end.char is not None:
                    if atom.char > end.char:
                        raise InvalidPatternError("range out of order in character class")
                    yield f"{atom.text}-{end.text}"
                    yield from map(_class_member, _case_equivalents_in_range(atom.char, end.char))
                    index += 3
                    continue
            yield atom.text
            if atom.char is not None:
                yield from map(_class_member, _CASE_EQUIVALENTS.get(atom.char, ""))
            index += 1


# Rules and matching ---------------------------------------------------------


def _code_units(text: str) -> str:
    """Spell astral characters as UTF-16 surrogate pairs."""
    if all(ord(char) <= 0xFFFF for char in text):
        return text
    units: list[str] = []
    for char in text:
        code = ord(char)
        if code <= 0xFFFF:
            units.append(char)
        else:
            code -= 0x10000
            units.append(chr(0xD800 + (code >> 10)))
            units.append(chr(0xDC00 + (code & 0x3FF)))
    return "".join(units)


def _is_rule_text(pattern: str) -> bool:
    # node-ignore skips blank lines, comments, and patterns ending in a single
    # unescaped backslash, all judged on the raw pattern.
    return (
        bool(pattern)
        and re.fullmatch(f"{_JS_WHITESPACE_CLASS}+", pattern) is None
        and re.search(r"(?:[^\\]|^)\\\Z", pattern) is None
        and not pattern.startswith("#")
    )


class _Rule:
    __slots__ = ("negative", "regex")

    def __init__(self, negative: bool, regex: re.Pattern[str]) -> None:
        self.negative = negative
        self.regex = regex


@lru_cache(maxsize=4096)
def _compile(pattern: str) -> _Rule | None:
    """Compile one pattern, or return ``None`` when node-ignore skips it.

    Raises:
        InvalidPatternError: JavaScript would reject the regular expression.
    """
    units = _code_units(pattern)
    if not _is_rule_text(units):
        return None
    negative = units.startswith("!")
    body = units[1:] if negative else units
    body = re.sub(r"^\\!", "!", body, count=1)
    body = re.sub(r"^\\#", "#", body, count=1)
    translated = _Translator(_javascript_source(body)).translate()
    return _Rule(negative, re.compile(translated))


def is_valid_pattern(pattern: str) -> bool:
    """Whether node-ignore 7.0.5 can evaluate ``pattern`` without throwing."""
    try:
        _compile(pattern)
    except InvalidPatternError:
        return False
    return True


_INVALID_PATH = re.compile(r"^\.{0,2}/|^\.{1,2}\Z")


class Matcher:
    """``ignore().add(patterns)`` from node-ignore 7.0.5, case-insensitive.

    Patterns node-ignore could not evaluate are dropped when the matcher is
    built, which is how Claude Code prepares a rule's globs.
    """

    def __init__(self, patterns: Iterable[str]) -> None:
        rules: list[_Rule] = []
        for pattern in patterns:
            try:
                rule = _compile(pattern)
            except InvalidPatternError:
                continue
            if rule is not None:
                rules.append(rule)
        self._rules = tuple(rules)

    def ignores(self, path: str) -> bool:
        """Whether ``path``, a relative POSIX path, is matched.

        A directory that matches makes everything beneath it match, and a
        negated pattern cannot re-include a path under a matched directory.
        Paths node-ignore rejects (empty, ``.``, ``..``, or starting with
        ``/``, ``./`` or ``../``) return ``False``.
        """
        if not path or _INVALID_PATH.search(path):
            return False
        units = _code_units(path)
        segments = [segment for segment in units.split("/") if segment]
        for depth in range(1, len(segments)):
            if self._test("/".join(segments[:depth]) + "/"):
                return True
        return self._test(units)

    def _test(self, path: str) -> bool:
        ignored = unignored = False
        for rule in self._rules:
            # node-ignore skips rules that cannot change the outcome.
            if rule.negative == unignored and ignored != unignored:
                continue
            if rule.negative and not ignored and not unignored:
                continue
            if rule.regex.search(path):
                ignored = not rule.negative
                unignored = rule.negative
        return ignored


def matches(patterns: Iterable[str], path: str) -> bool:
    """Whether node-ignore 7.0.5 with ``ignorecase`` matches ``path``."""
    return Matcher(patterns).ignores(path)
