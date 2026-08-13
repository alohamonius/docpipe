import pytest
from docpipe_core.llm import HEALTH_ASSISTANT_SYSTEM, ChatClient
from docpipe_core.models import ChatMessage
from helpers import FakeBedrock, converse_response


def test_multi_turn_reply() -> None:
    fake = FakeBedrock([converse_response("Rest 48h between heavy sessions.")])
    client = ChatClient(bedrock_client=fake)

    history = [
        ChatMessage(role="user", content="How often should I deadlift?"),
        ChatMessage(role="assistant", content="Twice a week works for most."),
        ChatMessage(role="user", content="And how much rest between sessions?"),
    ]
    reply = client.reply(history)

    assert reply.message == "Rest 48h between heavy sessions."
    assert reply.input_tokens == 120

    call = fake.calls[0]
    assert call["system"] == [{"text": HEALTH_ASSISTANT_SYSTEM}]
    assert len(call["messages"]) == 3
    assert call["messages"][1]["role"] == "assistant"


def test_reasoning_scratchpad_is_stripped() -> None:
    fake = FakeBedrock([converse_response("<think>hmm</think>Do mobility work.")])
    client = ChatClient(bedrock_client=fake)
    reply = client.reply([ChatMessage(role="user", content="Tight hips?")])
    assert reply.message == "Do mobility work."


def test_history_must_end_with_user_message() -> None:
    client = ChatClient(bedrock_client=FakeBedrock([]))
    with pytest.raises(ValueError, match="end with a user message"):
        client.reply([ChatMessage(role="assistant", content="hi")])
    with pytest.raises(ValueError, match="end with a user message"):
        client.reply([])


def test_custom_system_prompt() -> None:
    fake = FakeBedrock([converse_response("ok")])
    client = ChatClient(bedrock_client=fake)
    client.reply([ChatMessage(role="user", content="hi")], system="Custom rules.")
    assert fake.calls[0]["system"] == [{"text": "Custom rules."}]


# ── Guardrail ──────────────────────────────────────────────────────────────
# A guardrail provisioned in the account applies only to calls that name it.
# These assert the call names it, and that an intervention is visible to the
# caller — otherwise a blocked answer is indistinguishable from a real one.


def test_no_guardrail_config_when_unset() -> None:
    fake = FakeBedrock([converse_response("ok")])
    ChatClient(bedrock_client=fake).reply([ChatMessage(role="user", content="hi")])
    assert "guardrailConfig" not in fake.calls[0]


def test_guardrail_config_is_passed_when_set() -> None:
    fake = FakeBedrock([converse_response("ok")])
    client = ChatClient(bedrock_client=fake, guardrail_id="gr-123", guardrail_version="3")
    client.reply([ChatMessage(role="user", content="hi")])
    assert fake.calls[0]["guardrailConfig"] == {
        "guardrailIdentifier": "gr-123",
        "guardrailVersion": "3",
    }


def test_guardrail_version_defaults_to_draft() -> None:
    fake = FakeBedrock([converse_response("ok")])
    ChatClient(bedrock_client=fake, guardrail_id="gr-123").reply(
        [ChatMessage(role="user", content="hi")]
    )
    assert fake.calls[0]["guardrailConfig"]["guardrailVersion"] == "DRAFT"


def test_intervention_is_reported_not_silently_returned() -> None:
    blocked = converse_response("I can't help with diagnosing that.")
    blocked["stopReason"] = "guardrail_intervened"
    client = ChatClient(bedrock_client=FakeBedrock([blocked]), guardrail_id="gr-123")
    reply = client.reply([ChatMessage(role="user", content="do I have a torn disc?")])
    assert reply.guardrail_intervened is True
    # The blocked text is still returned — the caller decides how to render it.
    assert reply.message == "I can't help with diagnosing that."


def test_normal_reply_is_not_flagged_as_intervened() -> None:
    fake = FakeBedrock([converse_response("Rest and reassess in 48h.")])
    reply = ChatClient(bedrock_client=fake).reply([ChatMessage(role="user", content="hi")])
    assert reply.guardrail_intervened is False
