#!/usr/bin/env python3
"""Write tests/parity/cases.json, the file trees scripts/claude_parity_oracle.py records.

Each case is a tree of files under a temporary root, the working directory a
Claude Code session starts in, and probes: the files one session reads. Groups:

- G0: a smoke test of the recording itself.
- G1: front matter and YAML, one sentinel file read against many rules.
- G2: turning `paths` into globs, and invalid globs.
- G3: how globs meet paths.
- G4: ancestor, nested and outside directories, and --add-dir.
- G5: a git worktree nested in its repository.
- G6: links.
- G8: each rule loads once per session.
- G9: CLAUDE.local.md, recorded with the `local` setting source and one probe
  without it.

Later G1, G4, G5, G6 and G8 cases cover links inside linked directories, reads
through links, worktree edge cases, file names and additional directories.

User rules (`~/.claude/rules`) are not recorded: the sessions run with project
settings only, under the developer's own login and configuration directory. A
probe's `setting_sources` replaces the default `project`.

Usage:
    python3 scripts/gen_parity_cases.py tests/parity/cases.json
"""

import base64
import json
import sys
from pathlib import Path
from typing import Any


def scoped(globs_yaml: str, marker: str) -> str:
    return f"---\n{globs_yaml}\n---\n\n{marker}\n"


S = "zz-sentinel.txt"
cases: list[dict[str, Any]] = []

# G0: smoke -------------------------------------------------------------------
cases.append(
    {
        "id": "G0-smoke",
        "group": "G0",
        "cwd": "proj",
        "tree": {
            "proj/.claude/rules/scoped.md": scoped(
                'paths: ["probe.txt", "{a,b}/**"]', "G0 cwd scoped"
            ),
            "proj/pkg/.claude/rules/pkg-unscoped.md": "G0 nested unscoped\n",
            "proj/pkg/.claude/rules/pkg-scoped.md": scoped(
                'paths:\n  - "*.txt"', "G0 nested scoped"
            ),
            "proj/pkg/probe.txt": "probe\n",
        },
        "probes": [{"read": ["proj/pkg/probe.txt"]}],
    }
)

# G1: front matter and YAML (one session, all rules at the working directory) -
f: dict[str, str] = {}
f["F01-none.md"] = "F01 no front matter\n"
f["F02-empty.md"] = "---\n---\nF02 empty front matter\n"
f["F03-unclosed.md"] = f"---\npaths: {S}\nF03 unclosed\n"
f["F04-dashes-comment.md"] = f"---\n# -----\npaths: {S}\n---\nF04 early close\n"
f["F05-dashes-value.md"] = f'---\npaths: "{S}---x"\n---\nF05 dashes in value\n'
f["F06-plain.md"] = scoped(f"paths: {S}", "F06 plain")
f["F07-star-scalar.md"] = scoped(f"paths: *{S}", "F07 star scalar")
f["F08-star-item.md"] = scoped(f"paths:\n  - *{S}", "F08 star list item")
f["F09-star-flow.md"] = scoped(f"paths: [*{S}, {S}]", "F09 star flow list")
f["F10-comment-quoted.md"] = scoped(f"description: *x\npaths: {S} # note", "F10 comment swallowed")
f["F10b-comment.md"] = scoped(f"description: plain\npaths: {S} # note", "F10b comment")
f["F11-crlf-star.md"] = f"---\r\npaths: *{S}\r\n---\r\nF11 crlf star\r\n"
f["F12-crlf-list.md"] = f"---\r\npaths:\r\n  - {S}\r\n---\r\nF12 crlf list\r\n"
f["F13-bom.md"] = f"\U0000feff---\npaths: {S}\n---\nF13 bom\n"
f["F14-tabs.md"] = f"---\npaths:\n\t- {S}\n---\nF14 tabs\n"
f["F15-brace-scalar.md"] = scoped("paths: {zz-sentinel,q}.txt", "F15 brace scalar")
f["F16-brace-item.md"] = scoped("paths:\n  - {zz-sentinel,q}.txt", "F16 brace item")
f["F17-capital-key.md"] = scoped(f"Paths: {S}", "F17 capital key")
f["F18-null.md"] = scoped("paths:", "F18 null")
f["F19-empty-string.md"] = scoped('paths: ""', "F19 empty string")
f["F20-empty-list.md"] = scoped("paths: []", "F20 empty list")
f["F21-double-star.md"] = scoped('paths: ["**"]', "F21 double star")
f["F22-double-star-and.md"] = scoped(f'paths: ["**", "{S}"]', "F22 double star and sentinel")
f["F23-number.md"] = scoped("paths: 123", "F23 number")
f["F24-nested-list.md"] = scoped(f"paths: [{S}, 5, [q]]", "F24 nested list")
f["F25-mapping.md"] = scoped(f"paths: {{a: {S}}}", "F25 mapping")
f["F26-duplicate.md"] = scoped(f"paths: q\npaths: {S}", "F26 duplicate key")
f["F27-block-scalar.md"] = scoped(f"paths: |\n  {S}", "F27 block scalar")
f["F28-scalar-document.md"] = "---\nzz\n---\nF28 scalar document\n"
f["F29-four-dashes.md"] = f"----\npaths: {S}\n---\nF29 four dashes\n"
f["F30-blank-first.md"] = f"\n---\npaths: {S}\n---\nF30 blank first line\n"
f["F31-empty-body.md"] = f"---\npaths: {S}\n---\n\n"
f["F32-comment-body.md"] = f"---\npaths: {S}\n---\n<!-- only a comment -->\n"
f["F34-upper.MD"] = f"---\npaths: {S}\n---\nF34 upper extension\n"
f["F35-description-colon.md"] = scoped(
    f"description: Rules for: the API\npaths: {S}", "F35 colon in description"
)
tree = {f"proj/.claude/rules/{name}": text for name, text in f.items()}
tree["proj/.claude/rules/F33-invalid-utf8.md"] = {
    "base64": base64.b64encode(b"F33 invalid \xff utf8\n").decode()
}
tree[f"proj/{S}"] = "sentinel\n"
cases.append(
    {
        "id": "G1-front-matter",
        "group": "G1",
        "cwd": "proj",
        "tree": tree,
        "probes": [{"read": [f"proj/{S}"]}],
    }
)

# G2: normalization and pattern validity ---------------------------------------
n: dict[str, str] = {}
n["N01-scalar-commas.md"] = scoped(f"paths: q.txt, {S}", "N01 scalar commas")
n["N02-item-commas.md"] = scoped(f'paths: ["q.txt, {S}"]', "N02 item commas")
n["N03-brace-commas.md"] = scoped('paths: "{zz-sentinel,q}.txt"', "N03 brace commas")
n["N04-bracket-commas.md"] = scoped(f'paths: "[a,b]x, {S}"', "N04 bracket commas")
n["N05-single-brace.md"] = scoped('paths: "{zz-sentinel}.txt"', "N05 single alternative")
n["N06-empty-alternative.md"] = scoped('paths: "zz-sentinel{,.bak}.txt"', "N06 empty alternative")
n["N07-nested-braces.md"] = scoped('paths: "{zz-{sentinel,q}}.txt"', "N07 nested braces")
n["N08-two-groups.md"] = scoped('paths: "{q,zz}-{x,sentinel}.txt"', "N08 two groups")
n["N09-stray-brace.md"] = scoped(f'paths: "q}}, {S}"', "N09 stray closing brace")
n["N10-over-budget.md"] = scoped('paths: ["' + "{a,b}" * 10 + f'", "{S}"]', "N10 over budget")
n["N11-dir.md"] = scoped('paths: "zz-dir/**"', "N11 directory")
n["N12-dir-double.md"] = scoped('paths: "zz-dir/**/**"', "N12 directory double globstar")
n["N13-slash-globstar.md"] = scoped('paths: "/**"', "N13 slash globstar")
n["N14-dot-slash.md"] = scoped(f'paths: "./{S}"', "N14 dot slash")
n["N15-leading-slash.md"] = scoped(f'paths: "/{S}"', "N15 leading slash")
n["N16-invalid-and-valid.md"] = scoped(f'paths: ["[~-!]", "{S}"]', "N16 invalid and valid")
n["N17-only-invalid.md"] = scoped('paths: ["[~-!]"]', "N17 only invalid")
n["N18-negation.md"] = scoped(f'paths: ["*", "!{S}"]', "N18 negation")
n["N19-lone-negation.md"] = scoped(f'paths: ["!{S}"]', "N19 lone negation")
n["N20-escaped-braces.md"] = scoped("paths: '\\{zz-sentinel\\}.txt'", "N20 escaped braces")
tree = {f"proj/.claude/rules/{name}": text for name, text in n.items()}
tree[f"proj/{S}"] = "sentinel\n"
tree["proj/zz-dir/x.txt"] = "x\n"
cases.append(
    {
        "id": "G2-normalization",
        "group": "G2",
        "cwd": "proj",
        "tree": tree,
        "probes": [{"read": [f"proj/{S}"]}, {"read": ["proj/zz-dir/x.txt"]}],
    }
)

# G3: how globs meet paths -----------------------------------------------------
g = {
    "P1-star-md.md": 'paths: "*.md"',
    "P1-docs-star.md": 'paths: "docs/*.md"',
    "P1-docs-deep.md": 'paths: "docs/**/*.md"',
    "P1-slash-docs.md": 'paths: "/docs"',
    "P1-guide.md": 'paths: "guide"',
    "P1-upper.md": 'paths: "DOCS/**"',
    "P2-src.md": 'paths: "src/**"',
    "P2-src-ts.md": 'paths: "src/*.ts"',
    "P2-any-src.md": 'paths: "**/src/*.ts"',
    "P2-packages.md": 'paths: "packages/*/src/*.ts"',
    "P3-env.md": 'paths: ".env*"',
    "P3-local.md": 'paths: "**/*.local"',
    "P3-config.md": 'paths: ".config"',
    "P4-class.md": 'paths: "src/[slug]/page.tsx"',
    "P4-escaped.md": "paths: 'src/\\[slug\\]/page.tsx'",
    "P4-star.md": 'paths: "src/*/page.tsx"',
    "P5-any-ts.md": 'paths: "**/*.ts"',
    "P5-star-ts.md": 'paths: "*.ts"',
    "P6-unicode.md": 'paths: "docs/\U000000e4b.md"',
}
tree = {f"proj/.claude/rules/{name}": scoped(yaml, name) for name, yaml in g.items()}
probes = [
    "docs/guide/a.md",
    "packages/x/src/a.ts",
    ".config/.env.local",
    "src/[slug]/page.tsx",
    "..foo/a.ts",
    "Docs/\U000000c4B.MD",
]
for probe in probes:
    tree[f"proj/{probe}"] = "probe\n"
cases.append(
    {
        "id": "G3-matching",
        "group": "G3",
        "cwd": "proj",
        "tree": tree,
        "requires": ["posix_names"],
        "probes": [{"read": [f"proj/{probe}"]} for probe in probes],
    }
)

# G4: scopes and base directories ------------------------------------------------
tree = {
    "A/.claude/rules/a-unscoped.md": "G4 ancestor unscoped\n",
    "A/.claude/rules/a-proj.md": scoped('paths: "proj/src/**"', "G4 ancestor proj/src"),
    "A/.claude/rules/a-src.md": scoped('paths: "src/*.ts"', "G4 ancestor src"),
    "A/proj/.claude/rules/p-unscoped.md": "G4 cwd unscoped\n",
    "A/proj/.claude/rules/p-src.md": scoped('paths: "src/**"', "G4 cwd src"),
    "A/proj/.claude/rules/p-pkg.md": scoped('paths: "pkg/src/*.ts"', "G4 cwd pkg"),
    "A/proj/pkg/.claude/rules/k-unscoped.md": "G4 pkg unscoped\n",
    "A/proj/pkg/.claude/rules/k-src.md": scoped('paths: "src/*.ts"', "G4 pkg src"),
    "A/proj/pkg/sub/.claude/rules/s-unscoped.md": "G4 sub unscoped\n",
    "A/proj/pkg/sub/.claude/rules/s-ts.md": scoped('paths: "*.ts"', "G4 sub ts"),
    "A/proj/pkg/src/a.ts": "",
    "A/proj/src/a.ts": "",
    "A/proj/pkg/sub/x/y.ts": "",
    "A/other/src/a.ts": "",
    "A/proj2/src/a.ts": "",
}
cases.append(
    {
        "id": "G4-scopes",
        "group": "G4",
        "cwd": "A/proj",
        "tree": tree,
        "probes": [
            {"read": ["A/proj/pkg/src/a.ts"]},
            {"read": ["A/proj/src/a.ts"]},
            {"read": ["A/proj/pkg/sub/x/y.ts"]},
            {"read": ["A/other/src/a.ts"]},
            {"read": ["A/other/src/a.ts"], "args": ["--add-dir", "{root}/A/other"]},
            {"read": ["A/proj2/src/a.ts"], "args": ["--add-dir", "{root}/A/proj2"]},
            {"read": ["A/proj/pkg/.claude/rules/k-src.md"]},
            {"read": ["A/proj/pkg/src/a.ts"], "cwd": "A/proj/pkg"},
        ],
    }
)

# G5: git worktree nested in its repository ---------------------------------------
tree = {
    ".claude/rules/parent.md": "G5 parent unscoped\n",
    "M/.claude/rules/m-unscoped.md": "G5 main unscoped\n",
    "M/.claude/rules/m-src.md": scoped('paths: "src/**"', "G5 main src"),
    "M/src/a.ts": "",
    "M/sub/src/a.ts": "",
}
git_env = ["-c", "user.email=oracle@example.com", "-c", "user.name=oracle"]
cases.append(
    {
        "id": "G5-worktree",
        "group": "G5",
        "cwd": "M/.claude/worktrees/w1",
        "requires": ["git"],
        "tree": tree,
        "setup": [
            {"cwd": "M", "run": ["git", "init", "-q"]},
            {"cwd": "M", "run": ["git", *git_env, "add", "."]},
            {"cwd": "M", "run": ["git", *git_env, "commit", "-q", "-m", "init"]},
            {"cwd": "M", "run": ["git", "worktree", "add", "-q", ".claude/worktrees/w1"]},
        ],
        "probes": [
            {"read": ["M/.claude/worktrees/w1/src/a.ts"]},
            {"read": ["M/sub/src/a.ts"], "cwd": "M/sub"},
        ],
    }
)

# G6: symbolic links ----------------------------------------------------------------
tree = {
    "proj/shared/in-unscoped.md": "G6 linked in-cwd unscoped\n",
    "proj/shared/in-scoped.md": scoped(f'paths: "{S}"', "G6 linked in-cwd scoped"),
    "proj/.claude/rules/sibling.md": "G6 sibling\n",
    "outside/out-unscoped.md": "G6 linked outside unscoped\n",
    "outside/out-scoped.md": scoped(f'paths: "{S}"', "G6 linked outside scoped"),
    "proj/shared-dir/in-dir.md": "G6 linked dir in-cwd\n",
    "outside/dir/out-dir.md": "G6 linked dir outside\n",
    "proj/.claude/rules/broken-sub/b.md": "G6 beside broken link\n",
    "proj/.claude/rules/l-in-unscoped.md": {"symlink": "../../shared/in-unscoped.md"},
    "proj/.claude/rules/l-in-scoped.md": {"symlink": "../../shared/in-scoped.md"},
    "proj/.claude/rules/l-sibling.md": {"symlink": "sibling.md"},
    "proj/.claude/rules/l-out-unscoped.md": {"symlink": "../../../outside/out-unscoped.md"},
    "proj/.claude/rules/l-out-scoped.md": {"symlink": "../../../outside/out-scoped.md"},
    "proj/.claude/rules/l-in-dir": {"symlink": "../../shared-dir"},
    "proj/.claude/rules/l-out-dir": {"symlink": "../../../outside/dir"},
    "proj/.claude/rules/loop": {"symlink": "."},
    "proj/.claude/rules/broken-sub/broken.md": {"symlink": "missing.md"},
    f"proj/{S}": "sentinel\n",
}
cases.append(
    {
        "id": "G6-links-entries",
        "group": "G6",
        "cwd": "proj",
        "requires": ["symlink"],
        "tree": tree,
        "probes": [{"read": [f"proj/{S}"]}],
    }
)
cases.append(
    {
        "id": "G6-rules-dir-link",
        "group": "G6",
        "cwd": "proj",
        "requires": ["symlink"],
        "tree": {
            "outside/rules/x.md": "G6 rules dir linked outside\n",
            "proj/.claude/rules": {"symlink": "../../outside/rules"},
            f"proj/{S}": "sentinel\n",
        },
        "probes": [{"read": [f"proj/{S}"]}],
    }
)
cases.append(
    {
        "id": "G6-claude-dir-link",
        "group": "G6",
        "cwd": "proj",
        "requires": ["symlink"],
        "tree": {
            "outside/.claude/rules/x.md": "G6 .claude linked outside\n",
            "outside/.claude/rules/y.md": scoped(
                f'paths: "{S}"', "G6 .claude linked outside scoped"
            ),
            "proj/.claude": {"symlink": "../outside/.claude"},
            f"proj/{S}": "sentinel\n",
        },
        "probes": [{"read": [f"proj/{S}"]}],
    }
)
cases.append(
    {
        "id": "G6-ancestor-link",
        "group": "G6",
        "cwd": "A/proj",
        "requires": ["symlink"],
        "tree": {
            "A/shared.md": "G6 ancestor linked file\n",
            "A/.claude/rules/real.md": "G6 ancestor real\n",
            "A/.claude/rules/alias.md": {"symlink": "../../shared.md"},
            f"A/proj/{S}": "sentinel\n",
        },
        "probes": [{"read": [f"A/proj/{S}"]}],
    }
)
cases.append(
    {
        "id": "G6-file-link",
        "group": "G6",
        "cwd": "proj",
        "requires": ["symlink"],
        "tree": {
            "proj/.claude/rules/lib.md": scoped('paths: "lib/**"', "G6 lib"),
            "proj/.claude/rules/src.md": scoped('paths: "src/**"', "G6 src"),
            "proj/lib/real.ts": "",
            "proj/src/link.ts": {"symlink": "../lib/real.ts"},
        },
        "probes": [{"read": ["proj/src/link.ts"]}],
    }
)

# G8: once per session -------------------------------------------------------------
cases.append(
    {
        "id": "G8-once",
        "group": "G8",
        "cwd": "proj",
        "tree": {
            "proj/.claude/rules/src.md": scoped('paths: "src/*.ts"', "G8 src"),
            "proj/src/a.ts": "",
            "proj/src/b.ts": "",
        },
        "probes": [
            {"read": ["proj/src/a.ts", "proj/src/b.ts"]},
            {"read": ["proj/.claude/rules/src.md", "proj/src/a.ts"]},
        ],
    }
)

# Cases added after reviewing discovery against Claude Code's behavior: links
# inside linked directories, reads through links, worktree edge cases, file
# names and additional directories.
cases.append(
    {
        "id": "G6-claude-dir-link-entries",
        "group": "G6",
        "cwd": "proj",
        "requires": ["symlink"],
        "tree": {
            "outside/.claude/rules/x.md": "G6b regular file in linked .claude\n",
            "elsewhere/out.md": "G6b linked outside cwd\n",
            "elsewhere/out-scoped.md": "---\n"
            'paths: "zz-sentinel.txt"\n'
            "---\n"
            "\n"
            "G6b linked outside cwd scoped\n",
            "proj/shared/in.md": "G6b linked inside cwd\n",
            "outside/.claude/rules/l-out.md": {"symlink": "../../../elsewhere/out.md"},
            "outside/.claude/rules/l-out-scoped.md": {
                "symlink": "../../../elsewhere/out-scoped.md"
            },
            "outside/.claude/rules/l-in.md": {"symlink": "../../../proj/shared/in.md"},
            "proj/.claude": {"symlink": "../outside/.claude"},
            "proj/zz-sentinel.txt": "sentinel\n",
        },
        "probes": [{"read": ["proj/zz-sentinel.txt"]}],
    }
)
cases.append(
    {
        "id": "G6-rules-dir-link-inside",
        "group": "G6",
        "cwd": "proj",
        "requires": ["symlink"],
        "tree": {
            "proj/shared-rules/a.md": "G6c regular file in linked rules dir\n",
            "proj/docs/in.md": "G6c linked inside cwd\n",
            "outside/out.md": "G6c linked outside cwd\n",
            "proj/shared-rules/l-in.md": {"symlink": "../docs/in.md"},
            "proj/shared-rules/l-out.md": {"symlink": "../../outside/out.md"},
            "proj/.claude/rules": {"symlink": "../shared-rules"},
            "proj/zz-sentinel.txt": "sentinel\n",
        },
        "probes": [{"read": ["proj/zz-sentinel.txt"]}],
    }
)
cases.append(
    {
        "id": "G6-nested-through-link",
        "group": "G6",
        "cwd": "proj",
        "requires": ["symlink"],
        "tree": {
            "proj/pkg/.claude/rules/n.md": "G6d nested unscoped\n",
            "outside/out.md": "G6d nested entry linked outside cwd\n",
            "proj/pkg/.claude/rules/l-out.md": {"symlink": "../../../../outside/out.md"},
            "proj/pkg/a.ts": "",
            "proj/link-pkg": {"symlink": "pkg"},
        },
        "probes": [{"read": ["proj/link-pkg/a.ts"]}],
    }
)
cases.append(
    {
        "id": "G6-read-leaves-cwd",
        "group": "G6",
        "cwd": "proj",
        "requires": ["symlink"],
        "tree": {
            "proj/.claude/rules/ext.md": '---\npaths: "ext/*.ts"\n---\n\nG6e ext\n',
            "proj/.claude/rules/src.md": '---\npaths: "src/*.ts"\n---\n\nG6e src\n',
            "outside/a.ts": "",
            "outside/.claude/rules/n.md": "G6e nested rules outside cwd\n",
            "proj/ext": {"symlink": "../outside"},
            "proj/src/out.ts": {"symlink": "../../outside/a.ts"},
            "proj/src/in.ts": "",
        },
        "probes": [{"read": ["proj/ext/a.ts"]}, {"read": ["proj/src/out.ts", "proj/src/in.ts"]}],
    }
)
cases.append(
    {
        "id": "G6-read-enters-cwd",
        "group": "G6",
        "cwd": "proj",
        "requires": ["symlink"],
        "tree": {
            "proj/.claude/rules/src.md": '---\npaths: "src/*.ts"\n---\n\nG6f src\n',
            "proj/src/.claude/rules/n.md": "G6f nested unscoped\n",
            "proj/src/a.ts": "",
            "alias": {"symlink": "proj"},
        },
        "probes": [{"read": ["alias/src/a.ts", "proj/src/a.ts"]}],
    }
)
cases.append(
    {
        "id": "G8-rule-read-through-link",
        "group": "G8",
        "cwd": "proj",
        "requires": ["symlink"],
        "tree": {
            "proj/shared/s.md": '---\npaths: "src/*.ts"\n---\n\nG8b shared scoped\n',
            "proj/.claude/rules/alias.md": {"symlink": "../../shared/s.md"},
            "proj/.claude/rules/md.md": '---\npaths: "*.md"\n---\n\nG8b matches its own path\n',
            "proj/src/a.ts": "",
        },
        "probes": [
            {"read": ["proj/.claude/rules/alias.md", "proj/src/a.ts"]},
            {"read": ["proj/.claude/rules/md.md"]},
        ],
    }
)
cases.append(
    {
        "id": "G4-shadowing",
        "group": "G4",
        "cwd": "A/proj",
        "requires": ["symlink"],
        "tree": {
            "A/proj/.claude/rules/y.md": "---\n"
            'paths: "src/*.ts"\n'
            "---\n"
            "\n"
            "G4b cwd rule linked from the ancestor\n",
            "A/.claude/rules/l.md": {"symlink": "../../proj/.claude/rules/y.md"},
            "A/proj/.claude/rules/s.md": "---\n"
            'paths: "pkg/src/*.ts"\n'
            "---\n"
            "\n"
            "G4b cwd rule linked from a nested dir\n",
            "A/proj/pkg/.claude/rules/l.md": {"symlink": "../../../.claude/rules/s.md"},
            "A/proj/src/a.ts": "",
            "A/proj/pkg/src/x.ts": "",
        },
        "probes": [{"read": ["A/proj/src/a.ts"]}, {"read": ["A/proj/pkg/src/x.ts"]}],
    }
)
cases.append(
    {
        "id": "G4-add-dir",
        "group": "G4",
        "cwd": "A/proj",
        "tree": {
            "A/.claude/rules/a-other.md": "---\n"
            'paths: "other/src/*.ts"\n'
            "---\n"
            "\n"
            "G4c ancestor rule for an added dir\n",
            "A/proj2/.claude/rules/x.md": "G4c sibling with the cwd as a string prefix\n",
            "A/proj/.keep": "",
            "A/other/src/a.ts": "",
            "A/proj2/src/a.ts": "",
        },
        "probes": [
            {"read": ["A/other/src/a.ts"], "args": ["--add-dir", "{root}/A/other"]},
            {"read": ["A/proj2/src/a.ts"], "args": ["--add-dir", "{root}/A/proj2"]},
        ],
    }
)
cases.append(
    {
        "id": "G5-bare-worktree",
        "group": "G5",
        "cwd": "B/wt",
        "requires": ["git"],
        "tree": {".claude/rules/parent.md": "G5b parent unscoped\n", "src/.keep": ""},
        "setup": [
            {"cwd": "src", "run": ["git", "init", "-q"]},
            {
                "cwd": "src",
                "run": [
                    "git",
                    "-c",
                    "user.email=oracle@example.com",
                    "-c",
                    "user.name=oracle",
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "init",
                ],
            },
            {"cwd": ".", "run": ["git", "clone", "-q", "--bare", "src", "B"]},
            {"cwd": "B", "run": ["git", "worktree", "add", "-q", "wt"]},
            {
                "cwd": "B",
                "run": [
                    "sh",
                    "-c",
                    "mkdir -p .claude/rules && printf 'G5b bare repository dir\\n' > "
                    ".claude/rules/b.md",
                ],
            },
            {"cwd": "B/wt", "run": ["sh", "-c", "printf 'probe\\n' > probe.txt"]},
        ],
        "probes": [{"read": ["B/wt/probe.txt"]}],
    }
)
cases.append(
    {
        "id": "G5-stale-worktree",
        "group": "G5",
        "cwd": "M/.claude/worktrees/w1",
        "requires": ["git"],
        "tree": {
            ".claude/rules/parent.md": "G5c parent unscoped\n",
            "M/.claude/rules/m-unscoped.md": "G5c main unscoped\n",
            "M/f": "",
        },
        "setup": [
            {"cwd": "M", "run": ["git", "init", "-q"]},
            {
                "cwd": "M",
                "run": [
                    "git",
                    "-c",
                    "user.email=oracle@example.com",
                    "-c",
                    "user.name=oracle",
                    "add",
                    ".",
                ],
            },
            {
                "cwd": "M",
                "run": [
                    "git",
                    "-c",
                    "user.email=oracle@example.com",
                    "-c",
                    "user.name=oracle",
                    "commit",
                    "-q",
                    "-m",
                    "init",
                ],
            },
            {"cwd": "M", "run": ["git", "worktree", "add", "-q", ".claude/worktrees/w1"]},
            {
                "cwd": "M",
                "run": ["sh", "-c", "printf '/nonexistent/w1/.git\\n' > .git/worktrees/w1/gitdir"],
            },
        ],
        "probes": [{"read": ["M/.claude/worktrees/w1/f"]}],
    }
)
cases.append(
    {
        "id": "G6-case-variant-target",
        "group": "G6",
        "cwd": "Proj",
        "requires": ["symlink", "case_sensitive_fs"],
        "tree": {
            "proj/shared.md": "G6g target in a case-variant sibling\n",
            "Proj/.claude/rules/l.md": {"symlink": "../../../proj/shared.md"},
            "Proj/zz-sentinel.txt": "sentinel\n",
        },
        "probes": [{"read": ["Proj/zz-sentinel.txt"]}],
    }
)
cases.append(
    {
        "id": "G1-non-utf8-name",
        "group": "G1",
        "cwd": "proj",
        "requires": ["posix_names"],
        "tree": {"proj/.claude/rules/ok.md": "G1b control\n", "proj/zz-sentinel.txt": "sentinel\n"},
        "setup": [
            {
                "cwd": "proj/.claude/rules",
                "run": [
                    "python3",
                    "-c",
                    "import os; open(b'\\xff.md', 'w').write('G1b non-UTF-8 file "
                    "name\\n'); os.mkdir(b'd\\xfe'); open(b'd\\xfe/in.md', "
                    "'w').write('G1b inside non-UTF-8 dir\\n')",
                ],
            }
        ],
        "probes": [{"read": ["proj/zz-sentinel.txt"]}],
    }
)
cases.append(
    {
        "id": "G6-pwd-spelling",
        "group": "G6",
        "cwd": "alias",
        "requires": ["symlink"],
        "tree": {
            "proj/.claude/rules/src.md": '---\npaths: "src/*.ts"\n---\n\nG6h src\n',
            "proj/src/a.ts": "",
            "alias": {"symlink": "proj"},
        },
        "probes": [{"read": ["alias/src/a.ts"], "env": {"PWD": "{root}/alias"}}],
    }
)

# G9: CLAUDE.local.md, loaded only with the `local` setting source ------------------
LOCAL = "project,local"
cases.append(
    {
        "id": "G9-local-levels",
        "group": "G9",
        "cwd": "A/B/proj",
        "tree": {
            "A/CLAUDE.local.md": "G9a ancestor local\n",
            "A/B/CLAUDE.local.md": "<!-- G9a comment only -->\n",
            "A/B/proj/CLAUDE.local.md": "G9a cwd local\n",
            "A/B/proj/.claude/CLAUDE.local.md": "G9a cwd .claude local\n",
            "A/B/proj/.claude/rules/r.md": "G9a cwd rule\n",
            "A/B/proj/pkg/CLAUDE.local.md": "G9a nested local\n",
            "A/B/proj/pkg/.claude/CLAUDE.local.md": "G9a nested .claude local\n",
            "A/B/proj/pkg/.claude/rules/r.md": "G9a nested rule\n",
            "A/B/proj/pkg/a.txt": "a\n",
            "A/B/proj/deep/CLAUDE.local.md": "G9a intermediate local\n",
            "A/B/proj/deep/sub/a.txt": "a\n",
            "A/B/proj/blank/CLAUDE.local.md": "\n  \n",
            "A/B/proj/blank/a.txt": "a\n",
            "A/B/other/CLAUDE.local.md": "G9a local beside the cwd\n",
            "A/B/other/a.txt": "a\n",
        },
        "probes": [
            {
                "read": ["A/B/proj/pkg/a.txt", "A/B/proj/deep/sub/a.txt", "A/B/proj/blank/a.txt"],
                "setting_sources": LOCAL,
            },
            {"read": ["A/B/proj/pkg/a.txt", "A/B/proj/deep/sub/a.txt", "A/B/proj/blank/a.txt"]},
            {"read": ["A/B/other/a.txt"], "setting_sources": LOCAL},
            {"read": ["A/B/proj/pkg/CLAUDE.local.md"], "setting_sources": LOCAL},
        ],
    }
)
cases.append(
    {
        "id": "G9-local-paths",
        "group": "G9",
        "cwd": "A/proj",
        "tree": {
            "A/CLAUDE.local.md": scoped('paths: "proj/src/**"', "G9b ancestor local scoped"),
            "A/proj/CLAUDE.local.md": scoped('paths: "src/**"', "G9b cwd local scoped"),
            "A/proj/pkg/CLAUDE.local.md": scoped('paths: "*.ts"', "G9b nested glob missing"),
            "A/proj/lib/CLAUDE.local.md": scoped('paths: "/b.ts"', "G9b nested own-dir glob"),
            "A/proj/doc/CLAUDE.local.md": scoped('paths: "/doc/c.ts"', "G9b nested cwd glob"),
            "A/proj/src/a.ts": "a\n",
            "A/proj/pkg/a.txt": "a\n",
            "A/proj/lib/b.ts": "b\n",
            "A/proj/doc/c.ts": "c\n",
        },
        "probes": [
            {
                "read": [
                    "A/proj/src/a.ts",
                    "A/proj/pkg/a.txt",
                    "A/proj/lib/b.ts",
                    "A/proj/doc/c.ts",
                ],
                "setting_sources": LOCAL,
            }
        ],
    }
)
cases.append(
    {
        "id": "G9-local-imports",
        "group": "G9",
        "cwd": "proj",
        "tree": {
            "proj/CLAUDE.local.md": (
                "G9c local with imports\n"
                "@notes/line.md\n"
                "See @notes/inline.md for more.\n"
                "Ask oracle@notes/attached.md about it.\n"
                "Code span: `@notes/span.md`\n"
                "\n"
                "```text\n"
                "@notes/fenced.md\n"
                "```\n"
                "\n"
                "    @notes/indented.md\n"
                "\n"
                "<!--\n"
                "@notes/commented.md\n"
                "-->\n"
            ),
            "proj/notes/line.md": "G9c line\n",
            "proj/notes/inline.md": "G9c inline\n",
            "proj/notes/attached.md": "G9c attached\n",
            "proj/notes/span.md": "G9c span\n",
            "proj/notes/fenced.md": "G9c fenced\n",
            "proj/notes/indented.md": "G9c indented\n",
            "proj/notes/commented.md": "G9c commented\n",
            "proj/zz-sentinel.txt": "sentinel\n",
        },
        "probes": [{"read": ["proj/zz-sentinel.txt"], "setting_sources": LOCAL}],
    }
)
cases.append(
    {
        "id": "G9-local-links",
        "group": "G9",
        "cwd": "proj",
        "requires": ["symlink"],
        "tree": {
            "root-local.md": "G9d ancestor local linked beside it\n",
            "CLAUDE.local.md": {"symlink": "root-local.md"},
            "outside/cwd-local.md": "G9d cwd local linked outside the cwd\n",
            "proj/CLAUDE.local.md": {"symlink": "../outside/cwd-local.md"},
            "outside/pkg-local.md": "G9d nested local linked outside the cwd\n",
            "proj/pkg/CLAUDE.local.md": {"symlink": "../../outside/pkg-local.md"},
            "proj/shared/lib-local.md": "G9d nested local linked inside the cwd\n",
            "proj/lib/CLAUDE.local.md": {"symlink": "../shared/lib-local.md"},
            "proj/broken/CLAUDE.local.md": {"symlink": "missing.md"},
            "proj/pkg/a.txt": "a\n",
            "proj/lib/a.txt": "a\n",
            "proj/broken/a.txt": "a\n",
        },
        "probes": [
            {
                "read": ["proj/pkg/a.txt", "proj/lib/a.txt", "proj/broken/a.txt"],
                "setting_sources": LOCAL,
            }
        ],
    }
)
cases.append(
    {
        "id": "G9-local-worktree",
        "group": "G9",
        "cwd": "M/.claude/worktrees/w1",
        "requires": ["git"],
        "tree": {
            "CLAUDE.local.md": "G9e local above the repository\n",
            "M/.gitignore": "CLAUDE.local.md\n",
            "M/CLAUDE.local.md": "G9e main repository local\n",
            "M/.claude/worktrees/CLAUDE.local.md": "G9e local between repository and worktree\n",
            "M/f": "f\n",
        },
        "setup": [
            {"cwd": "M", "run": ["git", "init", "-q"]},
            {"cwd": "M", "run": ["git", *git_env, "add", "."]},
            {"cwd": "M", "run": ["git", *git_env, "commit", "-q", "-m", "init"]},
            {"cwd": "M", "run": ["git", "worktree", "add", "-q", ".claude/worktrees/w1"]},
            {
                "cwd": "M/.claude/worktrees/w1",
                "run": ["sh", "-c", "printf 'G9e worktree local\\n' > CLAUDE.local.md"],
            },
        ],
        "probes": [{"read": ["M/.claude/worktrees/w1/f"], "setting_sources": LOCAL}],
    }
)

Path(sys.argv[1]).write_text(json.dumps({"cases": cases}, indent=1, ensure_ascii=True) + "\n")
