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
