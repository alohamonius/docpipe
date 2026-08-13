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

Deliberately static (AST over `pulumi/components/kb.py`) rather than a Pulumi
unit test: pulumi-aws lives in `pulumi/venv`, not the uv workspace, so importing
the component here would make the contract check depend on an optional install
— the same "checked by nothing, in practice" failure it exists to end. Reading
the AST also lets it scan for a splitter reached by *any* path, and it ignores
comments, which legitimately name the strategies as the thing being refused.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_KB_COMPONENT = _REPO_ROOT / "pulumi" / "components" / "kb.py"

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


@pytest.fixture(scope="module")
def kb_module() -> ast.Module:
    assert _KB_COMPONENT.is_file(), f"KB component not found at {_KB_COMPONENT}"
    return ast.parse(_KB_COMPONENT.read_text(), filename=str(_KB_COMPONENT))


@pytest.fixture(scope="module")
def data_source_call(kb_module: ast.Module) -> ast.Call:
    """The single `aws.bedrock.AgentDataSource(...)` call in the component."""
    calls = [
        node
        for node in ast.walk(kb_module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "AgentDataSource"
    ]
    assert len(calls) == 1, f"expected exactly one AgentDataSource, found {len(calls)}"
    return calls[0]


def test_data_source_chunking_strategy_is_none(data_source_call: ast.Call) -> None:
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
        "AgentDataSource has no vector_ingestion_configuration — Bedrock's default "
        "is FIXED_SIZE, so an absent block is a splitter, not a no-op"
    )

    chunking = _dict_items(ingestion).get("chunking_configuration")
    assert isinstance(chunking, ast.Dict), "chunking_configuration must be a dict literal"

    strategy = _dict_items(chunking).get("chunking_strategy")
    assert strategy == "NONE", (
        f"chunking_strategy is {strategy!r}, must be 'NONE'. A splitter strips the "
        f"stamped evidence header off every fragment after the first."
    )


def test_no_splitter_configuration_survives_anywhere(kb_module: ast.Module) -> None:
    """Not just the happy path — no dict key in the file names a splitter."""
    offenders = sorted(
        {
            key.value
            for node in ast.walk(kb_module)
            if isinstance(node, ast.Dict)
            for key in node.keys
            if isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and key.value in _SPLITTER_CONFIG_KEYS
        }
    )
    assert not offenders, f"splitter configuration present: {offenders}"


def test_no_splitter_strategy_named_in_the_data_source(data_source_call: ast.Call) -> None:
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
    assert not named, f"data source references splitter strategies: {named}"


def test_component_exposes_no_chunk_size_knobs(kb_module: ast.Module) -> None:
    """No caller can re-enable splitting by passing an argument."""
    init = next(
        (
            node
            for node in ast.walk(kb_module)
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        ),
        None,
    )
    assert init is not None, "KnowledgeBase.__init__ not found"

    args = init.args
    params = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    knobs = sorted(p for p in params if "chunk" in p.lower())
    assert not knobs, f"chunking knobs still reachable from the constructor: {knobs}"
