"""The Bun.YAML predictor against answers recorded from Bun itself.

tests/vectors/bun_yaml.json is produced by scripts/gen_yaml_vectors.mjs; see
that script for the three document sets.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from c2c_rulesync import bun_yaml

VECTORS_PATH = Path(__file__).parent / "vectors" / "bun_yaml.json"
VECTORS: list[dict[str, Any]] = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["vectors"]


def _normalized(value: Any) -> Any:
    """Make recorded JSON values and parsed Python values comparable."""
    if isinstance(value, dict) and set(value) == {"number"}:
        return ("number", value["number"])
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, float) and math.isnan(value):
        return ("number", "NaN")
    if isinstance(value, float) and math.isinf(value):
        return ("number", "Infinity" if value > 0 else "-Infinity")
    if isinstance(value, (int, float)):
        return ("number", float(value))
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalized(item) for key, item in value.items()}
    raise TypeError(value)


def _outcomes(vector_set: str) -> tuple[list[str], int, int]:
    wrong = []
    unsupported = exact = 0
    for vector in VECTORS:
        if vector["set"] != vector_set:
            continue
        expected = ("ok", _normalized(vector["value"])) if vector["ok"] else ("error", None)
        try:
            actual = ("ok", _normalized(bun_yaml.parse(vector["doc"])))
        except bun_yaml.YamlError:
            actual = ("error", None)
        except bun_yaml.UnsupportedYaml:
            unsupported += 1
            continue
        if actual == expected:
            exact += 1
        else:
            wrong.append(f"{vector['doc']!r}: expected {expected}, got {actual}")
    return wrong, unsupported, exact


@pytest.mark.parametrize("vector_set", ["hand", "layout", "random"])
def test_no_document_gets_a_wrong_answer(vector_set: str) -> None:
    wrong, unsupported, exact = _outcomes(vector_set)

    assert exact > 0
    assert not wrong, "\n".join(wrong[:25])
    # Unsupported documents fall back to a line-by-line reading with a
    # warning, so their share is bounded rather than required to be zero.
    assert unsupported <= 0.07 * (exact + unsupported), (unsupported, exact)


def test_vector_sets_are_present() -> None:
    counts = Counter(vector["set"] for vector in VECTORS)

    assert counts["hand"] > 200
    assert counts["layout"] > 400
    assert counts["random"] > 2500


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ('paths:\n  - "src/**/*.ts"\n  - "lib/**"', {"paths": ["src/**/*.ts", "lib/**"]}),
        ("paths:\n- src/**\n- docs/*.md", {"paths": ["src/**", "docs/*.md"]}),
        ('paths: ["src/**", "*.md"]', {"paths": ["src/**", "*.md"]}),
        ("paths: src/**/*.{ts,tsx}", {"paths": "src/**/*.{ts,tsx}"}),
        (
            "description: Frontend rules\npaths: src/**",
            {"description": "Frontend rules", "paths": "src/**"},
        ),
        ("paths: src/** # frontend", {"paths": "src/**"}),
        ("paths:\r\n  - a\r\n  - b\r\n", {"paths": ["a", "b"]}),
        ("description: |\n  Two\n  lines\npaths: a", {"description": "Two\nlines\n", "paths": "a"}),
    ],
)
def test_common_front_matter_is_parsed_exactly(document: str, expected: Any) -> None:
    assert bun_yaml.parse(document) == expected


@pytest.mark.parametrize(
    "document",
    [
        "paths: *.ts",
        "paths:\n  - **/*.ts",
        "paths: {a,b}.ts",
        "description: Rules for: the API",
        "paths:\n\t- a",
        "paths: @generated",
    ],
)
def test_documents_bun_rejects_raise_yaml_error(document: str) -> None:
    with pytest.raises(bun_yaml.YamlError):
        bun_yaml.parse(document)
