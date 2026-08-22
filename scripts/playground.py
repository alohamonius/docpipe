"""Local playground ("tester window") for the chat path — PLAN.md Phase 5b.

A thin shell over ``docpipe_core``, same pattern as the eval CLIs: it wires a
browser page to ``ChatClient`` / ``KbAgent`` / ``KnowledgeBaseClient`` and adds
no retrieval or chat logic of its own. If it ever grows any, that logic moves
into ``packages/core`` with tests — the rule in README's repo layout.

Local because the AWS credentials live here. Once Phase 3's API exists, the
page can point at that instead and this script becomes redundant.

    make playground                     # http://127.0.0.1:8765
    make playground PORT=9000

Ids resolve from the dev stack, so it works right after ``make infra-up``;
every one can be overridden by flag or environment variable.

One asymmetry between the modes that the page reports rather than hides,
because a toggle that silently does nothing is worse than no toggle:

* **The agentic path ignores ``rerank``.** ``agent.py`` hardcodes
  ``rerank=False`` on its tool-side retrieve, so the checkbox applies to
  single-shot only.

RESOLVED 2026-08-23 — this used to list a second asymmetry: "the agentic path
applies no guardrail". It was true, and finding it was what this playground was
for. ``KbAgent`` now takes ``guardrail_id`` and sends ``guardrailConfig`` on
every Converse call, and the page passes the configured guardrail to both
modes, so they are guarded alike. An intervention surfaces as
``AgentSession.guardrail_intervened``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import webbrowser
from functools import cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from docpipe_core import ChatMessage, KbAgent, KnowledgeBaseClient, RetrievedPassage
from docpipe_core.agent import DEFAULT_AGENT_MODEL_ID
from docpipe_core.graph import RELATION_SECTIONS, BodyGraph, expand
from docpipe_core.kb_eval import chunk_key_of
from docpipe_core.kb_sync import DEFAULT_PREFIX
from docpipe_core.llm import ChatClient

_PAGE = Path(__file__).resolve().parent / "playground.html"
_DEFAULT_INFRA_DIR = Path(__file__).resolve().parents[1] / "infra"
_DEFAULT_GRAPH_INDEX = Path(__file__).resolve().parents[1] / ".scratch" / "graph-index.json"
_RELATIONS = sorted(set(RELATION_SECTIONS.values()))

# (stack output name, env var) — flags override both, in resolve_config below.
_SETTINGS = {
    "s3_kb_id": ("knowledge_base_id", "KNOWLEDGE_BASE_ID"),
    "aurora_kb_id": ("aurora_knowledge_base_id", "AURORA_KNOWLEDGE_BASE_ID"),
    "guardrail_id": ("guardrail_id", "GUARDRAIL_ID"),
    "guardrail_version": ("guardrail_version", "GUARDRAIL_VERSION"),
    "chat_model_id": ("chat_model_id", "CHAT_MODEL_ID"),
    "kb_source_bucket": ("kb_source_bucket", "KB_SOURCE_BUCKET"),
}


@cache
def _hop_text(bucket: str, chunk_key: str) -> str:
    """Fetch a hop target's body from the KB source bucket.

    Hops arrive as chunk keys, and a passage needs text. Reading it from the
    same bucket the knowledge base ingests from keeps one home for the corpus
    — the alternative, baking bodies into the index, would make it a second
    copy that silently goes stale. Cached, so a hub node is fetched once.
    """
    import boto3

    obj = boto3.client("s3").get_object(Bucket=bucket, Key=f"{DEFAULT_PREFIX}{chunk_key}")
    return str(obj["Body"].read().decode("utf-8"))


@cache
def _load_graph(path: str) -> BodyGraph | None:
    file = Path(path)
    if not file.is_file():
        return None
    return BodyGraph.from_json(file.read_text(encoding="utf-8"))


@cache
def _stack_outputs(infra_dir: str) -> dict[str, Any]:
    """Parse ``pulumi stack output --json``; empty dict if pulumi can't run.

    Fails soft on purpose — flags and env vars alone are enough to run this,
    and a laptop without the Pulumi backend logged in should still get a page.
    """
    env = {**os.environ}
    env.setdefault("PULUMI_CONFIG_PASSPHRASE", "")
    try:
        result = subprocess.run(
            ["pulumi", "stack", "output", "--json", "--cwd", infra_dir],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
    except (OSError, subprocess.CalledProcessError):
        return {}
    return dict(json.loads(result.stdout or "{}"))


def resolve_config(args: argparse.Namespace) -> dict[str, str | None]:
    """Flags → env vars → stack outputs. Never raises: the page reports gaps."""
    outputs = _stack_outputs(args.infra_dir)
    config: dict[str, str | None] = {}
    for key, (output_name, env_var) in _SETTINGS.items():
        value = getattr(args, key, None) or os.environ.get(env_var) or outputs.get(output_name)
        config[key] = str(value) if value else None
    config.setdefault("guardrail_version", None)
    config["guardrail_version"] = config["guardrail_version"] or "DRAFT"
    config["agent_model_id"] = args.agent_model_id or DEFAULT_AGENT_MODEL_ID
    config["graph_index"] = str(args.graph_index)
    graph = _load_graph(str(args.graph_index))
    config["graph_edges"] = str(len(graph.edges)) if graph else None
    config["graph_relations"] = ",".join(_RELATIONS)
    return config


def _kb_client(config: dict[str, str | None], store: str) -> KnowledgeBaseClient:
    """The Aurora KB needs the score-inversion flag; the S3 one must not have it."""
    if store == "aurora":
        kb_id = config["aurora_kb_id"]
        if not kb_id:
            raise ValueError(
                "no Aurora KB id — set --aurora-kb-id, $AURORA_KNOWLEDGE_BASE_ID, "
                "or run this where `pulumi stack output` resolves."
            )
        return KnowledgeBaseClient(kb_id, semantic_score_is_distance=True)
    kb_id = config["s3_kb_id"]
    if not kb_id:
        raise ValueError(
            "no S3 Vectors KB id — set --s3-kb-id, $KNOWLEDGE_BASE_ID, "
            "or run this where `pulumi stack output` resolves."
        )
    return KnowledgeBaseClient(kb_id)


def _passage_json(passage: Any) -> dict[str, Any]:
    return {
        "citation": passage.citation,
        "score": passage.score,
        "text": passage.text,
        "source": passage.source,
    }


def answer(config: dict[str, str | None], req: dict[str, Any]) -> dict[str, Any]:
    """One question → one response payload. Pure wiring; see the module docstring."""
    store = "aurora" if req.get("store") == "aurora" else "s3"
    mode = "agentic" if req.get("mode") == "agentic" else "single"
    question = (req.get("question") or "").strip()
    if not question:
        raise ValueError("empty question")

    top_k = max(1, min(20, int(req.get("top_k") or 5)))
    min_evidence = req.get("min_evidence")
    # S3 Vectors rejects HYBRID outright (FINDINGS 2026-08-20), so never send it.
    search_type = "HYBRID" if (req.get("hybrid") and store == "aurora") else None
    kb = _kb_client(config, store)

    notes: list[str] = []
    if mode == "agentic":
        if req.get("rerank"):
            notes.append("rerank ignored — agent.py hardcodes rerank=False")
        if not config.get("guardrail_id"):
            notes.append("no guardrail configured — set GUARDRAIL_ID to enforce one")
        agent = KbAgent(
            kb,
            model_id=config["agent_model_id"] or DEFAULT_AGENT_MODEL_ID,
            top_k=top_k,
            search_type=search_type,
            guardrail_id=config.get("guardrail_id") or None,
            guardrail_version=config.get("guardrail_version") or "DRAFT",
        )
        session = agent.ask(question)
        return {
            "answer": session.answer,
            "mode": mode,
            "store": store,
            "latency_ms": int(session.wall_s * 1000),
            "input_tokens": session.input_tokens,
            "output_tokens": session.output_tokens,
            "stop_reason": session.stop_reason,
            "hit_iteration_bound": session.hit_iteration_bound,
            "guardrail_intervened": session.guardrail_intervened,
            "tool_calls": [
                {
                    "query": call.query,
                    "min_evidence": call.min_evidence,
                    "passages": [_passage_json(p) for p in call.passages],
                }
                for call in session.tool_calls
            ],
            "passages": [],
            "notes": notes,
        }

    passages = kb.retrieve(
        question,
        top_k=top_k,
        min_evidence=min_evidence,
        rerank=bool(req.get("rerank")),
        search_type=search_type,
    )

    hops: list[dict[str, Any]] = []
    if req.get("graph_expand"):
        graph = _load_graph(str(config["graph_index"]))
        bucket = config["kb_source_bucket"]
        if graph is None:
            notes.append("no graph index — run `make graph-index`")
        elif not bucket:
            notes.append("no kb_source_bucket resolved — cannot fetch hop bodies")
        else:
            found = expand(
                passages,
                graph,
                chunk_key_of=chunk_key_of,
                relations=req.get("graph_relations") or None,
                min_evidence=req.get("graph_min_evidence"),
                budget=max(0, min(10, int(req.get("graph_budget") or 4))),
            )
            for hop in found:
                key = f"graph/{hop.node_id}.md"
                node = graph.nodes[hop.node_id]
                # No score: this passage arrived by traversal, not by vector
                # similarity, and inventing one would make the two look
                # commensurable in every downstream report.
                passages = [
                    *passages,
                    RetrievedPassage(
                        text=_hop_text(bucket, key),
                        source=f"s3://{bucket}/{DEFAULT_PREFIX}{key}",
                        doc_title=node.title,
                        section=node.kind,
                    ),
                ]
                hops.append(
                    {
                        "node_id": hop.node_id,
                        "title": node.title,
                        "relation": hop.relation,
                        "evidence": hop.evidence,
                        "verified": hop.verified,
                        "from_seed": hop.from_seed,
                    }
                )
            if not found:
                notes.append("graph expansion found no edges from these seeds")

    history = [ChatMessage(role=m["role"], content=m["content"]) for m in req.get("history") or []]
    history.append(ChatMessage(role="user", content=question))
    chat = ChatClient(
        model_id=config["chat_model_id"] or DEFAULT_AGENT_MODEL_ID,
        guardrail_id=config["guardrail_id"],
        guardrail_version=config["guardrail_version"] or "DRAFT",
    )
    if not config["guardrail_id"]:
        notes.append("no guardrail id resolved — running ungrounded of the safety layer")
    reply = chat.reply(history, passages=passages)
    return {
        "answer": reply.message,
        "mode": mode,
        "store": store,
        "latency_ms": reply.latency_ms,
        "input_tokens": reply.input_tokens,
        "output_tokens": reply.output_tokens,
        "guardrail_intervened": reply.guardrail_intervened,
        "stop_reason": None,
        "tool_calls": [],
        "passages": [_passage_json(p) for p in passages],
        "hops": hops,
        "notes": notes,
    }


def make_handler(config: dict[str, str | None]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
            if self.path in ("/", "/index.html"):
                self._send(200, _PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/config":
                self._json(200, dict(config))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
            if self.path != "/ask":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(length) or "{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "malformed JSON"})
                return
            try:
                self._json(200, answer(config, req))
            except Exception as exc:  # surface it in the page, keep serving
                self._json(200, {"error": f"{type(exc).__name__}: {exc}"})

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write(f"  {fmt % args}\n")

    return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--infra-dir", default=str(_DEFAULT_INFRA_DIR))
    parser.add_argument("--s3-kb-id", dest="s3_kb_id", help="else env / stack output")
    parser.add_argument("--aurora-kb-id", dest="aurora_kb_id", help="else env / stack output")
    parser.add_argument("--guardrail-id", dest="guardrail_id", help="else env / stack output")
    parser.add_argument("--guardrail-version", dest="guardrail_version", default=None)
    parser.add_argument("--chat-model-id", dest="chat_model_id", help="else env / stack output")
    parser.add_argument("--agent-model-id", dest="agent_model_id", default=None)
    parser.add_argument("--graph-index", type=Path, default=_DEFAULT_GRAPH_INDEX)
    parser.add_argument("--no-open", action="store_true", help="don't open a browser")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = resolve_config(args)

    url = f"http://{args.host}:{args.port}"
    print(f"docpipe playground → {url}")
    print(f"  S3 Vectors KB  {config['s3_kb_id'] or '— (unavailable)'}")
    print(f"  Aurora KB      {config['aurora_kb_id'] or '— (unavailable)'}")
    print(f"  chat model     {config['chat_model_id'] or DEFAULT_AGENT_MODEL_ID}")
    print(f"  agent model    {config['agent_model_id']}")
    guardrail = config["guardrail_id"] or "— NONE RESOLVED"
    print(f"  guardrail      {guardrail} (single-shot only; agentic sends none)")
    edges = config["graph_edges"]
    print(f"  graph index    {edges + ' edges' if edges else '— none (run `make graph-index`)'}")
    if not (config["s3_kb_id"] or config["aurora_kb_id"]):
        print("\n  No KB id resolved — is the dev stack selected?")
        print("  Try: pulumi -C infra stack select dev")
    print("\nCtrl-C to stop.\n")

    if not args.no_open:
        webbrowser.open(url)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(config))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
