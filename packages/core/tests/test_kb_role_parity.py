"""Every knowledge base's service role must grant the same Bedrock actions.

`kb.py` and `kb_aurora.py` are two knowledge bases behind one retrieval
contract: `KnowledgeBaseClient` talks to whichever id it is given and cannot
tell them apart. Anything Bedrock does under the KB's *service role* — embed,
rerank — therefore has to be granted identically by both, or the same call
succeeds against one store and 403s against the other.

That is not hypothetical. Retrieve-with-rerank runs the rerank step under the
KB's role, `kb.py` learned it on 2026-08-16 and grew `bedrock:Rerank` +
`bedrock:InvokeModel` on the rerank model, and `kb_aurora.py` — written four
days later — did not inherit either. `retrieve()` defaults `rerank=True`, so
the Aurora store answered 403 to the DEFAULT retrieval path from the day it was
created until 2026-08-22, and nothing noticed: every Aurora measurement taken
in that window passed `rerank=False`, and the agentic loop supplies its own
tool arguments. The number the broken path would have produced turned out to be
identical to the working store's, so even a comparison would not have shown it.

**The class of bug is "a second implementation does not inherit the first one's
fixes".** A review cannot catch it, because the diff that introduces it is a
new file that looks complete on its own. Only a cross-component assertion can.

Static AST, for the same reason as `test_kb_chunking_contract.py`: pulumi-aws
lives in `infra/venv`, not the uv workspace, so importing the components here
would make this depend on an optional install — the same "checked by nothing,
in practice" failure it exists to end.

Scope, deliberately narrow: only `bedrock:*` actions are compared. The storage
grants SHOULD differ — `s3vectors:*` for one, `rds-data:*` and
`secretsmanager:GetSecretValue` for the other — and demanding those match would
be wrong. What must not differ is what Bedrock itself is allowed to do on the
caller's behalf.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPONENTS_DIR = _REPO_ROOT / "infra" / "components"


def _role_policy_calls(module: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "RolePolicy"
    ]


def _bedrock_actions(call: ast.Call) -> set[str]:
    """Every `bedrock:*` action named anywhere in a RolePolicy's document.

    Walks the whole call rather than tracing `policy=` through
    `pulumi.Output.json_dumps` — the statement list is built inline in both
    components, and an action string is an action string wherever it sits. A
    component that ever builds its policy elsewhere will read as zero actions
    and fail `test_every_kb_role_grants_some_bedrock_action` loudly.
    """
    return {
        node.value
        for node in ast.walk(call)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("bedrock:")
    }


def _discover() -> dict[str, set[str]]:
    """{component filename: bedrock actions its KB service role grants}.

    Globbed, not named: a third knowledge base is covered on the day it is
    written. `kb.py`/`kb_aurora.py` are asserted present separately, so a
    rename cannot quietly shrink this to one file — or zero.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(_COMPONENTS_DIR.glob("kb*.py")):
        module = ast.parse(path.read_text(), filename=str(path))
        actions: set[str] = set()
        for call in _role_policy_calls(module):
            actions |= _bedrock_actions(call)
        if actions:
            found[path.name] = actions
    return found


_ROLES = _discover()


def test_discovery_found_both_knowledge_bases() -> None:
    """Guards the guard. A broken glob makes every assertion below vacuous —
    two empty sets are trivially equal."""
    assert set(_ROLES) >= {"kb.py", "kb_aurora.py"}, (
        f"expected both KB components to declare a role policy, found {sorted(_ROLES)}"
    )


@pytest.mark.parametrize("component", sorted(_ROLES))
def test_every_kb_role_grants_some_bedrock_action(component: str) -> None:
    """At minimum each must invoke its embedding model; zero means the walk
    stopped finding what it used to."""
    assert _ROLES[component], f"{component} grants no bedrock:* action at all"


def test_all_knowledge_bases_grant_the_same_bedrock_actions() -> None:
    """The assertion this file exists for.

    Storage grants may differ and should. What Bedrock is permitted to do under
    the KB's own role may not — a caller cannot tell the stores apart, so a
    capability present on one and absent on the other is a 403 waiting for
    whoever switches `KB_ID`.
    """
    shared = set.intersection(*_ROLES.values())
    odd = {name: sorted(actions - shared) for name, actions in _ROLES.items() if actions - shared}
    assert not odd, (
        "knowledge-base roles disagree on Bedrock actions — a call that works "
        f"against one store will 403 against another: {odd}"
    )


def test_rerank_is_granted_everywhere_and_not_just_where_it_was_first_needed() -> None:
    """The specific regression, named so it cannot come back silently.

    `bedrock:Rerank` alone is not enough: it authorises the rerank *operation*,
    while invoking the rerank model is a separate `bedrock:InvokeModel` grant
    scoped to that model's ARN. `kb.py` carries both under `Sid`s
    `RerankRetrievedResults` and `InvokeRerankModel`; missing either reproduces
    the 403.
    """
    for component, actions in _ROLES.items():
        assert "bedrock:Rerank" in actions, (
            f"{component} cannot rerank — `retrieve()` defaults rerank=True, so this "
            "is the DEFAULT retrieval path, not an opt-in one"
        )
        assert "bedrock:InvokeModel" in actions, (
            f"{component} grants bedrock:Rerank but no bedrock:InvokeModel; the rerank "
            "model itself must be invocable or the operation still 403s"
        )
