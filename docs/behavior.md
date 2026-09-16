# Behavior

c2c-rulesync makes Codex load Claude Code rule files under the conditions
Claude Code loads them. This document specifies those conditions, where they
come from, and where c2c-rulesync deliberately or unavoidably differs.

Target versions: Claude Code **2.1.273**. Claude Code changes quickly, so every
statement here is about that version unless it says otherwise.

Each behavior is labeled with how it is known:

| Label | Meaning |
|---|---|
| documented | Stated in Claude Code or Codex documentation, linked. |
| vector-verified | Reproduced from test vectors computed by the upstream library or runtime itself, committed under `tests/vectors/`. |
| observed | Observed in the installed Claude Code without an end-to-end confirmation. |
| oracle-confirmed | Recorded from a real Claude Code session through the `InstructionsLoaded` hook, committed under `tests/parity/`. |

## Matching `paths:` globs

### Engine

Claude Code decides whether a rule's `paths:` globs match a file with
[node-ignore](https://github.com/kaelzhang/node-ignore) **7.0.5** (observed:
version-specific strings of 7.0.5, and none of 7.0.6 or later, are present in
the installed Claude Code 2.1.270 through 2.1.273). node-ignore implements
`.gitignore` semantics, not the shell-glob semantics the Claude Code
documentation suggests, so several globs match far more than they appear to.

`src/c2c_rulesync/ignore.py` ports node-ignore 7.0.5's `ignores()` with its
default options, which make matching case-insensitive (vector-verified).

| Glob | Path | Matches | Why |
|---|---|---|---|
| `*.md` | `docs/a.md` | yes | A glob without a slash matches at any depth. |
| `*.md` | `.hidden.md` | yes | `*` matches a leading dot. |
| `src` | `packages/x/src/a.ts` | yes | A directory match covers everything beneath it, at any depth. |
| `src/*.ts` | `x/src/a.ts` | no | A slash before the end anchors the glob to the base directory. |
| `/src` | `src/a.ts` | yes | A leading slash anchors; the directory match covers `a.ts`. |
| `**/*.TS` | `a/b.ts` | yes | Matching ignores case. |
| `src/**/*.ts` | `src/a.ts` | yes | `/**/` spans zero or more directories. |
| `docs/` | `docs/a.md` | yes | A trailing slash matches the directory, and so its contents. |
| `src/[slug]/page.tsx` | `src/[slug]/page.tsx` | no | `[slug]` is a character class. |
| `src/\[slug\]/page.tsx` | `src/[slug]/page.tsx` | yes | Escaped brackets are literal. |

Negation works within one rule's glob list: `["*", "!*.md"]` does not match
`a.md`. A negated glob cannot re-include a path whose parent directory matched:
`["src", "!src/a.ts"]` still matches `src/a.ts` (vector-verified).

### Where node-ignore 7.0.5 differs from git

c2c-rulesync keeps these differences, because Claude Code has them
(vector-verified):

- `**/**/foo` does not match `foo`.
- `[!a]` and `[^a]` are not negated classes; they match `!` or `^` and `a`.
- A trailing escaped star is still a wildcard: `abc\*` matches `abcd`.
- `\?` matches neither `?` nor any character.
- Backslash escapes reach the regular expression engine: `\d` matches a digit
  and `\w` a word character.
- Trailing tabs and other whitespace are removed, not only spaces.
- `abc/**/` matches the file `abc/x`.
- An unclosed `[` makes the glob match nothing, and a reversed range such as
  `[a-9]` inside `0`-`z` empties the class.

### Invalid globs

A glob whose regular expression JavaScript cannot compile, such as `[~-!]`, is
dropped on its own and the rule's other globs still apply (vector-verified;
dropping one glob at a time is how Claude Code prepares globs, observed). A
rule whose globs are all invalid matches nothing.

### Characters

node-ignore builds JavaScript regular expressions without the `u` flag, so:

- `?` and `[...]` match one UTF-16 code unit. `a?b` does not match `a😀b`, but
  `a??b` does. The port converts patterns and paths to UTF-16 code units.
- Case-insensitive equality follows JavaScript, not Python. Python's
  `re.IGNORECASE` equates `k` with U+212A KELVIN SIGN and `s` with U+017F LONG
  S, and JavaScript does not. The port does not use that flag; it expands
  characters into JavaScript's equivalence classes, generated from Bun into
  `src/c2c_rulesync/_js_case.py`.

Claude Code 2.1.273 embeds Bun 1.4.3, which has not been published; the classes
are generated with Bun 1.4.2, the newest public release. Node.js 24 and Bun
1.4.2 produce identical results for every vector in
`tests/vectors/node_ignore_7_0_5.json`.

### Known differences from Claude Code

A differential run of 6.5 million pattern and path pairs against node-ignore
7.0.5 under Bun found only these differences, all in globs no one writes:

- A glob nesting escaped parentheses more than 100 levels deep is treated as
  invalid; JavaScript accepts it.
- Bun rejects some globs whose brace quantifier asks for billions of
  repetitions ("pattern exceeds string length limits"); the port accepts them.
  Such a glob cannot match a real path either way.
- A backreference created through escaped parentheses compares
  case-insensitively with Python's folding, which differs from Bun for a few
  characters such as `k`.

### Paths

Paths are matched as relative POSIX paths. node-ignore rejects empty paths,
`.`, `..`, and paths starting with `/`, `./` or `../`; the port returns "no
match" for them instead of raising.

### Regenerating the test data

Both commands need network access or the named runtime, and neither runs in CI:

```bash
node scripts/gen_ignore_vectors.mjs tests/vectors/node_ignore_7_0_5.json
bun scripts/gen_js_case_classes.mjs src/c2c_rulesync/_js_case.py
```

`gen_ignore_vectors.mjs` downloads node-ignore at the pinned commit, checks
the SHA-256 of `index.js` and `test/fixtures/cases.js`, and records:

- node-ignore's own fixture cases;
- curated Claude-shaped and git-divergent inputs;
- 4,000 seeded random patterns with derived paths.

For every vector it records whether each glob is valid on its own and whether
the kept globs match.
