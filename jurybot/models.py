from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class CaseStatus(str, Enum):
    OPEN = "open"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class VoteDecision(str, Enum):
    SPAM = "spam"
    NOT_SPAM = "not_spam"


@dataclass(slots=True)
class CaseRecord:
    id: Optional[int]
    chat_id: int
    message_id: int
    offender_id: int
    reporter_id: int
    status: CaseStatus
    opened_at: datetime
    closes_at: datetime
    poll_chat_id: int | None
    poll_message_id: int | None
    config_snapshot: dict
    participant_target: int


@dataclass(slots=True)
class VoteRecord:
    case_id: int
    voter_id: int
    decision: VoteDecision
    updated_at: datetime
