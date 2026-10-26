"""The body graph that is already in the corpus — parsed, indexed, traversable.

health.studio's ``body-graph-*`` chunks are not prose that happens to mention
relationships; they are a **hand-curated, typed, per-edge-graded graph** that
the exporter writes out as markdown. Every connection chunk declares a
``Connection group id``, every chain declares a ``Chain code``, and the edges
between them carry their own star rating — capped, per the corpus's own rule,
by *that edge's* sourcing rather than inherited from the chain's headline.

So graph retrieval here does **not** need a graph database, and it does not
need an LLM to infer entities: the entities and edges are already written down
and checked. What it needs is a lookup table. The whole graph serialises to
~50 KB of JSON — small enough to sit next to the corpus in S3, be fetched once
at cold start, and live in a dict thereafter.

**What is and is not parseable, measured 2026-08-21.** Only bullets carrying a
backtick id resolve to a chunk; the corpus's richest prose does not:

===========================================  =======  ==================
section                                      bullets  parseable
===========================================  =======  ==================
``Member groups, with per-edge evidence``     yes      id + grade
``Fascial chain membership``                  yes      id + grade
``Neural connections``                        yes      id + grade
``Supplies``                                  yes      id, no grade
``Key clinical connections``                  no       free prose
``Functional relationships``                  partly   prose names only
``Trigger-point referral``                    no       free prose
``Refers pain to (canonical regions)``        yes      **zone** ids, not
                                                       chunk ids — a facet,
                                                       not an expansion edge
===========================================  =======  ==================

The prose sections are deliberately left alone. Extracting edges from them
would mean guessing, which is the thing this module exists to avoid — an
inferred edge next to a ``[VERIFIED]`` one, indistinguishable downstream, is
worse than no edge.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, Field

from docpipe_core.retrieval import RetrievedPassage

# Section heading → the relation an edge parsed from it represents. Anything
# not listed here is prose and is skipped; see the module docstring.
RELATION_SECTIONS: dict[str, str] = {
    "Member groups, with per-edge evidence": "has_member",
    "Fascial chain membership": "in_chain",
    "Neural connections": "innervated_by",
    "Supplies": "supplies",
}

_NODE_PREFIX = "body-graph-"
# Six fields declare a node's addressable id, one per node kind (measured
# 2026-08-21: Connection group id 245, Structure id 31, Chain code 11, Nerve
# code 8, Referral source id 5, Joint id 1 — 301 of 304 chunks). Matched
# generically rather than by name so a seventh kind indexes itself.
_NODE_ALIAS = re.compile(r"^\*\*([A-Za-z][A-Za-z ]*?(?:code|id))\*\*\s*·\s*`([^`]+)`", re.M)
_REGION_LAYER = re.compile(r"\*\*Region\*\*\s*·\s*(\S+)\s*·\s*\*\*Layer\*\*\s*·\s*(\S+)")
_HEADING = re.compile(r"^###\s+(.+?)\s*$", re.M)
# `- Rectus abdominis (`abdominals`) — ★☆☆`, `- **Sciatic nerve** (`sciatic`) → … — ★★☆ [VERIFIED]`
#
# The id is case-SENSITIVE and must allow uppercase: chain codes are `FFL`,
# `SFL`, `DFL`. A lowercase-only pattern silently parsed zero `in_chain` edges
# across all 62 files that carry them — no error, just an empty relation.
_BULLET = re.compile(
    r"^-\s+(?P<label>.+?)\s*\(`(?P<id>[A-Za-z][A-Za-z0-9_-]*)`\)(?P<rest>.*)$", re.M
)


class GraphNode(BaseModel):
    """One corpus chunk, as a graph node. ``id`` is its chunk key."""

    id: str
    kind: str
    title: str | None = None
    #: Every id this chunk answers to — ``{"Connection group id": "abdominals"}``,
    #: ``{"Chain code": "FFL"}``. Edges name these, not the chunk key.
    aliases: dict[str, str] = Field(default_factory=dict)
    region: str | None = None
    layer: str | None = None


class GraphEdge(BaseModel):
    source: str
    target: str
    relation: str
    #: Star count on the edge itself, 0–3. ``None`` where the section carries
    #: no grading (``Supplies``) — which is NOT the same as a graded zero.
    evidence: int | None = None
    verified: bool = False


class GraphHop(BaseModel):
    """A neighbour reached by traversal, and the seed it was reached from."""

    node_id: str
    relation: str
    evidence: int | None = None
    verified: bool = False
    from_seed: str


class BodyGraph(BaseModel):
    nodes: dict[str, GraphNode] = Field(default_factory=dict)
    edges: list[GraphEdge] = Field(default_factory=list)

    def model_post_init(self, _: Any) -> None:
        self._by_source: dict[str, list[GraphEdge]] = {}
        for edge in self.edges:
            self._by_source.setdefault(edge.source, []).append(edge)

    def neighbours(
        self,
        node_id: str,
        *,
        relations: Iterable[str] | None = None,
        min_evidence: int | None = None,
    ) -> list[GraphEdge]:
        """Edges leaving ``node_id``, optionally filtered by type and grade.

        ``min_evidence`` drops ungraded edges too. That is deliberate: an
        ungraded edge is unknown, not zero, and a caller asking for "★★☆ or
        better" is asking about evidence it can see.
        """
        allowed = set(relations) if relations is not None else None
        out = []
        for edge in self._by_source.get(node_id, []):
            if allowed is not None and edge.relation not in allowed:
                continue
            if min_evidence is not None and (edge.evidence or 0) < min_evidence:
                continue
            out.append(edge)
        return out

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True)

    @classmethod
    def from_json(cls, blob: str | bytes) -> BodyGraph:
        return cls.model_validate(json.loads(blob))


def _node_id(chunk_key: str) -> str:
    """``graph/body-graph-connection--abdominals.md`` → the bare stem."""
    stem = chunk_key.rsplit("/", 1)[-1]
    return stem[:-3] if stem.endswith(".md") else stem


def _sections(text: str) -> dict[str, str]:
    """Split a chunk body on ``###`` headings. Content before the first is dropped."""
    out: dict[str, str] = {}
    marks = list(_HEADING.finditer(text))
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[mark.group(1)] = text[mark.end() : end]
    return out


def _stars(fragment: str) -> int | None:
    """★★☆ → 2. ``None`` when the fragment carries no rating at all."""
    filled = fragment.count("★")
    return filled if (filled or "☆" in fragment) else None


def build_graph(documents: Mapping[str, str]) -> BodyGraph:
    """Parse ``{chunk_key: markdown}`` into a graph.

    Pure — no filesystem, no S3, no network. Callers supply the documents from
    wherever they hold the corpus, which is what makes this unit-testable and
    keeps the sync CLI a thin shell.

    Edges whose target id resolves to no chunk are dropped rather than kept as
    dangling references: an edge you cannot fetch is not a retrieval edge, and
    silently returning half a hop would corrupt the measurement this exists to
    support. ``build_report`` surfaces how many were dropped.
    """
    nodes: dict[str, GraphNode] = {}
    raw: dict[str, str] = {}
    for chunk_key, text in documents.items():
        node_id = _node_id(chunk_key)
        if not node_id.startswith(_NODE_PREFIX):
            continue
        kind = node_id[len(_NODE_PREFIX) :].split("--", 1)[0]
        node = GraphNode(id=node_id, kind=kind)
        node.aliases = {field: value for field, value in _NODE_ALIAS.findall(text)}
        if m := _REGION_LAYER.search(text):
            node.region, node.layer = m.group(1), m.group(2)
        title = re.search(r"^##\s+(.+?)\s*$", text, re.M)
        node.title = title.group(1) if title else None
        nodes[node_id] = node
        raw[node_id] = text

    by_alias: dict[str, str] = {}
    for node in nodes.values():
        for alias in node.aliases.values():
            by_alias.setdefault(alias, node.id)

    edges: list[GraphEdge] = []
    for node_id, text in raw.items():
        for heading, body in _sections(text).items():
            relation = RELATION_SECTIONS.get(heading)
            if relation is None:
                continue
            for match in _BULLET.finditer(body):
                target = by_alias.get(match.group("id"))
                if target is None or target == node_id:
                    continue
                rest = match.group("rest")
                edges.append(
                    GraphEdge(
                        source=node_id,
                        target=target,
                        relation=relation,
                        evidence=_stars(rest),
                        verified="[VERIFIED]" in rest,
                    )
                )
    return BodyGraph(nodes=nodes, edges=edges)


def expand(
    passages: Iterable[RetrievedPassage],
    graph: BodyGraph,
    *,
    chunk_key_of: Any,
    relations: Iterable[str] | None = None,
    min_evidence: int | None = None,
    budget: int = 4,
) -> list[GraphHop]:
    """Neighbours of the retrieved seeds, best-graded first, deduped.

    Ordering is the honest part of this function. Hops arrive by **traversal**,
    so unlike seeds they carry no similarity score — there is nothing to rank
    them by that is commensurate with the seeds' vector distance. They are
    therefore ordered by the only signal that is actually about the edge: its
    evidence grade, then the rank of the seed it hung off. ``budget`` caps the
    total so a hub node cannot flood the context window.

    Consequence for measurement, stated here so it cannot be missed: an
    expanded run and an unexpanded one do **not** have comparable recall@k or
    MRR, because expansion changes both the size and the nature of the
    candidate list. Report it as its own instrument — the same rule PLAN.md
    already applies to agentic ``session_recall`` versus single-shot MRR.
    """
    seen: set[str] = set()
    scored: list[tuple[int, int, GraphHop]] = []
    for rank, passage in enumerate(passages):
        key = chunk_key_of(passage)
        if key is None:
            continue
        seed_id = _node_id(key)
        seen.add(seed_id)
        for edge in graph.neighbours(seed_id, relations=relations, min_evidence=min_evidence):
            scored.append(
                (
                    -(edge.evidence or 0),
                    rank,
                    GraphHop(
                        node_id=edge.target,
                        relation=edge.relation,
                        evidence=edge.evidence,
                        verified=edge.verified,
                        from_seed=seed_id,
                    ),
                )
            )
    scored.sort(key=lambda row: (row[0], row[1]))

    hops: list[GraphHop] = []
    for _, _, hop in scored:
        # Checked before appending, not after: the other order yields one hop
        # on budget=0, which is the difference between "expansion off" and
        # "expansion on with a small budget".
        if len(hops) >= budget:
            break
        if hop.node_id in seen:
            continue
        seen.add(hop.node_id)
        hops.append(hop)
    return hops


def build_report(documents: Mapping[str, str], graph: BodyGraph) -> dict[str, Any]:
    """Coverage numbers for the build, so a silent parse regression is visible."""
    kinds: dict[str, int] = {}
    for node in graph.nodes.values():
        kinds[node.kind] = kinds.get(node.kind, 0) + 1
    relations: dict[str, int] = {}
    for edge in graph.edges:
        relations[edge.relation] = relations.get(edge.relation, 0) + 1
    return {
        "documents": len(documents),
        "nodes": len(graph.nodes),
        "nodes_by_kind": dict(sorted(kinds.items())),
        "edges": len(graph.edges),
        "edges_by_relation": dict(sorted(relations.items())),
        "graded_edges": sum(1 for e in graph.edges if e.evidence is not None),
        "verified_edges": sum(1 for e in graph.edges if e.verified),
    }
