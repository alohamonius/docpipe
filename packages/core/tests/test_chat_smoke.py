"""scripts/chat_smoke.py's judgement, without a network: the refusal and the three checks."""

from __future__ import annotations

from typing import Any

import chat_smoke  # scripts/chat_smoke.py — on sys.path via pyproject's pytest `pythonpath`
import pytest
from docpipe_core.chat import NOT_COVERED_MARKER, SYNTHETIC_USER_ID

GOOD: dict[str, Any] = {
    "reply": "The scalenes and upper trapezius; forward head posture lengthens the lever.",
    "citations": [
        {"source_path": "anatomy/neck.md", "max_evidence": 2, "verification": ["review"]},
        {"source_path": None, "max_evidence": None, "verification": None},
    ],
    "conversationId": "conv-1",
    "guardrail_intervened": False,
    "latency_ms": 9000,
    "input_tokens": 4000,
    "output_tokens": 300,
    "tool_calls": 2,
}


def turn(payload: dict[str, Any], status: int = 200) -> chat_smoke.Turn:
    return chat_smoke.Turn("t", status, 1234, payload)


# ── refusal before any request ──────────────────────────────────────────────


@pytest.mark.parametrize("user_id", ["user-1", "smoke-XYZ", "smoke-0badc0de1", "alice"])
def test_a_non_synthetic_user_id_is_refused_before_any_request(
    user_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(chat_smoke.URL_VAR, "https://example.invalid/dev/chat")
    monkeypatch.setenv(chat_smoke.KEY_VAR, "k")
    monkeypatch.setattr(chat_smoke, "post", lambda *a, **k: pytest.fail("a request was sent"))
    assert chat_smoke.main(["--user-id", user_id]) == 2


def test_a_missing_environment_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(chat_smoke.URL_VAR, raising=False)
    monkeypatch.delenv(chat_smoke.KEY_VAR, raising=False)
    monkeypatch.setattr(chat_smoke, "post", lambda *a, **k: pytest.fail("a request was sent"))
    assert chat_smoke.main([]) == 2


def test_the_minted_id_is_synthetic_by_construction() -> None:
    for _ in range(20):
        assert SYNTHETIC_USER_ID.match(chat_smoke.mint_user_id())


# ── the three checks ────────────────────────────────────────────────────────


def test_the_first_turn_needs_a_stamped_citation() -> None:
    ok = turn(GOOD)
    chat_smoke.check_first_turn(ok)
    assert ok.ok

    unstamped = turn({**GOOD, "citations": [{"source_path": "x", "max_evidence": None}]})
    chat_smoke.check_first_turn(unstamped)
    assert not unstamped.ok and "max_evidence" in unstamped.reasons[0]

    none = turn({**GOOD, "citations": []})
    chat_smoke.check_first_turn(none)
    assert not none.ok and "no citations" in none.reasons[0]

    blocked = turn({**GOOD, "guardrail_intervened": True})
    chat_smoke.check_first_turn(blocked)
    assert not blocked.ok and "guardrail" in blocked.reasons[0]


def test_the_follow_up_must_keep_the_conversation() -> None:
    same = turn(GOOD)
    chat_smoke.check_follow_up(same, "conv-1")
    assert same.ok

    changed = turn({**GOOD, "conversationId": "conv-2"})
    chat_smoke.check_follow_up(changed, "conv-1")
    assert not changed.ok and "conversationId changed" in changed.reasons[0]


def test_the_not_covered_turn_must_say_so_not_guess() -> None:
    honest = turn({**GOOD, "reply": f"{NOT_COVERED_MARKER}. I can't advise on shoe brands."})
    chat_smoke.check_not_covered(honest)
    assert honest.ok

    guess = turn({**GOOD, "reply": "Buy the Brand X Pegasus, it is the best marathon shoe."})
    chat_smoke.check_not_covered(guess)
    assert not guess.ok and "guessed" in guess.reasons[0]

    refused = turn({**GOOD, "guardrail_intervened": True})
    chat_smoke.check_not_covered(refused)
    assert not refused.ok and "guardrail" in refused.reasons[0]


@pytest.mark.parametrize(
    ("status", "fragment"),
    [(429, "usage plan"), (403, "HTTP 403"), (502, "HTTP 502"), (504, "HTTP 504")],
)
def test_a_non_200_names_the_layer(status: int, fragment: str) -> None:
    failed = turn({"message": "Forbidden"}, status=status)
    chat_smoke.check_first_turn(failed)
    assert not failed.ok and fragment in failed.reasons[0]


def test_the_report_truncates_replies_and_carries_no_key() -> None:
    long_reply = turn({**GOOD, "reply": "x" * 1000})
    result = chat_smoke.report([long_reply], "smoke-0badc0de", "https://example.invalid/dev/chat")
    assert len(result["turns"][0]["payload"]["reply"]) == 200
    assert result["user_id"] == "smoke-0badc0de"
    assert "key" not in str(result).lower().replace("x-api-key", "")
    assert result["passed"] is False  # one turn is not a full run
