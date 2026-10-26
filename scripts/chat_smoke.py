"""Smoke-test the deployed ``POST /chat`` from a laptop — PLAN.md Phase 3's last box.

Reads ``DOCPIPE_API_URL`` and ``DOCPIPE_API_KEY`` from the environment — never
from a file, and the key is never printed. Three requests, in order:

1. a covered question on a fresh **synthetic** user → 200, a non-empty reply,
   at least one citation carrying ``max_evidence``, a ``conversationId``;
2. a follow-up carrying that ``conversationId`` → 200, the *same* id back;
3. an out-of-corpus question in a new conversation → the reply opens with
   ``NOT_COVERED_MARKER`` rather than a guess, and the guardrail did not fire.

Each turn prints wall time, the Lambda's ``latency_ms``, tokens, searches and
citations. ``--json PATH`` writes the run (synthetic id, numbers, the first
200 characters of each reply, no key) for the goal's ``state.json``.

**The user id is synthetic by construction.** It is minted here as
``smoke-<8 hex>`` and ``--user-id`` must match ``SYNTHETIC_USER_ID`` or the
script refuses before any request. This repo is public; a real health.studio
id in a smoke log would publish it.

Exit codes: 0 all three turns pass · 1 a turn failed (the reason is printed) ·
2 refused before any request (bad id, missing environment).

    DOCPIPE_API_URL=https://…/dev/chat DOCPIPE_API_KEY=… uv run python scripts/chat_smoke.py
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from docpipe_core.chat import NOT_COVERED_MARKER, SYNTHETIC_USER_ID

URL_VAR = "DOCPIPE_API_URL"
KEY_VAR = "DOCPIPE_API_KEY"

# Turn 1 is a question goal 01 measured as covered (3/3 passages on both KBs);
# turn 2 depends on turn 1's answer; turn 3 is outside the corpus and outside
# every denied topic, so a refusal there is a false positive worth seeing.
COVERED_QUESTION = (
    "What connects the neck to the shoulder, and why does forward head posture load it?"
)
FOLLOW_UP = "Which of those should I work on first, and how often?"
NOT_COVERED_QUESTION = "Which brand of running shoes should I buy for a marathon?"

REQUEST_TIMEOUT_S = 60  # the gateway cuts at 29 s; this only bounds a hung socket


def mint_user_id() -> str:
    return f"smoke-{secrets.token_hex(4)}"


@dataclass
class Turn:
    name: str
    status: int
    wall_ms: int
    payload: dict[str, Any]
    ok: bool = True
    reasons: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> None:
        self.ok = False
        self.reasons.append(reason)


def post(url: str, key: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], int]:
    """One request. Returns (status, parsed body, wall ms); a non-JSON body is wrapped."""
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"content-type": "application/json", "x-api-key": key},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    wall_ms = int((time.monotonic() - started) * 1000)
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        payload = {"raw": raw.decode("utf-8", "replace")[:500]}
    return status, payload if isinstance(payload, dict) else {"raw": payload}, wall_ms


# ── the three checks, as pure functions over a response ─────────────────────


def check_first_turn(turn: Turn) -> None:
    _check_ok_shape(turn)
    if not turn.ok:
        return
    citations = turn.payload.get("citations") or []
    if not citations:
        turn.fail("no citations — the verbatim seed should guarantee one retrieval")
    elif not any(c.get("max_evidence") is not None for c in citations):
        turn.fail("no citation carries max_evidence — the stamp did not reach the response")
    if turn.payload.get("guardrail_intervened"):
        turn.fail("guardrail intervened on the covered question")


def check_follow_up(turn: Turn, conversation_id: str) -> None:
    _check_ok_shape(turn)
    if turn.ok and turn.payload.get("conversationId") != conversation_id:
        turn.fail(
            f"conversationId changed: sent {conversation_id!r}, "
            f"got {turn.payload.get('conversationId')!r}"
        )


def check_not_covered(turn: Turn) -> None:
    _check_ok_shape(turn)
    if not turn.ok:
        return
    reply = str(turn.payload.get("reply", ""))
    if turn.payload.get("guardrail_intervened"):
        turn.fail("guardrail intervened on an out-of-corpus, non-medical question")
    elif NOT_COVERED_MARKER.lower() not in reply.lower():
        turn.fail(f"reply does not open with {NOT_COVERED_MARKER!r} — it guessed")


def _check_ok_shape(turn: Turn) -> None:
    if turn.status == 429:
        turn.fail("429 from the gateway — the usage plan's quota or throttle; not the Lambda")
        return
    if turn.status != 200:
        turn.fail(f"HTTP {turn.status}: {json.dumps(turn.payload)[:300]}")
        return
    if not str(turn.payload.get("reply", "")).strip():
        turn.fail("empty reply")
    if not turn.payload.get("conversationId"):
        turn.fail("no conversationId in the response")


# ── the run ─────────────────────────────────────────────────────────────────


def describe(turn: Turn) -> str:
    p = turn.payload
    citations = p.get("citations") or []
    stamped = sum(1 for c in citations if c.get("max_evidence") is not None)
    verdict = "ok" if turn.ok else "FAIL"
    line = (
        f"{turn.name}: HTTP {turn.status} · {verdict} · wall {turn.wall_ms} ms · "
        f"latency_ms={p.get('latency_ms')} · "
        f"tokens {p.get('input_tokens')}/{p.get('output_tokens')} · "
        f"tool_calls={p.get('tool_calls')} · citations={len(citations)} ({stamped} stamped) · "
        f"guardrail={p.get('guardrail_intervened')} · conversationId={p.get('conversationId')}"
    )
    for reason in turn.reasons:
        line += f"\n    ! {reason}"
    return line


def run(url: str, key: str, user_id: str) -> list[Turn]:
    status, payload, wall = post(url, key, {"userId": user_id, "message": COVERED_QUESTION})
    first = Turn("turn 1 (covered)", status, wall, payload)
    check_first_turn(first)
    print(describe(first))
    turns = [first]
    if not first.ok:
        return turns  # turn 2 needs turn 1's id; stop here, honestly

    conversation_id = str(first.payload["conversationId"])
    status, payload, wall = post(
        url, key, {"userId": user_id, "message": FOLLOW_UP, "conversationId": conversation_id}
    )
    second = Turn("turn 2 (follow-up)", status, wall, payload)
    check_follow_up(second, conversation_id)
    print(describe(second))
    turns.append(second)

    status, payload, wall = post(url, key, {"userId": user_id, "message": NOT_COVERED_QUESTION})
    third = Turn("turn 3 (not covered)", status, wall, payload)
    check_not_covered(third)
    print(describe(third))
    turns.append(third)
    return turns


def report(turns: list[Turn], user_id: str, url: str) -> dict[str, Any]:
    """What goes into the goal's state.json: numbers and shapes, no key, synthetic id only."""
    out: list[dict[str, Any]] = []
    for turn in turns:
        row = asdict(turn)
        payload = dict(turn.payload)
        payload["reply"] = str(payload.get("reply", ""))[:200]
        row["payload"] = payload
        out.append(row)
    return {
        "run_date": datetime.now(UTC).isoformat(timespec="seconds"),
        "api_url": url,
        "user_id": user_id,
        "passed": all(t.ok for t in turns) and len(turns) == 3,
        "turns": out,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--user-id", default=None, help="must match smoke-[a-f0-9]{8}; minted when omitted"
    )
    parser.add_argument("--json", default=None, help="write the run (no key) to this path")
    args = parser.parse_args(argv)

    user_id = args.user_id or mint_user_id()
    if not SYNTHETIC_USER_ID.match(user_id):
        print(
            f"refused: --user-id {user_id!r} is not synthetic (smoke-[a-f0-9]{{8}}); "
            "this repo is public"
        )
        return 2
    url, key = os.environ.get(URL_VAR), os.environ.get(KEY_VAR)
    if not url or not key:
        print(f"refused: set {URL_VAR} and {KEY_VAR} in the environment (never in a file)")
        return 2

    print(f"POST {url} as {user_id}")
    turns = run(url, key, user_id)
    result = report(turns, user_id, url)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2)
        print(f"wrote {args.json}")
    print("PASS" if result["passed"] else "FAIL")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
