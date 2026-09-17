# c2c-rulesync

A Codex CLI hook that loads Claude Code `.claude/rules/*.md` files into Codex
under the same conditions Claude Code 2.1.273 loads them.

- Rules without `paths:` front matter are injected when a Codex session or
  subagent starts.
- Rules with `paths:` are injected the first time a tool call reads or edits a
  matching file, with Claude Code's glob semantics.
- Rules come from `~/.claude/rules` (or `$CLAUDE_CONFIG_DIR/rules`), from the
  `.claude/rules` of the working directory and its ancestors, and from
  `.claude/rules` directories below the working directory that a touched file
  lives under.
- With `C2C_RULESYNC_LOCAL_INSTRUCTIONS=1`, `CLAUDE.local.md` files, private
  project instructions, are delivered too, where Claude Code loads them;
  `@path` imports in them are not expanded. `CLAUDE.md` files are not delivered:
  they usually import `AGENTS.md`, which Codex reads itself.

[docs/behavior.md](docs/behavior.md) specifies what loads when, and every known
difference from Claude Code.

> **Status:** 0.2.0. Checked against Claude Code 2.1.273
> and Codex CLI 0.154.0.

## Requirements

- Codex CLI 0.145.0 or later.
- Linux or macOS. Windows is untested.
- [uv](https://docs.astral.sh/uv/), which installs the tool and a Python 3.11
  or later interpreter for it.

## Install

```bash
uv tool install git+https://github.com/92khsang/c2c-rulesync@v0.2.0
c2c-rulesync --version
```

`uv tool install` puts `c2c-rulesync` in the directory `uv tool dir --bin`
prints. That directory must be on the `PATH` Codex starts with.

## Configure Codex

Add the hooks to `~/.codex/config.toml`, or to a project's
`.codex/config.toml`:

```toml
[[hooks.SessionStart]]
matcher = "startup|clear|compact"

[[hooks.SessionStart.hooks]]
type = "command"
command = "c2c-rulesync hook"
timeout = 10
additionalContextLimit = 0

[[hooks.SubagentStart]]

[[hooks.SubagentStart.hooks]]
type = "command"
command = "c2c-rulesync hook"
timeout = 10
additionalContextLimit = 0

[[hooks.PreToolUse]]
matcher = "Bash|apply_patch|view_image"

[[hooks.PreToolUse.hooks]]
type = "command"
command = "c2c-rulesync hook"
timeout = 10
additionalContextLimit = 0

[[hooks.PostCompact]]

[[hooks.PostCompact.hooks]]
type = "command"
command = "c2c-rulesync hook"
timeout = 10
```

Then start Codex and trust the new hooks with `/hooks`. Codex runs a hook only
while its definition matches what was trusted, so trust them again after
changing any of these lines.

- `additionalContextLimit = 0` delivers rules whole. Without it, Codex moves
  hook output longer than about 10,000 bytes into a file and shows the model
  only its beginning and end.
- `PostCompact` makes rules deliverable again after Codex compacts a
  conversation, as Claude Code reloads them after compaction.
- The hook never blocks a tool call: on any error it exits 0 without output.

## Environment

Codex hooks see the environment Codex was started with. Set these before
starting Codex, or for one project in front of its hook command (see
[Settings for one project](#settings-for-one-project)):

| Variable | Effect |
|---|---|
| `CLAUDE_CONFIG_DIR` | As in Claude Code: user rules are read from `$CLAUDE_CONFIG_DIR/rules` instead of `~/.claude/rules`. A hook that does not see the variable, for example because only a shell function or alias sets it for `claude`, uses `~/.claude/rules`. A working directory under the home directory still loads `~/.claude/rules` as project rules, as Claude Code does. |
| `C2C_RULESYNC_USER_RULES` | `0` turns user rules off. |
| `C2C_RULESYNC_EPHEMERAL_RULES` | `0` gives no rules to threads Codex keeps no transcript for. These are side conversations (`/side`, `/btw`), whose copied history already holds the rules the main thread received but whose own file reads and edits then load none; `codex exec --ephemeral` sessions; and every thread of a thread store that is not local, as read from the Codex CLI 0.154.0 source ([docs/behavior.md](docs/behavior.md#threads-without-a-transcript)). |
| `C2C_RULESYNC_LOCAL_INSTRUCTIONS` | `1` also delivers `CLAUDE.local.md` files: those of the working directory and its ancestors when a thread starts, and those of the directories below it that lead to a file a tool call reads or edits. Off by default, because the files are private; any other value leaves them off. `@path` imports in them are not expanded, and the hook warns about them. A thread that received its session start rules before the variable was set gets the start-time files after compaction. Links are followed wherever they lead, as in Claude Code, so a `CLAUDE.local.md` link in any checkout delivers the file it points to ([docs/behavior.md](docs/behavior.md#local-instructions-claudelocalmd)). |
| `C2C_RULESYNC_STATE_DIR` | An absolute directory, used only by c2c-rulesync, for the record of what each session received. A relative value is ignored. The default is `$XDG_STATE_HOME/c2c-rulesync`, or `~/.local/state/c2c-rulesync`. |

If the record cannot be written, the hook says so when a session starts and
delivers only rules without `paths:`.

### Settings for one project

Codex runs a hook command with the user's shell, so a project can set these
variables in front of the command, in every c2c-rulesync handler of its
`.codex/config.toml`. For a project used only with a second Claude Code account
whose configuration lives in `~/.claude-extra`, write
`command = 'CLAUDE_CONFIG_DIR="$HOME/.claude-extra" c2c-rulesync hook'`. Several
variables can go in front, as in
`CLAUDE_CONFIG_DIR="$HOME/.claude-extra" C2C_RULESYNC_EPHEMERAL_RULES=0 c2c-rulesync hook`.

- Codex runs the hooks of every configuration layer together: a project's
  hooks do not replace those in `~/.codex`
  ([Codex hooks](https://developers.openai.com/codex/hooks)). Wire c2c-rulesync
  only in project configuration; a handler left in `~/.codex/config.toml` or
  `~/.codex/hooks.json` still runs beside the project's, with the environment
  Codex started with, so the variables do not take effect.
- Write `$HOME` rather than `~`: no shell expands `~` inside quotes, as in
  `"~/.claude-extra"`, and dash and zsh do not expand it in
  `env VARIABLE=~/x command`.
- The `VARIABLE=value command` form needs a POSIX-style shell such as bash or
  zsh.
- Trust a changed command again with `/hooks`; until then Codex does not run it.
- Under the home directory, `~/.claude/rules` still loads as project rules, so a
  rule copied into both directories loads twice
  ([docs/behavior.md](docs/behavior.md#when-rules-load)).

## What Codex sees

Rules, and `CLAUDE.local.md` files when they are turned on, reach the model in a
developer message, one element per file:

```text
<rule path=".claude/rules/testing.md">
Run the unit tests before committing.
</rule>
```

The user sees a hook message listing the files loaded, and any warning about a
file c2c-rulesync could not read the way Claude Code would or imports it did not
expand.

## License

MIT. See [LICENSE](LICENSE).

## Acknowledgements

c2c-rulesync is a Python port of
[codex-path-rules](https://github.com/bengous/codex-path-rules) by Augustin
BENGOLEA, released under the MIT License. It also ports
[node-ignore](https://github.com/kaelzhang/node-ignore) 7.0.5, which matches
globs, and the block lexer of [marked](https://github.com/markedjs/marked)
15.0.12, which finds the comments Claude Code removes. Their notices are
reproduced in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
