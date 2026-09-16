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

## Reading a rule file

`src/c2c_rulesync/frontmatter.py` turns a rule file into an injected body and a
list of globs, or no globs for a rule that applies unconditionally. Each step
reproduces Claude Code 2.1.273 (observed); the oracle-confirmed cases are
listed in `tests/parity/`.

### Front matter

- A leading byte order mark is ignored.
- Front matter must start at the very first character with `---` followed by
  white space up to a line break, so `----` does not open it.
- It closes at the first later `---`, even in the middle of a line:
  `paths: "x---y"` ends the front matter after `"x`.
- Without a closing `---` there is no front matter, and the whole file,
  including its first line, is the body of an unconditional rule.

### YAML and the retry

The front matter is parsed as YAML (see [Parsing front matter YAML](#parsing-front-matter-yaml)).
If Bun rejects it, Claude Code retries once after two rewrites:

1. Every line of the form `key: value`, whose key uses only letters, `_` and
   `-`, gets its value double-quoted, escaping `\` and `"`, when the value
   contains any of `{ } [ ] * & # ! | > % @` or a backtick, or contains `: `.
   A value already wrapped in matching quotes, or written `[...]` and parsing
   as a list, is left alone.
2. Every run of tabs at the start of a line becomes two spaces per tab.

Consequences worth knowing:

- `paths: *.ts` works, because the retry quotes it.
- A list item `- **/*.ts` is never rewritten, so the retry fails too, and the
  rule loads for every file. Quote globs that start with `*`.
- `paths: [**/*.ts]` becomes the single literal glob `[**/*.ts]`, a character
  class.
- The retry quotes a whole value, comments included, so once any line forces
  a retry, `paths: a.md # note` becomes the glob `a.md # note`.
- A line ending in a carriage return (CRLF files) is never rewritten.

If the retry fails, the front matter is ignored: the rule loads for every file.
c2c-rulesync does the same and reports a warning. If the YAML uses constructs
c2c-rulesync does not model, it reads `paths` line by line and warns that the
result may differ from Claude Code.

### From `paths` to globs

- Only the `paths` key counts; `globs` or `Paths` are ignored.
- A missing or falsy value (`null`, `""`, `0`, `false`) leaves the rule
  unconditional. So does a value yielding no glob, such as `[]`, `123` or a
  mapping.
- A list is flattened, nested lists included, and non-string items are
  skipped.
- Each string is split on commas outside braces, so `paths: src/**, docs/**`
  gives two globs. The brace depth can go negative, and a stray `}` then
  disables splitting for the rest of the string.
- Each piece is trimmed and brace-expanded left to right: `{a,b}{1,2}` gives
  `a1 a2 b1 b2`, `x{,.bak}` gives `x x.bak`, and `{x}` gives `x`. A rule's
  expansions share a budget of 1,000 results and 4 MiB; a piece that would
  exceed it stays unexpanded.
- One trailing `/**` is removed from each glob, so `src/**` becomes `src`,
  which matches a directory named `src` at any depth (see
  [Matching](#matching-paths-globs)).
- If no glob remains, or every glob is `**`, the rule is unconditional.

### Body

When the body contains `<!--`, block-level HTML comments are removed as marked
15-17 tokenizes them (Claude Code uses a marked release in that range, observed
from its HTML tokenizer). The body's line endings become `\n`, and:

- A comment starting a block (up to three spaces of indentation, outside code,
  lists, block quotes and other HTML blocks) is removed, along with the rest of
  its last line if that is blank, and the line breaks after it.
- A comment inside a paragraph line, a list item, a block quote or code stays.
- An unclosed comment stays.

`src/c2c_rulesync/markdown_blocks.py` approximates marked's block structure
rather than porting marked. `tests/vectors/marked_comments.json` holds marked
17.0.6's output for 31 curated bodies and 3,000 random ones: the curated bodies
all match, and 2 random bodies differ. Both combine a stray `</script>` line, a
comment and a setext underline.

```bash
npm install --prefix /tmp/marked-17 marked@17.0.6
node scripts/gen_comment_vectors.mjs /tmp/marked-17/node_modules/marked tests/vectors/marked_comments.json
```

## Parsing front matter YAML

Claude Code parses a rule's front matter with `Bun.YAML.parse` (observed), and
whether that parse succeeds decides whether the rule is path-scoped.
`src/c2c_rulesync/bun_yaml.py` predicts Bun's result for the YAML front matter
contains: block and flow collections, plain, quoted and block scalars, anchors,
aliases, tags and comments.

Bun departs from YAML 1.2 in ways that matter for rules (vector-verified):

- An unquoted value starting with `*`, such as `paths: *.ts` or the list item
  `- **/*.ts`, is an alias to an undefined anchor, so the whole document fails.
- `paths: {a,b}.ts` is a flow mapping followed by text, and fails.
- A value containing `: `, such as `description: Rules for: the API`, fails.
- `012` is the number 12, `0X1F` and `1_000` are strings, and `yes`, `no` and
  `on` are strings. A hexadecimal or octal number above 64 bits is a string,
  and a decimal one too large for a double is infinity.
- Tabs used as indentation fail, and so does a tab between indentation and a
  list item or key, as in `paths:\n  \t- a.md`. A blank or comment line
  starting with a tab also fails in the value of a mapping key that is not the
  mapping's first, unless the text before it is a plain or block scalar: after
  `description: d` and `paths: "a"`, a line holding only a tab fails. Claude
  Code's retry replaces those tabs, so such front matter usually still applies.
- A lone carriage return breaks lines, so `paths: a.md\rb.md` fails.
- A directive such as `%YAML 1.2` fails, because front matter cannot hold the
  `---` that would follow it.
- `\u` escapes of a surrogate pair form one character; any other surrogate
  escape fails.
- A plain value cannot start with `]` or `}`.

The parser answers in one of three ways: a value, "Bun throws", or "not
modeled" for constructs such as explicit `?` keys, nesting deeper than Python's
recursion limit, and quoted scalars broken across lines ending in carriage
returns. The caller treats "not modeled" separately and warns instead of
guessing.

`tests/vectors/bun_yaml.json` holds Bun's answers for about 3,500 documents:

- hand-picked edge cases;
- combinations of keys, values and layouts rule files use;
- a seeded corpus of mostly malformed documents held out from calibration.

The parser gives no wrong answer on any of them and declines about 2% as not
modeled. Documents generated outside the repository also produced no wrong
answer: eleven held-out corpora of 4,000 documents each, and 80,000 documents
derived from all of these by inserting tabs, carriage returns, directives,
surrogate escapes and large numbers (between 6% and 15% declined). Claude Code 2.1.273 embeds Bun
1.4.3, which is unpublished; the vectors come from Bun 1.4.2.

```bash
bun scripts/gen_yaml_vectors.mjs tests/vectors/bun_yaml.json
```

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
