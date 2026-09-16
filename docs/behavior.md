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

## Finding rules

`src/c2c_rulesync/rules.py` decides which rule files load and when. Each
statement is observed in Claude Code 2.1.273 unless labeled otherwise.

### Where rules live

- **User rules** live in one directory, `~/.claude/rules` by default. Their
  globs match paths relative to the session's working directory.
- **Project rules** live in a `.claude/rules` directory of the working
  directory, of any of its ancestors except the filesystem root, or of any
  directory below the working directory. Their globs match paths relative to
  the directory that contains `.claude`.

A rules directory is searched recursively. A rule file's name must end in
exactly `.md`, so `RULE.MD` is not a rule. Claude Code visits files in
directory order; c2c-rulesync sorts names so the order is stable.

### When rules load

At session start:

1. unconditional user rules;
2. unconditional project rules of the outermost ancestor, then of each
   directory down to the working directory.

When a file is read:

1. user rules whose globs match it;
2. for each directory strictly between the working directory and the file,
   outermost first: all its unconditional rules, then its rules whose globs
   match;
3. rules of the working directory and its ancestors whose globs match,
   outermost first.

A read loads rules only when the path read, each link it passes through as a
file, and its resolved path all lie inside the working directory: the resolved
working directory, or the directory as the session was started in it (`$PWD`
for Claude Code, the payload's `cwd` for Codex). So a file reached through a link
leaving the working directory loads nothing, and neither does a file outside it.
A relative path that is empty or starts with `..`, including a directory named
`..foo`, matches no glob. When the path relative to the base directory leaves
it, the file's parent directory is resolved through links and the path is tried
again; the file itself is never resolved, so `src/link.ts` linking to
`lib/real.ts` inside the working directory matches `src/**`, not `lib/**`.

A session loads each rule file at most once. Reading a rule file by its own
path counts as loading it, so neither that read nor a later match loads it; a
rule read through a link to it still loads later. Within one scan, a file
reached twice, for example through a link, loads once. When the user rules
directory is also a project rules directory, as `~/.claude/rules` is for a
session under the home directory, the user copy loads first and wins; this
follows from the load order and has not been checked in a session.

A rule file is skipped when its body is empty after front matter and comments
are removed, or when it is larger than 4 MiB (c2c-rulesync warns). Invalid
UTF-8 in a file is replaced and loads, with a warning; a file or directory whose
name is not valid UTF-8 is skipped, as Claude Code's runtime cannot open it.
c2c-rulesync does not walk rules directories nested more than 256 levels deep.

### Git worktrees

When the working directory is inside a git worktree nested in its own main
repository, such as `repo/.claude/worktrees/w1`, the main repository's
directories from its root down to the worktree root are not ancestors for rule
loading. The worktree's own checked-out `.claude/rules` loads instead, and
ancestors above the main repository still load.

- The worktree must be registered in the repository, and the repository's
  record must point back to it; a stale record does not skip anything.
- For a worktree of a bare repository, the bare repository's directory is the
  main root.
- An unreadable or non-regular `.git`, `gitdir` or `commondir` file means no
  worktree.

### Links

- Links to files and directories are followed, and link cycles end.
- A link that cannot be followed is skipped; c2c-rulesync warns.
- A project `.claude/rules` that is itself a link is skipped unless it
  resolves inside the working directory.
- A project rules entry whose resolved path is not its own place in the
  directory must resolve inside the working directory. An ancestor rules
  directory is outside the working directory, so even its link to a file
  beside it is skipped. The same holds in a rules directory reached through a
  linked `.claude`: its own files load, its links leaving the working directory
  do not.
- These containment checks ignore case (c2c-rulesync approximates Claude Code's
  folding with lower case), so on a case-sensitive file system a link from
  `Proj` to `proj/shared.md` counts as inside `Proj`.
- User rules may link anywhere.

### Not implemented

Claude Code also loads instructions that c2c-rulesync does not bring to Codex:

- `CLAUDE.md` and `CLAUDE.local.md` files, at any level;
- the managed (system-wide) rules directory;
- directories added with `--add-dir` or `permissions.additionalDirectories`:
  Claude Code loads rules for files read there, and with
  `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` their rules at session start.
  Codex does not pass its added directories to hooks;
- `@path` imports inside rule files;
- rules linked from outside the working directory after a project approved
  external imports in Claude Code, which then load at session start;
- `claudeMdExcludes` settings.

## Which tool calls load rules

Claude Code loads path-scoped rules when it reads a file, including before an
edit, which its tools require. Codex has no read tool: the model reads files
with shell commands and edits them with `apply_patch`. c2c-rulesync therefore
treats the files a tool call reads or writes as read
(`src/c2c_rulesync/touched.py`). This loads rules for tool calls Claude Code has
no equivalent for; a shell command it cannot read, described below, loads
nothing.

What Codex 0.154.0 sends a `PreToolUse` hook is read from its source
(`openai/codex` tag `rust-v0.154.0`): `Bash` carries only
`{"command": "<script>"}` (`core/src/tools/handlers/unified_exec/exec_command.rs`),
`apply_patch` carries the raw patch as `command`
(`core/src/tools/handlers/apply_patch.rs`), and other tools carry their JSON
arguments.

### `apply_patch`

The file of every `*** Add File:`, `*** Update File:` and `*** Delete File:`
header, and the destination of `*** Move to:`, between `*** Begin Patch` and
`*** End Patch`. As Codex's patch parser reads them
(`apply-patch/src/streaming_parser.rs`), lines split at line feeds, and white
space around a header is ignored, except inside an `*** Update File:` section,
where an indented header-like line is diff context. Paths resolve against the
session's working directory; a patch with an `*** Environment ID:` header may
apply in another environment, which the hook cannot see.

### `view_image` and other tools

The `path`, `file_path` or `filePath` string of the tool input, for any tool the
hook is wired to, unless it names an existing directory.

### Shell commands (`Bash`)

`src/c2c_rulesync/shell.py` splits the command the way a POSIX shell would,
closely enough to find:

- the file operands of `cat`, `nl`, `head`, `tail`, `less`, `more`, `bat`,
  `tee` and `diff`; of `sed`, `awk`, `grep`, `rg` and `jq` after their script,
  pattern or filter; and of `perl` with `-e` or `-E`, as in `perl -pi -e`;
- for `git diff`, `git show` and `git log`, the paths after `--`, or without
  `--` the operands that are existing files, as git accepts them; `git blame`'s
  file; and `git show REV:PATH`, relative to the repository's top level;
- redirection targets (`<`, `>`, `>>`, `&>` and similar), except `/dev/*`;
- `apply_patch` run through the shell, with its patch in a here-document or an
  argument;
- commands inside `-c` scripts and here-documents of `bash`, `sh`, `zsh`, `dash`
  and `ksh`, inside `$(...)`, backquotes and `<(...)`, and in subshells;
- commands behind `env`, `sudo`, `timeout`, `nice`, `nohup`, `time`,
  `command`, `builtin`, `exec`, `stdbuf` and variable assignments.

Options are read per command, including clusters such as `grep -nC 3`, so their
values are not taken for files.

`cd` changes the directory later relative paths resolve against, except inside
a subshell, a pipeline or a background command; `pushd` and `popd` keep a stack.
A substitution resolves against the directory of the command it belongs to.
After `cd` to a directory only the shell knows (no argument, `-`, a variable or
command output), or `popd` without a matching `pushd`, relative paths are
ignored until the next `cd` to a known directory. `~` expands to the home
directory.

It ignores:

- operands that are existing directories, so `grep -r TODO src` loads nothing
  for `src`, as Claude Code's search tools load nothing;
- `find`, `ls`, `cp`, `mv`, `python` and every command not listed above;
- words the shell would expand: variables, command output, and unquoted globs
  (`*`, `?`, `[`) and brace expansions (`{a,b}`, `{1..3}`);
- here-document bodies other than a patch or a shell script;
- `<` and `>` inside `(( ))` and `[[ ]]`.

A script is examined up to 20,000 commands and 16 levels of nesting.

Codex runs a command in its tool call's `workdir` when one is given, but does
not pass `workdir` to hooks, so relative paths resolve against the session's
working directory.

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

When the body contains `<!--`, Claude Code removes block-level HTML comments
with marked's lexer, GFM off, and that lexer behaves as marked **15.0.12**
(observed: the two give identical output on 23,031 bodies). Later releases
differ: marked 16.4.2 and 17.0.6 disagree with it on about 7% of
`tests/vectors/marked_comments.json`, for example by keeping link reference
definitions. The body's line endings become `\n`, and (vector-verified):

- A comment starting a block (up to three spaces of indentation, outside code,
  lists, block quotes and other HTML blocks) is removed, along with the rest of
  its last line if JavaScript's `trim()` empties that, and the line breaks
  after it. `trim()` removes U+FEFF and U+3000 but not U+0085.
- A comment inside a paragraph line, a list item, a block quote or code stays.
- An unclosed comment stays.
- A link reference definition such as `[style]: https://example.com/style`
  disappears unless it continues a paragraph.
- A single space or tab ending the body right after a comment line is removed
  with the comment.
- A block quote continued by lines without `>` can come out with its text
  rearranged, because marked rebuilds its raw text from lengths measured in
  different strings.

`src/c2c_rulesync/markdown_blocks.py` ports marked 15.0.12's block lexer,
keeping only what decides the raw text of top-level tokens, and spells out the
JavaScript semantics it relies on: `\s`, `.`, case-insensitive matching and
UTF-16 lengths. `tests/vectors/marked_comments.json` holds marked 15.0.12's
output for 58 curated bodies and 3,000 random ones, and the port matches all
of them. A differential run of 500,000 further generated bodies, built from
comments, every kind of HTML block, fences, indented code, lists, block quotes,
headings, link definitions, CRLF and CR line endings and Unicode white space,
found no difference either.

The one known difference: block quotes nested more than about 490 levels deep
exceed Python's recursion limit, and the body is then kept unchanged, comments
included. marked under Bun 1.4.2 handles 4,000 levels.

Speed differs too. marked takes quadratic time on some bodies: 4,000 paragraphs
each followed by a comment line with trailing text take 79 s under Bun 1.4.2
(0.3 s under Node.js 24), while the port takes linear time on them. Lazily
continued nested block quotes stay quadratic in the port: 8,000 of them take 9 s
in the port and 50 s in marked under Bun 1.4.2.

```bash
npm install --prefix /tmp/marked-15 marked@15.0.12
node scripts/gen_comment_vectors.mjs /tmp/marked-15/node_modules/marked tests/vectors/marked_comments.json
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
