"""The chunking contract, asserted against what is actually DEPLOYED.

`test_kb_chunking_contract.py` reads the Pulumi source with an AST and proves
the *code* never asks for a splitter. It says so itself, in its own docstring:

    It still does **not** check what is deployed. `pulumi/components/kb.py` has
    said `NONE` since `dbc3e8d` while the live data source has said
    `FIXED_SIZE` since 2026-08-12 — this test was green for all of it.

That gap is this file. Everything both repos check is *source code*: docpipe
asserts its Pulumi component, health.studio asserts its build manifest, and a
console edit to the deployed data source tomorrow passes every test in both.
The cloud was read by hand on 2026-08-16 and agreed — but a hand reading is a
measurement, not a check, and it expires the moment somebody clicks something.

Delegated here by health.studio's `kb-retrieval-readiness/01-ingestion-contract`
(closed 5/5; all four instruments its `done_when` named were discharged, and
holding it open for an instrument the text never named would have made it a
goal that closes on nothing). `05-chunk-shape` is its natural owner because 05
runs the first live round-trip and is therefore the first goal with any reason
to read the deployed configuration at all. docpipe PLAN.md Phase 3.

## Why this test is opt-in, and why that is not a loophole

It needs real credentials, so it cannot run in the default `make test` (which
`conftest.py` deliberately poisons with `AWS_ACCESS_KEY_ID=testing` so no unit
test can ever reach a live account by accident). An opt-in test that nobody
opts into is a proof nobody runs — so:

  * the skip is LOUD and names the exact command that would have run it, rather
    than vanishing into pytest's dot output;
  * `make kb-live-check` is that command, so the gate has a name;
  * `test_the_live_check_is_reachable` runs ALWAYS, with no credentials, and
    fails if the opt-in switch or the resolution path it documents has rotted.
    A skipped test whose *runner* is broken is the failure mode that let
    `FIXED_SIZE` live for four days.

Run it:

    make kb-live-check          # resolves ids from `pulumi stack output`
    DOCPIPE_LIVE_STACK=1 KNOWLEDGE_BASE_ID=… KB_DATA_SOURCE_ID=… \
        AWS_PROFILE=docpipe uv run pytest packages/core/tests/test_kb_data_source_live.py
"""

from __future__ import annotations

import os
from typing import Any

import pytest

# The switch. Present-and-truthy is the only thing that lets this file talk to
# an AWS account; absent means skip, loudly.
LIVE_SWITCH = "DOCPIPE_LIVE_STACK"

# Ids come from the environment because that is what `make kb-live-check` fills
# from `pulumi stack output --json` — the same resolution order
# `services/kb_sync/sync.py` documents (flags → env → stack output).
KB_ID_VAR = "KNOWLEDGE_BASE_ID"
DS_ID_VAR = "KB_DATA_SOURCE_ID"
REGION_VAR = "AWS_REGION"
DEFAULT_REGION = "us-east-1"

HOW_TO_RUN = (
    "set DOCPIPE_LIVE_STACK=1 and run `make kb-live-check` "
    "(or pass KNOWLEDGE_BASE_ID / KB_DATA_SOURCE_ID and AWS_PROFILE yourself)"
)

# Every per-strategy config block Bedrock accepts. None may appear in the
# DEPLOYED configuration either — the same set the AST test forbids in source,
# in Bedrock's API casing. Kept as one tuple of (api_key, source_key) pairs so
# the two spellings cannot drift apart into two half-lists.
SPLITTER_CONFIG_KEYS: tuple[tuple[str, str], ...] = (
    ("fixedSizeChunkingConfiguration", "fixed_size_chunking_configuration"),
    ("hierarchicalChunkingConfiguration", "hierarchical_chunking_configuration"),
    ("semanticChunkingConfiguration", "semantic_chunking_configuration"),
)


@pytest.fixture(autouse=True)
def aws_credentials() -> None:
    """Neutralise `conftest.py`'s autouse credential poisoning.

    That fixture sets `AWS_ACCESS_KEY_ID=testing` for every test in this
    package, which is exactly right for the moto suite and exactly wrong here:
    without this override the live check would authenticate as nobody and fail
    with a credentials error that looks like a stack problem.
    """
    return None


def _live_client() -> Any:
    """The bedrock-agent client, or a skip that says how to get one."""
    if not os.environ.get(LIVE_SWITCH):
        pytest.skip(f"live-stack check is opt-in — {HOW_TO_RUN}")
    missing = [var for var in (KB_ID_VAR, DS_ID_VAR) if not os.environ.get(var)]
    if missing:
        pytest.skip(f"{LIVE_SWITCH} is set but {', '.join(missing)} is not — {HOW_TO_RUN}")

    import boto3

    return boto3.client("bedrock-agent", region_name=os.environ.get(REGION_VAR, DEFAULT_REGION))


def _deployed_data_source() -> dict[str, Any]:
    client = _live_client()
    return client.get_data_source(
        knowledgeBaseId=os.environ[KB_ID_VAR],
        dataSourceId=os.environ[DS_ID_VAR],
    )["dataSource"]


def test_the_live_check_is_reachable() -> None:
    """Guards the guard — runs with no credentials, on every `make test`.

    The AST suite has `test_discovery_actually_found_the_data_sources` for the
    same reason: a contract test that silently stops covering anything is worse
    than no test, because it reads as proof. Here the thing that can rot is the
    OPT-IN itself. If the switch name drifts out of the Makefile, or the
    resolution path stops being documented, this fails in the default gate
    where somebody will see it — instead of the live check quietly never
    running again.
    """
    from pathlib import Path

    makefile = (Path(__file__).resolve().parents[3] / "Makefile").read_text()
    assert "kb-live-check" in makefile, (
        "the `make kb-live-check` target is gone — the only named way to run the "
        "live assertion. An opt-in test with no documented invocation is a test "
        "nobody runs."
    )
    for var in (LIVE_SWITCH, KB_ID_VAR, DS_ID_VAR):
        assert var in makefile, f"{var} is no longer wired into `make kb-live-check`"
    # And the skip must be able to explain itself, whatever the switch is called.
    assert LIVE_SWITCH in HOW_TO_RUN and "kb-live-check" in HOW_TO_RUN


def test_deployed_chunking_strategy_is_none() -> None:
    """The DEPLOYED data source does not split. Not the code — the cloud.

    The corpus is chunk-final: `pnpm kb:build` emits one file per retrievable
    unit, each opening with the stamped ★ scale and the "not a diagnosis" line.
    Any splitter Bedrock applies on top cuts that header off every fragment
    after the first, and the model then gets bare star glyphs with no legend.
    On health content that is a safety defect (constitution §1/§2), not a
    tuning choice.
    """
    ingestion = _deployed_data_source().get("vectorIngestionConfiguration")
    assert ingestion is not None, (
        "the deployed data source has no vectorIngestionConfiguration — Bedrock's "
        "default is FIXED_SIZE, so an absent block is a splitter, not a no-op"
    )
    chunking = ingestion.get("chunkingConfiguration")
    assert chunking is not None, (
        "the deployed data source has no chunkingConfiguration — see above, absent is not neutral"
    )
    assert chunking.get("chunkingStrategy") == "NONE", (
        f"deployed chunkingStrategy is {chunking.get('chunkingStrategy')!r}, must be "
        f"'NONE'. The Pulumi source can say NONE while the account says otherwise — "
        f"that is precisely the four-day gap this test exists to close."
    )


@pytest.mark.parametrize(
    ("api_key", "source_key"), SPLITTER_CONFIG_KEYS, ids=[k for _, k in SPLITTER_CONFIG_KEYS]
)
def test_no_splitter_configuration_is_deployed(api_key: str, source_key: str) -> None:
    """No per-strategy block survives in the deployed config, under any name.

    `chunkingStrategy: NONE` with a leftover `fixedSizeChunkingConfiguration`
    beside it is a configuration one console click away from splitting again,
    and the goal's `done_when` names this key explicitly.
    """
    chunking = (_deployed_data_source().get("vectorIngestionConfiguration") or {}).get(
        "chunkingConfiguration"
    ) or {}
    assert api_key not in chunking, (
        f"deployed chunkingConfiguration still carries {api_key} "
        f"(`{source_key}` in the Pulumi source): a splitter left loaded, not fired"
    )
