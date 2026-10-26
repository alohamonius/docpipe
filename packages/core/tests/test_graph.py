"""The body graph: parsing the corpus's own edges, and traversing them."""

from docpipe_core.graph import BodyGraph, build_graph, build_report, expand
from docpipe_core.retrieval import RetrievedPassage

# Trimmed to the shape that matters, but every construct here is copied from
# the live corpus: the `·` field separator, the star scale, the uppercase chain
# code, the `[VERIFIED]` stamp, and the free-prose section that must be ignored.
CONNECTION = """\
# Body Connection Graph — verified edges
## Rectus abdominis

**Connection group id** · `abdominals`
**Region** · core · **Layer** · muscle

### Fascial chain membership
- Front Functional Line (`FFL`) — ★☆☆
- Superficial Front Line (`SFL`) — ★★☆

### Key clinical connections
Part of the FFL bridge — right pec major → rectus abdominis → left adductor
longus. Prose, no ids: this section must contribute no edges.

### Neural connections
- **Sciatic nerve** (`sciatic`) → left_foot, right_foot — ★★☆ [VERIFIED]
"""

CHAIN_FFL = """\
## Front Functional Line

**Chain code** · `FFL`

### Member groups, with per-edge evidence
- Rectus abdominis (`abdominals`) — ★☆☆
- Pectoralis major (`pectoralis_major`) — ★☆☆
"""

NERVE = """\
## Sciatic nerve

**Nerve code** · `sciatic`

### Supplies
- Forearm flexors (`forearm_flexors`)
"""

FLEXORS = "## Forearm flexors\n\n**Connection group id** · `forearm_flexors`\n"
PEC = "## Pectoralis major\n\n**Connection group id** · `pectoralis_major`\n"

CORPUS = {
    "graph/body-graph-connection--abdominals.md": CONNECTION,
    "graph/body-graph-chain--FFL.md": CHAIN_FFL,
    "graph/body-graph-nerve--sciatic.md": NERVE,
    "graph/body-graph-connection--forearm_flexors.md": FLEXORS,
    "graph/body-graph-connection--pectoralis_major.md": PEC,
}


def graph() -> BodyGraph:
    return build_graph(CORPUS)


def test_every_id_declaring_field_registers_an_alias() -> None:
    """Six fields name a node across the corpus; matching them by name would rot."""
    g = graph()
    assert g.nodes["body-graph-connection--abdominals"].aliases == {
        "Connection group id": "abdominals"
    }
    assert g.nodes["body-graph-chain--FFL"].aliases == {"Chain code": "FFL"}
    assert g.nodes["body-graph-nerve--sciatic"].aliases == {"Nerve code": "sciatic"}


def test_uppercase_chain_codes_resolve() -> None:
    """Regression: a lowercase-only id pattern parsed ZERO in_chain edges.

    It failed silently across all 62 files carrying that section — no error,
    just a relation that was quietly always empty. Chain codes are `FFL`,
    `SFL`, `DFL`; the corpus is the only place that says so.
    """
    edges = graph().neighbours("body-graph-connection--abdominals", relations=["in_chain"])
    assert {e.target for e in edges} == {"body-graph-chain--FFL"}, (
        "SFL has no chunk in this fixture, so only FFL should survive resolution"
    )
    assert edges[0].evidence == 1


def test_prose_sections_contribute_no_edges() -> None:
    """`Key clinical connections` is narrative. Guessing edges from it is the
    failure mode this module exists to avoid."""
    relations = {e.relation for e in graph().edges}
    assert relations == {"in_chain", "has_member", "innervated_by", "supplies"}


def test_edges_to_chunks_that_do_not_exist_are_dropped() -> None:
    """SFL is referenced but has no chunk. A hop you cannot fetch is not an edge."""
    targets = {e.target for e in graph().edges}
    assert "body-graph-chain--SFL" not in targets
    assert all(t in graph().nodes for t in targets)


def test_the_verified_stamp_and_star_grade_survive_onto_the_edge() -> None:
    edge = graph().neighbours("body-graph-connection--abdominals", relations=["innervated_by"])[0]
    assert edge.target == "body-graph-nerve--sciatic"
    assert edge.evidence == 2
    assert edge.verified is True


def test_ungraded_edges_are_none_not_zero() -> None:
    """`Supplies` carries no stars. Unknown is not the same as graded zero, and
    min_evidence must not treat it as one."""
    edge = graph().neighbours("body-graph-nerve--sciatic")[0]
    assert edge.evidence is None
    assert graph().neighbours("body-graph-nerve--sciatic", min_evidence=1) == []


def test_min_evidence_filters_by_edge_grade() -> None:
    g = graph()
    node = "body-graph-connection--abdominals"
    assert len(g.neighbours(node, min_evidence=1)) == 2  # FFL ★1, sciatic ★2
    assert len(g.neighbours(node, min_evidence=2)) == 1  # sciatic only
    assert g.neighbours(node, min_evidence=3) == []


def _passage(uri: str) -> RetrievedPassage:
    return RetrievedPassage(text="…", source=f"s3://bucket/corpus/{uri}")


def _key_of(passage: RetrievedPassage) -> str | None:
    assert passage.source is not None
    return passage.source.split("/corpus/", 1)[1]


def test_expand_orders_by_edge_grade_then_seed_rank() -> None:
    """Hops have no similarity score, so the only honest ranking signal is the
    edge's own evidence grade, then how well its seed scored."""
    hops = expand(
        [_passage("graph/body-graph-connection--abdominals.md")],
        graph(),
        chunk_key_of=_key_of,
    )
    assert [h.node_id for h in hops] == [
        "body-graph-nerve--sciatic",  # ★★☆ outranks…
        "body-graph-chain--FFL",  # …★☆☆
    ]
    assert hops[0].from_seed == "body-graph-connection--abdominals"


def test_expand_never_returns_a_seed_and_never_repeats_a_hop() -> None:
    seeds = [
        _passage("graph/body-graph-connection--abdominals.md"),
        _passage("graph/body-graph-chain--FFL.md"),
    ]
    hops = expand(seeds, graph(), chunk_key_of=_key_of)
    ids = [h.node_id for h in hops]
    assert "body-graph-chain--FFL" not in ids, "FFL is a seed; re-adding it wastes budget"
    assert "body-graph-connection--abdominals" not in ids
    assert len(ids) == len(set(ids))


def test_budget_caps_the_expansion() -> None:
    seeds = [_passage("graph/body-graph-chain--FFL.md")]
    assert len(expand(seeds, graph(), chunk_key_of=_key_of, budget=1)) == 1
    assert expand(seeds, graph(), chunk_key_of=_key_of, budget=0) == []


def test_passages_with_no_resolvable_chunk_key_are_skipped() -> None:
    hops = expand([RetrievedPassage(text="…", source=None)], graph(), chunk_key_of=lambda _: None)
    assert hops == []


def test_round_trip_through_json_keeps_traversal_working() -> None:
    """The adjacency map is built in model_post_init, so it must survive a load."""
    restored = BodyGraph.from_json(graph().to_json())
    assert restored.neighbours("body-graph-connection--abdominals", relations=["in_chain"])


def test_build_report_counts_what_a_parse_regression_would_change() -> None:
    report = build_report(CORPUS, graph())
    assert report["nodes"] == 5
    assert report["edges_by_relation"]["in_chain"] == 1
    assert report["verified_edges"] == 1
    # 4 of the 5 edges carry stars; `Supplies` is the ungraded one.
    assert report["graded_edges"] == 4
