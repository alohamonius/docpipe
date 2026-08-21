"""What is worth retrying against AWS, and how — shared by every path.

This lives in its own module because the answer is a property of the
*infrastructure*, not of any one caller. Aurora at ``min_capacity = 0``
auto-pauses whichever client wakes it: the query path (`retrieval`), the
agentic loop (`agent`), and the ingest path (`kb_sync.syncer`) all meet the
same resume. ``is_retryable`` previously lived in ``agent.py``, then moved to
``retrieval.py`` when a second caller appeared; the third caller is on the
*ingest* side, and having a sync import from a retrieval module to learn how
to wake a database is a dependency pointing the wrong way.

Deliberately NOT shared with ``llm.py``: it calls Bedrock Converse, never
Aurora, so the auto-pause arm below would be dead weight there and its narrower
set is correct for it.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError

_RETRYABLE_CODES = frozenset(
    {"ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException"}
)

# Aurora's resume message. Matched as substring because the sentence arrives
# wrapped in RdsData's own envelope, with the DB instance id interpolated.
_AUTO_PAUSE = "resuming after being auto-paused"


def is_retryable(error: ClientError) -> bool:
    """Transient Bedrock/RDS failures worth another attempt.

    The second arm is the one that matters operationally. An Aurora-backed KB
    at ``min_capacity = 0`` auto-pauses when idle — which is the *designed*
    resting state, not an edge case — and the first call after that comes back
    as a **``ValidationException``** naming the resume, not as any retryable
    error code. Matching on the code alone therefore fails every cold start,
    and on a scale-to-zero cluster "cold" is the normal case.
    """
    code = error.response.get("Error", {}).get("Code", "")
    if code in _RETRYABLE_CODES:
        return True
    return code == "ValidationException" and _AUTO_PAUSE in str(error)


def with_backoff(
    call: Callable[[], Any],
    *,
    max_retries: int = 4,
    sleep: Callable[[float], None] = time.sleep,
    rng: Callable[[], float] = random.random,
) -> Any:
    """Run ``call``, retrying transient failures with jittered exponential waits.

    Sized for an Aurora resume: four retries sleep ~1+2+4+8 s, which covers the
    auto-pause wake-up with room to spare (measured 1.32 s on the Retrieve
    path, FINDINGS 2026-08-20).
    """
    for attempt in range(max_retries + 1):
        try:
            return call()
        except ClientError as error:
            if not is_retryable(error) or attempt == max_retries:
                raise
            sleep(2**attempt + rng())
    raise RuntimeError("unreachable")
