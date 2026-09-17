# Behavior

c2c-rulesync makes Codex load Claude Code rule files, and on request
`CLAUDE.local.md` files, under the conditions Claude Code loads them. This
document specifies those conditions, where they come from, and where
c2c-rulesync deliberately or unavoidably differs.

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
statement is observed in Claude Code 2.1.273. The project-rule behaviors below
are also oracle-confirmed by the cases in
[Recorded Claude Code sessions](#recorded-claude-code-sessions), except the
4 MiB limit and unreadable git files; user rules, and what c2c-rulesync itself
adds (sorting, warnings, the depth limit), are not. `CLAUDE.local.md` files are
described in [Local instructions](#local-instructions-claudelocalmd), and files
that settings leave out in [Excluded files](#excluded-files-claudemdexcludes).

### Where rules live

- **User rules** live in one directory: `~/.claude/rules`, or `rules` under
  the directory `CLAUDE_CONFIG_DIR` names, which moves every `~/.claude` path
  ([documented](https://code.claude.com/docs/en/claude-directory)). Their globs
  match paths relative to the session's working directory.
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

When `CLAUDE_CONFIG_DIR` moves the user rules elsewhere, `~/.claude/rules` is
still the project rules directory of the home directory. A session under the
home directory then loads its rules as project rules besides the user rules,
and a rule file copied into both directories loads twice, even when the two
copies are identical; entries that resolve to the same file load once, and a
link in `~/.claude/rules` leaving the working directory is skipped, as for any
ancestor rules directory ([Links](#links)). This was observed in Claude Code
2.1.274, not the 2.1.273 target (September 2026): with
`CLAUDE_CONFIG_DIR=~/.claude-extra` and a working directory under the home
directory, reading a `.py` file loaded both
`~/.claude-extra/rules/comments-python.md` and
`~/.claude/rules/comments-python.md`, and the rules without `paths:` in
`~/.claude/rules` were listed as project instructions. c2c-rulesync tells rule
files apart by resolved path and does the same. A `claudeMdExcludes` pattern in
the user settings file can leave them out
([Excluded files](#excluded-files-claudemdexcludes)).

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

### Local instructions (`CLAUDE.local.md`)

Claude Code loads `CLAUDE.local.md` files, private instructions kept out of
version control, when its `local` setting source is on, which it is by default
([documented](https://code.claude.com/docs/en/memory)). c2c-rulesync delivers
them only with `C2C_RULESYNC_LOCAL_INSTRUCTIONS=1`: once delivered, their text
is stored in Codex's session files. The G9 recordings show, for Claude Code
2.1.273 (oracle-confirmed):

- Without the `local` source, no `CLAUDE.local.md` loads.
- At session start, the `CLAUDE.local.md` of the working directory and of each
  ancestor loads. When a file is read, those of the directories strictly between
  the working directory and the file load; a file outside the working directory
  loads none.
- Only `<directory>/CLAUDE.local.md` counts: `.claude/CLAUDE.local.md` does not
  load.
- A file whose body is empty after comments are removed does not load.
- Reading a `CLAUDE.local.md` by its own path does not load it.
- `paths:` front matter never keeps the file from loading. At session start it
  loads regardless, and a nested one loads on any read in its directory, even
  one its globs do not match; Claude Code only reports its globs.
- A `CLAUDE.local.md` that is a link loads, both in an ancestor and in the
  working directory or below, including a link to a file outside the working
  directory (recorded: in a directory beside it). Claude Code reports it under
  the link's path, while it reports a linked rule file under the resolved path.
  A broken link loads nothing.
- A git worktree nested in its main repository does not skip them: the main
  repository's `CLAUDE.local.md`, and one between it and the worktree, load.
- Claude Code imported `@path` written at the start of a line or after white
  space, and not one attached to a word, in a code span, in a fenced or indented
  code block, or inside an HTML comment.

What c2c-rulesync decides itself:

- A directory's `CLAUDE.local.md` follows that directory's rules. The
  documentation says it is appended after `CLAUDE.md`; the recordings cannot
  show order.
- As for rules, the filesystem root is not searched, so `/CLAUDE.local.md` is
  not delivered. The documentation says the files load from every directory
  above the working directory; the root is not recorded.
- As with rules, a `CLAUDE.local.md` read by its own path counts as delivered
  for the rest of the thread, so a later read in its directory does not deliver
  it either.
- A linked `CLAUDE.local.md` is identified, shown and counted as read under the
  link's path, as Claude Code reports it; its content comes from wherever the
  link leads, including outside the working directory, and a broken link is
  skipped with a warning. So a `CLAUDE.local.md` link committed to a repository
  delivers the file it names. That two links to one file load twice, and that
  reading a link counts as delivering its file, follow from this and are not
  recorded.
- The body is read like a rule's: front matter and block-level HTML comments
  are removed, invalid UTF-8 is replaced with a warning, and a file over 4 MiB
  is skipped (documented for CLAUDE.md files). What Claude Code injects is not
  recorded.
- Imports are not expanded. Claude Code resolves them relative to the file,
  follows up to four hops, and asks once before importing files outside the
  working directory (documented). c2c-rulesync delivers the text as written and
  warns once per thread about the `@` tokens that name an existing file,
  relative to the file's directory (for a link, beside the link or its target),
  or under the home directory for `@~/`. A token is `@` at the start of a line
  or after white space, outside code spans and fenced code blocks, without
  trailing sentence punctuation. The warning names the first three, each cut to
  100 characters, and counts the rest. It can also name a token Claude Code did
  not import, such as one in an indented code block. The documentation's advice
  to share instructions across worktrees with an `@~/.claude/...` import does
  not carry over to Codex: write the text into the file, or into a user rule.
- `CLAUDE.md` files are not delivered: they usually import `AGENTS.md`, which
  Codex 0.154.0 loads by itself.

### Excluded files (`claudeMdExcludes`)

Claude Code skips a memory file whose absolute path matches a glob pattern of
the `claudeMdExcludes` setting. The setting works in user, project, local and
managed settings, the lists of all of them apply, and managed `CLAUDE.md` files
cannot be excluded
([documented](https://code.claude.com/docs/en/memory#exclude-specific-claude-md-files)).
The pattern syntax is not documented. The G10 recordings show, for Claude Code
2.1.273 with the patterns in the working directory's `.claude/settings.json`
(oracle-confirmed):

- An excluded rule loads neither at session start, nor as a rule of a directory
  a read passes, nor as a rule whose `paths:` match a read. An excluded
  `CLAUDE.local.md` loads neither at session start nor after a read. The files
  no pattern matches still load.
- A pattern is compared with the whole absolute path, case-sensitively. The
  relative patterns `a.md` and `.claude/rules/a.md` matched nothing, and `~/`
  is not expanded.
- `*` matches within one path segment. `**` as a whole segment matches any
  number of segments, none included; inside a segment, as in `a**.md`, it acts
  as `*`. Both match names that start with a dot, so `/home/me/**` covers
  `/home/me/.claude/rules`.
- A pattern naming a directory, with or without a trailing `/`, does not exclude
  the files in it; `<directory>/**` does.
- `?` matches one character. `{a,b}`, nested braces and integer ranges such as
  `{1..3}` expand. `[ab]`, `[a-c]` and `[^a]` match one character, and `!` in a
  bracket expression is an ordinary member, so `[!a]` matches `a`.
- A rule file reached through a link is excluded by a pattern matching either
  its path under the rules directory, whether the file, a directory below the
  rules directory or `.claude/rules` itself is the link, or its resolved path. A
  `CLAUDE.local.md` that is a link is excluded only by its own path, which
  Claude Code reports, not by the file it leads to.
- A pattern written through a linked directory, such as
  `/alias/.claude/rules/a.md` when `/alias` links to the working directory
  `/real`, excludes `/real/.claude/rules/a.md`. A pattern naming a link to a
  rule file does not exclude that file where it is reached by its own path.
- An excluded file does not count as processed: in the layout of G4-shadowing, a
  working-directory rule first reached through an excluded link in a nested
  directory still loads when its globs match the read.
- A list with an entry that is not a string excludes nothing.

What c2c-rulesync decides itself:

- It reads the patterns only from the user settings file,
  `$CLAUDE_CONFIG_DIR/settings.json` or `~/.claude/settings.json`, found as the
  user rules directory is; project, local and managed settings and `--settings`
  are not read. Patterns are assumed to match there as they did in project
  settings, which the recordings cannot show. `C2C_RULESYNC_CLAUDE_MD_EXCLUDES=0`
  turns the setting off; `C2C_RULESYNC_USER_RULES=0` does not.
- User rules are excluded like project rules (documented for user memory files,
  not recorded).
- The settings file is read on every hook call. A file a thread already
  received is not taken back, so a changed list takes effect for later
  deliveries, and for the session start rules after compaction.
- Only the syntax above is supported. A pattern that uses anything else is not
  applied, so that it never hides a file Claude Code would load, and
  c2c-rulesync warns: a leading `!`, a backslash, parentheses or `|` as in
  extglobs, a `.`, `..` or empty segment, also where braces produce one, an
  unmatched brace or `]`, an empty, unclosed or reversed bracket expression or
  one holding `[`, `/`, `{`, `}` or `,`, braces that are neither a comma list nor
  an integer range, an empty brace alternative, braces nested more than 16 deep,
  or more than 4,096 characters.
- Generalizing the recorded relative patterns, a pattern or brace alternative
  that starts with neither `/` nor a whole `**` segment, such as `*/a.md` or
  `**a.md`, is not applied, with a warning.
- Forms the recordings do not show are matched the usual way: `-` at either end
  of a bracket expression is a member, a range such as `{3..-1}` counts down,
  `?` and bracket expressions match a leading dot as `*` does, a trailing `/**`
  also matches the path before it, and the leading directories of a pattern
  with wildcards, such as `/alias/**/a.md`, are resolved through links too.
- At most 1,000 patterns, and 256 KiB of them, after brace expansion apply, and
  warnings name at most 10 patterns.
- A settings file that is not valid JSON (a byte order mark and `NaN` included),
  not an object, larger than 2 MiB or unreadable, or whose `claudeMdExcludes` is
  not a list of strings, excludes nothing, with a warning. The other keys are
  not checked, so a file whose other values Claude Code rejects still has its
  `claudeMdExcludes` applied. Claude Code reports such a file as a settings
  error, skips it in `-p` sessions and offers to continue without it in
  interactive ones ([documented](https://code.claude.com/docs/en/settings)).
- An excluded file raises no warning, even one that could not be read, or a
  broken link whose path or target a pattern matches. Warnings about a rules
  directory itself, unreadable or nested too deeply, remain.
- The same matching applies on every platform; the recordings are from Linux.

### Not implemented

Claude Code also loads instructions that c2c-rulesync does not bring to Codex:

- `CLAUDE.md` files at any level, and `CLAUDE.local.md` files unless
  `C2C_RULESYNC_LOCAL_INSTRUCTIONS=1`;
- the managed (system-wide) rules directory;
- directories added with `--add-dir` or `permissions.additionalDirectories`:
  Claude Code loads rules for files read there, and with
  `CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD` their rules, and with the
  `local` source their `CLAUDE.local.md`, at session start (documented). Codex
  does not pass its added directories to hooks;
- `@path` imports inside rule files and `CLAUDE.local.md` files;
- rules linked from outside the working directory after a project approved
  external imports in Claude Code, which then load at session start;
- `claudeMdExcludes` in project, local and managed settings and in `--settings`,
  which can leave out files c2c-rulesync delivers.

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

## Delivering rules in Codex

`src/c2c_rulesync/hook.py` handles the Codex events wired in the README
(Codex 0.145.0 or later; behavior checked against the Codex 0.154.0 source and
hook schemas, vendored under `tests/vectors/codex-0.154.0/`).

| Event | What c2c-rulesync does |
|---|---|
| `SessionStart` with source `startup` or `clear` | Injects the session start rules. |
| `SessionStart` with source `compact` | Forgets what the thread received, then injects the session start rules. |
| `SessionStart` with source `resume` | Nothing: the resumed conversation still holds what it received. |
| `SubagentStart` | Injects the session start rules into the subagent. |
| `PreToolUse` | Injects the rules the tool call's files load, and any session start rules the thread has not received. |
| `PostCompact` | Forgets what the thread received; prints nothing. |

With `C2C_RULESYNC_EPHEMERAL_RULES=0`, none of these events does anything in a
thread whose payload has a null `transcript_path`; see
[Threads without a transcript](#threads-without-a-transcript).

### Once per thread

Like Claude Code, each thread of a session receives a rule file once until
its conversation is compacted. c2c-rulesync records what each thread received
under the state directory (see the README), per session id and thread:

- a subagent spawned by a tool is its own thread, named by its `agent_id`;
- another internal thread, such as a review, is told apart by the thread id in
  its transcript file name (inferred from the Codex source, not observed). An
  ephemeral session has no transcript, so its internal threads share the main
  thread's record, and a rule one of them receives is not sent to the other,
  unless `C2C_RULESYNC_EPHEMERAL_RULES=0` leaves them without rules;
- everything else belongs to the main thread.

Reading a rule file counts as receiving it; only reads of `.md` files are
recorded, so reading a rule through a link to a file with another extension
does not.

Hook processes of one thread run one at a time under a file lock, and output
is written only after the new record is staged, then the record is committed.
A failure before the commit makes a rule arrive again later. A hook process
killed after the commit but before Codex reads its output (a Codex timeout or
an interrupt) can lose that delivery until the next compaction.

When the record cannot be kept (an unusable state directory, or a lock held
for more than 2 seconds), `SessionStart` and `SubagentStart` still inject and
say so in the hook message, and `PreToolUse` injects nothing. A compaction
whose new epoch cannot be written removes the record instead.

Rules for the files of one tool call are looked up for at most 3 seconds after
the hook starts; files left over are looked up again on a later call. Records
of sessions whose files have not been used for a week are removed; the sweep
removes only the hook's own files.

Limits of the Codex events:

- A subagent receives no `SessionStart` after it compacts; its session start
  rules come back with its next wired tool call.
- A forked conversation (`codex fork`, or a subagent spawned with the parent's
  history) is a new thread and receives rules again, which duplicates them in
  the copied history. A side conversation is such a fork too; see
  [Threads without a transcript](#threads-without-a-transcript).
- The session start rules are those of the working directory when a thread
  first receives them; rules without `paths:` of a directory the session
  changes to later are not delivered.
- A file edited by `apply_patch` without a prior read loads its rules just
  before the patch applies, after the model has already written the patch.
- With `C2C_RULESYNC_LOCAL_INSTRUCTIONS=1`, the start-time `CLAUDE.local.md`
  files come with the session start rules, so a thread that received those
  before the variable was set, or before upgrading, gets them only after
  compaction or in a new session. After compaction c2c-rulesync sends them
  again, while Claude Code's documentation names only the project-root
  `CLAUDE.md` as read again (not recorded).

### Threads without a transcript

Codex 0.154.0 sends a null `transcript_path` for every thread it keeps no
transcript file for (read from its source, `openai/codex` tag `rust-v0.154.0`,
`core/src/session/mod.rs`):

- a side conversation, started with `/side` or `/btw` in the Codex TUI. It is an
  ephemeral fork (`tui/src/app/side.rs`) that starts with a copy of the parent's
  history, rules already injected included, which its instructions call
  reference context only. It may read files, and edit them when the user asks.
  Its first turn runs `SessionStart` with source `startup`
  (`core/src/session/session.rs`) and a new session id, its tool calls carry
  that id, and compaction in it runs `SessionStart` with source `compact`;
- `codex exec --ephemeral` sessions, and forks made with
  `codex exec fork --ephemeral`;
- a subagent or review started in an ephemeral session, which copies that
  session's configuration (`core/src/tools/handlers/multi_agents_common.rs`,
  `core/src/tasks/review.rs`; not observed);
- every thread of a thread store that is not local.

Persistent sessions, `/fork`, `codex fork`, and the subagents and review
threads of a persistent session have a transcript. Nothing in the payload, the
hook's environment or Codex's configuration tells a side conversation apart
from the other threads without a transcript.

By default such a thread is handled like any other, so a side conversation
receives the session start rules again, duplicating them in its copied history,
as well as rules for the files it reads. With Codex CLI 0.154.0 (September
2026), two side conversations each left a record and received a user rule the
main thread had already received.

With `C2C_RULESYNC_EPHEMERAL_RULES=0`, every event whose `transcript_path` is
JSON null does nothing: no rules, no hook message, and no record, lock, epoch
or sweep. A missing `transcript_path` or any string counts as a transcript.
Files a side conversation reads or edits then load no rules either, including
rules the main thread never received, and a side conversation that compacts
holds none. Claude Code's side questions need no rules because they have no
tool access and answer only from what is already in the conversation
([documented](https://code.claude.com/docs/en/interactive-mode#side-questions-with-btw));
a Codex side conversation can run tools. The setting also leaves
`codex exec --ephemeral` sessions and threads of a store that is not local
without rules, and it takes effect only when every c2c-rulesync handler Codex
runs for the session sets it.

### Checked with Codex

`scripts/codex_e2e.py` runs short `codex exec` sessions against a temporary git
repository with the hook wired in, and reads the rule elements Codex recorded
as developer messages in each session's rollout file. With Codex CLI 0.154.0
(`gpt-5.6-luna`, reasoning low, September 2026) it confirmed:

- a new session receives the unconditional rules of the working directory and
  of an ancestor;
- a shell `cat` of a file receives its path-scoped rule once;
- `codex exec resume` receives nothing again;
- `apply_patch` adding a file receives the rules of the nested directory it is
  added under;
- hooks defined in a trusted project `.codex/config.toml` behave the same as
  hooks passed with `-c`.

Run again with `C2C_RULESYNC_EPHEMERAL_RULES=0` in its environment (same Codex
version and model, September 2026), the same checks passed: the setting leaves
sessions that have a transcript their rules.

It uses the developer's Codex login and configuration, including their own
hooks, and never runs in CI. It turns user rules and `claudeMdExcludes` off, so
the developer's own Claude Code configuration does not change the result.
Compaction, subagents, side conversations, `CLAUDE.local.md` files and
`claudeMdExcludes` are not exercised.

### Output

Rules, and `CLAUDE.local.md` files, reach the model as one `<rule path="...">`
element each, joined by blank lines, in load order. The path is relative to the
working directory when inside it, otherwise under `~` when possible. A
`</rule>` inside a rule is written `<\/rule>`. The user-facing hook message
lists the rules loaded and each warning once per thread until compaction (up to
2,000 warnings). Output is ASCII-only JSON with only the keys Codex accepts for
the event. With `additionalContextLimit = 0`, as the README configures, Codex
does not limit the size of the injected rules.

## Recorded Claude Code sessions

`tests/parity/` holds what real Claude Code 2.1.273 sessions loaded, and
`tests/test_parity.py` replays every recording against c2c-rulesync. Statements
labeled oracle-confirmed in this document are covered by these recordings.

`tests/parity/cases.json` (written by `scripts/gen_parity_cases.py`) describes
36 cases with 60 probes: a file tree, the directory a session starts in, and the
files each session reads. `scripts/claude_parity_oracle.py` builds each tree
under `/tmp`, runs one `claude -p --model haiku` session per probe that may call
only the Read tool on the probe files, and records the `InstructionsLoaded` hook
events: which files loaded at session start, and which loaded after each read,
with the reason and the normalized globs. A `CLAUDE.md` in the working directory
confirms that the hook ran and is left out of the recording. Sessions run with
`--setting-sources project`; a probe's `setting_sources` replaces that. All G9
probes but one add `local`; the remaining one records that no `CLAUDE.local.md`
loads without it. G10 cases put `claudeMdExcludes` in the working directory's
`.claude/settings.json`, the only settings file such a session reads, with
`{root}` in the file standing for the case root; the replay passes those
patterns to c2c-rulesync's matcher.

| Group | What it covers |
|---|---|
| G0 | The recording itself. |
| G1 | Front matter detection, the YAML retry and which `paths` values scope a rule, in 35 rules read against one file; file names that are not UTF-8. |
| G2 | Comma splitting, brace expansion, trailing `/**`, and invalid and negated globs. |
| G3 | How globs meet paths: depth, anchoring, case, character classes, dot files, `..` names and non-ASCII names. |
| G4 | Ancestors, nested directories, files outside the working directory, `--add-dir` directories, reading a rule file, a working directory below the project, and rules shadowed through links. |
| G5 | Git worktrees: nested in their repository, of a bare repository, and with a stale record. |
| G6 | Links: to rule files and directories inside and outside the working directory, in ancestors, under a linked `.claude` or `.claude/rules`, broken and cyclic links, reads through links leaving or entering the working directory, the `$PWD` spelling, and case-variant targets. |
| G8 | A rule loads once per session; reading a rule file by its own path counts, and through a link it does not. |
| G9 | `CLAUDE.local.md`: the `local` setting source, ancestor, working-directory, nested and intermediate files, files outside the working directory, empty files, `.claude/CLAUDE.local.md`, a read of the file by its own path, `paths:` front matter, imports, links and a nested git worktree. |
| G10 | `claudeMdExcludes`: rules and `CLAUDE.local.md` files at session start and after reads, `*`, `**` and dot names, relative, directory and case-variant patterns, `?`, braces, ranges and bracket expressions, links to files and directories, patterns written through links, a processed file behind an excluded link, a non-string entry, and `~`. |

The recordings compare which files load and with which globs, not the order of
loads or the text injected: the hook sees neither. The oracle also records a
probe whose Read fails, so a missing load is not mistaken for a rule; none of
the recorded reads failed.

Two recorded differences are intended. Claude Code loads rules for files in
`--add-dir` directories, which Codex does not pass to hooks
(`LAZY_DIVERGENCES` in `tests/test_parity.py`). Claude Code loads the files a
`CLAUDE.local.md` imports, recorded as `include` loads; c2c-rulesync does not
expand imports, so the replay checks instead that its import warning names each
of them. The replay compares a recorded `CLAUDE.local.md` by the path Claude
Code reported and a rule file by its resolved path.

Not recorded:

- user rules (`~/.claude/rules`), because the sessions run with project settings
  only under the developer's own login;
- a rules directory shared by user and project scope, approved external
  imports, and the other behaviors described without the oracle-confirmed
  label;
- `CLAUDE.local.md` at the filesystem root, over 4 MiB or with invalid UTF-8,
  after compaction, and with imports from outside the working directory;
- `claudeMdExcludes` in user, local or managed settings, lists from several
  settings files, invalid JSON, syntax beyond the G10 patterns, and matching on
  macOS or Windows.

Recording calls a paid model (the first 44 probes reported about $0.60, the 8 G9
probes $0.12, and the 8 G10 probes $0.11, in September 2026) and never runs in
CI:

```bash
python3 scripts/gen_parity_cases.py tests/parity/cases.json
python3 scripts/claude_parity_oracle.py --claude-bin ~/.local/share/claude/versions/2.1.273
```

The recording stores the SHA-256 of `cases.json`, so a changed case fails the
replay until it is recorded again; `--only <case id>` records single cases into
the existing file. A new Claude Code version gets its own
`claude-code-<version>.json`.

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
