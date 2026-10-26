"""Conversation history in DynamoDB: PK ``userId``, SK ``conversationId``, TTL.

The table is ``infra/components/data.py``'s ``<prefix>-conversations`` — the
same keys, the same TTL attribute (``expiresAt``, epoch seconds, as DynamoDB
requires). ``test_conversations.py`` reads that component and asserts the names
below against it, because a drift between this module and the table definition
is invisible to every unit test and fatal on the first deployed request.

One method writes: ``append`` is a single ``UpdateItem`` with
``list_append(if_not_exists(messages, :empty), :turns)`` — it creates the
conversation on first use and appends in order after that, atomically, so two
Lambdas racing on one conversation interleave turns instead of losing one.
``createdAt`` is set once (``if_not_exists``); ``updatedAt`` and ``expiresAt``
move on every turn, so the TTL slides: a conversation expires ``ttl_days`` after
its *last* message, not its first.

Why TTL at all: a conversation is user data the product has no reason to keep
past its usefulness (health.studio's product rules), and DynamoDB deletes
expired items for free. ``get`` does not filter on ``expiresAt`` itself —
DynamoDB's TTL sweep lags expiry by up to ~48 h, and a conversation that is
still readable in that window is correct to resume.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3

from docpipe_core.models import ChatMessage, Conversation, utc_now

# Attribute names — the table contract, shared with infra/components/data.py.
USER_ID = "userId"
CONVERSATION_ID = "conversationId"
TTL_ATTRIBUTE = "expiresAt"
_MESSAGES = "messages"
_CREATED_AT = "createdAt"
_UPDATED_AT = "updatedAt"

DEFAULT_TTL_DAYS = 30


class ConversationStore:
    """Per-user conversations in DynamoDB. PK userId, SK conversationId, TTL expiresAt."""

    def __init__(
        self,
        table_name: str,
        dynamodb_resource: Any = None,
        *,
        ttl_days: int = DEFAULT_TTL_DAYS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if ttl_days <= 0:
            raise ValueError("ttl_days must be positive — a TTL in the past deletes on arrival")
        resource = dynamodb_resource or boto3.resource("dynamodb")
        self._table = resource.Table(table_name)
        self._ttl = timedelta(days=ttl_days)
        self._clock = clock

    def get(self, user_id: str, conversation_id: str) -> Conversation | None:
        response = self._table.get_item(Key={USER_ID: user_id, CONVERSATION_ID: conversation_id})
        item = response.get("Item")
        return None if item is None else _conversation_of(item)

    def append(
        self, user_id: str, conversation_id: str, turns: Sequence[ChatMessage]
    ) -> Conversation:
        """Append ``turns`` in order, creating the conversation if it is new.

        Returns the conversation as stored after the write (``ALL_NEW``), so the
        caller never needs a second read to know what the user will see next.
        """
        if not turns:
            raise ValueError("nothing to append — a turn has at least one message")
        now = self._clock()
        response = self._table.update_item(
            Key={USER_ID: user_id, CONVERSATION_ID: conversation_id},
            UpdateExpression=(
                f"SET {_MESSAGES} = list_append(if_not_exists({_MESSAGES}, :empty), :turns), "
                f"{_CREATED_AT} = if_not_exists({_CREATED_AT}, :now), "
                f"{_UPDATED_AT} = :now, {TTL_ATTRIBUTE} = :expires"
            ),
            ExpressionAttributeValues={
                ":empty": [],
                ":turns": [m.model_dump(mode="json") for m in turns],
                ":now": now.isoformat(),
                ":expires": int((now + self._ttl).timestamp()),
            },
            ReturnValues="ALL_NEW",
        )
        return _conversation_of(response["Attributes"])


def _conversation_of(item: dict[str, Any]) -> Conversation:
    # DynamoDB's resource API hands numbers back as Decimal; TTL is epoch seconds.
    expires = item.get(TTL_ATTRIBUTE)
    return Conversation(
        conversation_id=str(item[CONVERSATION_ID]),
        user_id=str(item[USER_ID]),
        messages=[ChatMessage.model_validate(m) for m in item.get(_MESSAGES, [])],
        created_at=datetime.fromisoformat(item[_CREATED_AT]),
        updated_at=datetime.fromisoformat(item[_UPDATED_AT]),
        expires_at=None if expires is None else datetime.fromtimestamp(int(expires), UTC),
    )
