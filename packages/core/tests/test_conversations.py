"""ConversationStore against moto's DynamoDB — the keys, the TTL, the append order.

Plus one contract test that reads the Pulumi table definition: the store and
``infra/components/data.py`` name the same keys and the same TTL attribute, or
the first deployed request fails with a ValidationException no unit test saw.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from docpipe_core.conversations import (
    CONVERSATION_ID,
    TTL_ATTRIBUTE,
    USER_ID,
    ConversationStore,
)
from docpipe_core.models import ChatMessage

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
TABLE = "docpipe-conversations"


def turn(question: str, answer: str) -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content=question),
        ChatMessage(role="assistant", content=answer),
    ]


def store_at(resource, *ticks: datetime, ttl_days: int = 30) -> ConversationStore:
    clock = iter(ticks)
    return ConversationStore(
        TABLE, dynamodb_resource=resource, ttl_days=ttl_days, clock=clock.__next__
    )


def test_get_missing_returns_none(conversations_table) -> None:
    store = store_at(conversations_table)
    assert store.get("user-a", "conv-1") is None


def test_append_creates_the_item_with_planned_keys_and_epoch_ttl(conversations_table) -> None:
    store = store_at(conversations_table, T0, ttl_days=7)
    conversation = store.append("user-a", "conv-1", turn("neck?", "Scalenes."))

    assert conversation.user_id == "user-a"
    assert conversation.conversation_id == "conv-1"
    assert [m.content for m in conversation.messages] == ["neck?", "Scalenes."]
    assert conversation.created_at == conversation.updated_at == T0
    assert conversation.expires_at == T0 + timedelta(days=7)

    # The raw item: DynamoDB's TTL only honours a Number of epoch seconds, under
    # exactly the attribute the table was created with.
    item = conversations_table.Table(TABLE).get_item(
        Key={USER_ID: "user-a", CONVERSATION_ID: "conv-1"}
    )["Item"]
    assert int(item[TTL_ATTRIBUTE]) == int((T0 + timedelta(days=7)).timestamp())
    assert item["messages"][0] == {"role": "user", "content": "neck?"}


def test_append_keeps_order_across_turns_and_slides_the_ttl(conversations_table) -> None:
    t1 = T0 + timedelta(minutes=5)
    store = store_at(conversations_table, T0, t1)
    store.append("user-a", "conv-1", turn("q1", "a1"))
    store.append("user-a", "conv-1", turn("q2", "a2"))

    loaded = store.get("user-a", "conv-1")
    assert loaded is not None
    assert [(m.role, m.content) for m in loaded.messages] == [
        ("user", "q1"),
        ("assistant", "a1"),
        ("user", "q2"),
        ("assistant", "a2"),
    ]
    assert loaded.created_at == T0  # set once
    assert loaded.updated_at == t1  # moves with every turn
    assert loaded.expires_at == t1 + timedelta(days=30)  # ...and so does the TTL


def test_conversations_are_scoped_to_the_user(conversations_table) -> None:
    store = store_at(conversations_table, T0, T0)
    store.append("user-a", "conv-1", turn("mine", "yes"))
    store.append("user-b", "conv-1", turn("also mine", "yes"))

    a = store.get("user-a", "conv-1")
    b = store.get("user-b", "conv-1")
    assert a is not None and b is not None
    assert a.messages[0].content == "mine"
    assert b.messages[0].content == "also mine"
    assert store.get("user-c", "conv-1") is None


def test_append_refuses_an_empty_turn(conversations_table) -> None:
    store = store_at(conversations_table, T0)
    with pytest.raises(ValueError, match="nothing to append"):
        store.append("user-a", "conv-1", [])
    assert store.get("user-a", "conv-1") is None


def test_a_non_positive_ttl_is_refused() -> None:
    with pytest.raises(ValueError, match="ttl_days"):
        ConversationStore(TABLE, dynamodb_resource=object(), ttl_days=0)


def test_the_store_names_the_same_keys_and_ttl_as_the_pulumi_table() -> None:
    """Source-level contract with infra/components/data.py — the only place the
    two halves meet before a deploy. Parsed, not imported: infra/ is quarantined
    from the packages (README, repo layout)."""
    source = (Path(__file__).resolve().parents[3] / "infra/components/data.py").read_text()
    block = re.search(r"self\.conversations = aws\.dynamodb\.Table\((.*?)\n\s*\)\n", source, re.S)
    assert block, "the conversations table left infra/components/data.py"
    table = block.group(1)
    assert f'hash_key="{USER_ID}"' in table
    assert f'range_key="{CONVERSATION_ID}"' in table
    assert f'"attribute_name": "{TTL_ATTRIBUTE}"' in table
