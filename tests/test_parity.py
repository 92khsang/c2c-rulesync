"""Replay rule loads recorded from real Claude Code sessions.

tests/parity/cases.json describes file trees and the files a session reads.
scripts/claude_parity_oracle.py recorded, for each of them, which rule files
Claude Code 2.1.273 loaded at session start and after each read. These tests
rebuild every tree and check that c2c-rulesync predicts the same loads.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from c2c_rulesync.rules import RuleFinder, SessionRules

PARITY_DIR = Path(__file__).parent / "parity"
CASES_BYTES = (PARITY_DIR / "cases.json").read_bytes()
CASES: list[dict[str, Any]] = json.loads(CASES_BYTES)["cases"]
GOLDEN: dict[str, Any] = json.loads(
    (PARITY_DIR / "claude-code-2.1.273.json").read_text(encoding="utf-8")
)

# Recorded reads whose loads c2c-rulesync does not reproduce, keyed by (case id,
# probe key), with the loads after reads that c2c-rulesync produces instead.
LAZY_DIVERGENCES: dict[tuple[str, str], list[dict[str, Any]]] = {
    # Codex does not tell hooks about directories added with --add-dir, so a
    # file there loads no rule; Claude Code treats it as inside the session.
    ("G4-add-dir", "A/other/src/a.ts --add-dir {root}/A/other"): [],
    ("G4-add-dir", "A/proj2/src/a.ts --add-dir {root}/A/proj2"): [],
}


def probe_key(probe: dict[str, Any]) -> str:
    key = "+".join(probe["read"])
    if probe.get("cwd"):
        key += f" (cwd {probe['cwd']})"
    if probe.get("args"):
        key += " " + " ".join(probe["args"])
    if probe.get("env"):
        key += " " + " ".join(f"{name}={value}" for name, value in sorted(probe["env"].items()))
    return key


def unmet_requirement(case: dict[str, Any], root: Path) -> str | None:
    for requirement in case.get("requires", []):
        if requirement == "git" and shutil.which("git") is None:
            return "git is not installed"
        if requirement in ("posix_names", "case_sensitive_fs"):
            (root / "case-probe").touch()
            if (root / "CASE-PROBE").exists() or sys.platform == "darwin":
                return "needs a case-sensitive file system that accepts any byte in names"
    return None


PROBES = [
    pytest.param(case, probe, id=f"{case['id']}: {probe_key(probe)}")
    for case in CASES
    for probe in case["probes"]
]


def build_tree(root: Path, case: dict[str, Any]) -> None:
    for relative, content in case["tree"].items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, dict) and "symlink" in content:
            path.symlink_to(content["symlink"])
        elif isinstance(content, dict) and "base64" in content:
            path.write_bytes(base64.b64decode(content["base64"]))
        else:
            path.write_text(content, encoding="utf-8", newline="")
    for command in case.get("setup", []):
        subprocess.run(
            command["run"], cwd=root / command.get("cwd", "."), check=True, capture_output=True
        )


def canonical(root: Path, relative: str) -> str:
    """A recorded path, relative to the case root, with links resolved."""
    return os.path.relpath(os.path.realpath(root / relative), root)


def test_the_recording_is_for_the_current_cases() -> None:
    assert GOLDEN["cases_sha256"] == hashlib.sha256(CASES_BYTES).hexdigest(), (
        "tests/parity/cases.json changed; rerun scripts/claude_parity_oracle.py"
    )
    recorded = {
        (case_id, key) for case_id, case in GOLDEN["cases"].items() for key in case["probes"]
    }
    expected = {(case["id"], probe_key(probe)) for case in CASES for probe in case["probes"]}
    assert recorded == expected


@pytest.mark.parametrize(("case", "probe"), PROBES)
def test_rule_loads_match_claude_code(
    tmp_path: Path, case: dict[str, Any], probe: dict[str, Any]
) -> None:
    root = (tmp_path / "root").resolve()
    root.mkdir()
    reason = unmet_requirement(case, tmp_path)
    if reason is not None:
        pytest.skip(reason)
    build_tree(root, case)
    cwd = root / probe.get("cwd", case["cwd"])
    finder = RuleFinder(str(cwd), user_rules_dir=None)
    session = SessionRules()

    session_start = [
        {"file": os.path.relpath(rule.path, root)}
        for rule in session.take(finder.session_start_rules())
    ]
    lazy = []
    for read in probe["read"]:
        # Claude Code records the read before it loads the rules the read triggers.
        session.mark_read(str(root / read))
        for rule in session.take(finder.trigger_rules(str(root / read))):
            entry: dict[str, Any] = {
                "file": os.path.relpath(rule.path, root),
                "trigger": read,
                "load_reason": "path_glob_match" if rule.globs else "nested_traversal",
            }
            if rule.globs:
                entry["globs"] = list(rule.globs)
            lazy.append(entry)

    recorded = GOLDEN["cases"][case["id"]]["probes"][probe_key(probe)]
    assert "read_errors" not in recorded
    expected_lazy = LAZY_DIVERGENCES.get((case["id"], probe_key(probe)), recorded["lazy"])
    assert sorted_entries(session_start) == sorted_entries(
        [{"file": canonical(root, load["file"])} for load in recorded["session_start"]]
    )
    assert sorted_entries(lazy) == sorted_entries(
        [
            {
                **{k: v for k, v in load.items() if k != "memory_type"},
                "file": canonical(root, load["file"]),
            }
            for load in expected_lazy
        ]
    )


def sorted_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda entry: json.dumps(entry, sort_keys=True))
