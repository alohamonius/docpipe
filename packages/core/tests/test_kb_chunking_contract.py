"""The chunking contract, asserted instead of commented.

health.studio's `pnpm kb:build` emits a **chunk-final** corpus: one file per
retrievable unit, each opening with a stamped header carrying the ★ evidence
scale, the [VERIFIED]/[UNVERIFIED] vocabulary and the "not a diagnosis" line.
Any splitter Bedrock applies on top of that cuts the header off every fragment
after the first, and the model then receives bare star ratings with no scale
attached. On health content that is a safety defect, not a tuning choice — and
"better splitter" (SEMANTIC, HIERARCHICAL) is not a fix, because the problem is
splitting at all.

The requirement was written down in three places — `build-kb.ts`'s header,
`MANIFEST.json.ingestion.note`, and a comment in `pulumi/components/kb.py` —
and enforced in none, while the deployed configuration said `FIXED_SIZE` for
its entire life. Two repos each wrote the rule down *for the other* and neither
could see the other's copy. This file is docpipe's half of the fix: the
consumer-side assertion. health.studio asserts the producer half.

Deliberately static (AST over the Pulumi components) rather than a Pulumi unit
test: pulumi-aws lives in `pulumi/venv`, not the uv workspace, so importing the
component here would make the contract check depend on an optional install — the
same "checked by nothing, in practice" failure it exists to end. Reading the AST
also lets it scan for a splitter reached by *any* path, and it ignores comments,
which legitimately name the strategies as the thing being refused.

**Every data source is discovered, not named.** The first version of this file
asserted "exactly one `AgentDataSource`, in `kb.py`". Adding the Aurora KB
(`kb_aurora.py`, Phase 5b) added a second one that the test would have ignored
completely while still passing green — a contract test that silently stops
covering new code is worse than none, because it reads as proof. So the file set
is globbed and every data source found in it must satisfy the contract. A third
KB is covered on the day it is written.

It still does **not** check what is deployed. `pulumi/components/kb.py` has said
`NONE` since `dbc3e8d` while the live data source has said `FIXED_SIZE` since
2026-08-12 — this test was green for all of it. Closing that needs a
`describe_data_source` check against the running stack (PLAN.md, Phase 3).
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPONENTS_DIR = _REPO_ROOT / "pulumi" / "components"

# The components that must exist and carry a data source. Named explicitly so a
# rename or deletion fails loudly here instead of quietly shrinking coverage.
_EXPECTED_COMPONENTS = {"kb.py", "kb_aurora.py"}

# Every per-strategy config block Bedrock accepts. None may appear.
_SPLITTER_CONFIG_KEYS = {
    "fixed_size_chunking_configuration",
    "hierarchical_chunking_configuration",
    "semantic_chunking_configuration",
}
_SPLITTER_STRATEGIES = {"FIXED_SIZE", "HIERARCHICAL", "SEMANTIC"}

_NON_LITERAL = "<non-literal>"


def _literal(node: ast.AST) -> object:
    """`ast.literal_eval`, but a computed value degrades to a marker.

    A splitter fed from a variable (`max_tokens=chunk_max_tokens`, as HEAD~ had
    it) must show up as a readable assertion failure, not a ValueError.
    """
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return _NON_LITERAL


def _dict_items(node: ast.AST) -> dict[str, object]:
    if not isinstance(node, ast.Dict):
        return {}
    out: dict[str, object] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            out[key.value] = value if isinstance(value, ast.Dict) else _literal(value)
    return out


@cache
def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _data_source_calls(module: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "AgentDataSource"
    ]


def _discover() -> list[tuple[Path, ast.Call]]:
    """Every (component file, data source) pair in the Pulumi tree."""
    found: list[tuple[Path, ast.Call]] = []
    for path in sorted(_COMPONENTS_DIR.glob("*.py")):
        for call in _data_source_calls(_parse(path)):
            found.append((path, call))
    return found


_DATA_SOURCES = _discover()
_DATA_SOURCE_IDS = [f"{p.name}#{i}" for i, (p, _) in enumerate(_DATA_SOURCES)]
_KB_COMPONENTS = sorted({p for p, _ in _DATA_SOURCES})


def test_discovery_actually_found_the_data_sources() -> None:
    """Guards the guard: a broken glob would make every test below vacuous.

    Without this, moving or renaming the components directory turns the entire
    contract suite green-by-emptiness — the failure mode that lets a splitter
    back in while CI reports success.
    """
    assert _DATA_SOURCES, (
        f"no AgentDataSource found under {_COMPONENTS_DIR} — the contract suite "
        f"would pass vacuously"
    )
    names = {p.name for p in _KB_COMPONENTS}
    assert names >= _EXPECTED_COMPONENTS, (
        f"expected data sources in {sorted(_EXPECTED_COMPONENTS)}, found {sorted(names)}. "
        f"If a KB component was renamed or removed, update _EXPECTED_COMPONENTS "
        f"deliberately — do not let coverage shrink by accident."
    )


@pytest.mark.parametrize(("path", "data_source_call"), _DATA_SOURCES, ids=_DATA_SOURCE_IDS)
def test_data_source_chunking_strategy_is_none(path: Path, data_source_call: ast.Call) -> None:
    """The corpus is chunk-final: one S3 object in, one vector out."""
    ingestion = next(
        (
            kw.value
            for kw in data_source_call.keywords
            if kw.arg == "vector_ingestion_configuration"
        ),
        None,
    )
    assert ingestion is not None, (
        f"{path.name}: AgentDataSource has no vector_ingestion_configuration — "
        f"Bedrock's default is FIXED_SIZE, so an absent block is a splitter, not a no-op"
    )

    chunking = _dict_items(ingestion).get("chunking_configuration")
    assert isinstance(chunking, ast.Dict), (
        f"{path.name}: chunking_configuration must be a dict literal"
    )

    strategy = _dict_items(chunking).get("chunking_strategy")
    assert strategy == "NONE", (
        f"{path.name}: chunking_strategy is {strategy!r}, must be 'NONE'. A splitter "
        f"strips the stamped evidence header off every fragment after the first."
    )


@pytest.mark.parametrize("path", _KB_COMPONENTS, ids=lambda p: p.name)
def test_no_splitter_configuration_survives_anywhere(path: Path) -> None:
    """Not just the happy path — no dict key in the file names a splitter."""
    offenders = sorted(
        {
            key.value
            for node in ast.walk(_parse(path))
            if isinstance(node, ast.Dict)
            for key in node.keys
            if isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and key.value in _SPLITTER_CONFIG_KEYS
        }
    )
    assert not offenders, f"{path.name}: splitter configuration present: {offenders}"


@pytest.mark.parametrize(("path", "data_source_call"), _DATA_SOURCES, ids=_DATA_SOURCE_IDS)
def test_no_splitter_strategy_named_in_the_data_source(
    path: Path, data_source_call: ast.Call
) -> None:
    """Comments may name the strategies (they explain the refusal); code may not."""
    named = sorted(
        {
            node.value
            for node in ast.walk(data_source_call)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _SPLITTER_STRATEGIES
        }
    )
    assert not named, f"{path.name}: data source references splitter strategies: {named}"


@pytest.mark.parametrize("path", _KB_COMPONENTS, ids=lambda p: p.name)
def test_component_exposes_no_chunk_size_knobs(path: Path) -> None:
    """No caller can re-enable splitting by passing an argument."""
    init = next(
        (
            node
            for node in ast.walk(_parse(path))
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        ),
        None,
    )
    assert init is not None, f"{path.name}: no __init__ found"

    args = init.args
    params = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    knobs = sorted(p for p in params if "chunk" in p.lower())
    assert not knobs, f"{path.name}: chunking knobs still reachable from the constructor: {knobs}"
