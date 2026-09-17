c2c-rulesync is a Codex CLI command hook that loads Claude Code `.claude/rules/*.md` files, and on
request `CLAUDE.local.md` files, into Codex under the conditions Claude Code 2.1.273 loads them,
including the `claudeMdExcludes` of Claude Code's user settings. It is a Python port of
codex-path-rules.

## Rules

- `c2c-rulesync hook` fails open: every path exits 0 and writes either nothing or exactly one JSON
  object to stdout. No command may exit 2, because Codex treats exit 2 with stderr output from a
  `PreToolUse` hook as a block of the tool call.
- The runtime is standard library only. `dependencies` in `pyproject.toml` stays empty, and the
  code must run on the `requires-python` floor.
- Glob matching reproduces node-ignore 7.0.5 as Claude Code 2.1.273 runs it, quirks included. Do
  not replace it with `fnmatch`, `pathlib` or a git-semantics library, and do not "fix" a quirk.
- Generated files (`tests/vectors/*.json`, `tests/parity/*.json`, `src/c2c_rulesync/_js_case.py`)
  change only by rerunning their script, and a behavior change updates `docs/behavior.md` in the
  same PR.
- Do not copy code, regular expressions, or minified identifiers from the Claude Code binary.
  Describe Claude Code behavior as observed results, and cite public URLs, upstream commits, or
  committed test data.
- Build rule trees for tests under `tmp_path`. Never commit a `.claude/rules` directory under
  `tests/`, or a `CLAUDE.local.md` anywhere: Claude Code and this hook would load it as real
  instructions.

## Commands

```bash
uv sync --locked
uv run pytest tests/test_cli.py -k version   # one test
uv run pytest                                 # full suite
uv run ruff check && uv run ruff format --check
uv run mypy
node scripts/gen_ignore_vectors.mjs tests/vectors/node_ignore_7_0_5.json   # network
bun scripts/gen_js_case_classes.mjs src/c2c_rulesync/_js_case.py         # Bun runtime
bun scripts/gen_yaml_vectors.mjs tests/vectors/bun_yaml.json               # Bun runtime
node scripts/gen_comment_vectors.mjs <marked 15.0.12 dir> tests/vectors/marked_comments.json
python3 scripts/gen_parity_cases.py tests/parity/cases.json
python3 scripts/claude_parity_oracle.py --claude-bin <claude 2.1.273> [--only <case id>]  # paid
python3 scripts/codex_e2e.py [--model <cheap model>]   # paid, real Codex CLI and login
```

## Contributing

Branch from `main` as `<type>/<slug>`. Commit messages and PR titles follow Conventional Commits;
wire the template with `git config commit.template .gitmessage`. Fill
`.github/pull_request_template.md` with the commands you actually ran. `main` is protected: an agent
may open a PR and squash-merge it once the `ci-ok` check is green, and never pushes to `main`.
`scripts/claude_parity_oracle.py` and `scripts/codex_e2e.py` call paid models with the developer's
login: ask before running them.

## Pointers

- [docs/behavior.md](docs/behavior.md) — what loads when, how globs match, and every known
  difference from Claude Code; read it before changing matching, discovery or triggers.
- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) — upstream MIT notices; update it in the same
  change that ports code or test data from another project.
