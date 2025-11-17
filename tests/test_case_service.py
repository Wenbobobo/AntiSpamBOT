from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jurybot.config import ChatSettings
from jurybot.models import CaseRecord, CaseStatus
from jurybot.services.case import CaseService


def _case_record(status: CaseStatus, closes_in_seconds: int = 300) -> CaseRecord:
    now = datetime.now(tz=timezone.utc)
    return CaseRecord(
        id=1,
        chat_id=-123,
        message_id=42,
        offender_id=1001,
        reporter_id=2002,
        status=status,
        opened_at=now - timedelta(seconds=30),
        closes_at=now + timedelta(seconds=closes_in_seconds),
        poll_chat_id=None,
        poll_message_id=None,
        config_snapshot=ChatSettings().model_dump(),
        participant_target=5,
    )


def make_service() -> CaseService:
    bot = AsyncMock()
    storage = SimpleNamespace(
        set_case_status=AsyncMock(),
        blacklist_add=AsyncMock(),
    )
    return CaseService(bot=bot, storage=storage, defaults=ChatSettings())


@pytest.mark.parametrize(
    ("strategy", "total", "target", "expected"),
    [
        ("ratio_and_count", 5, 5, True),
        ("ratio_and_count", 4, 5, False),
        ("ratio_only", 4, 5, False),
        ("ratio_only", 6, 5, True),
        ("count_only", 3, 5, False),
        ("count_only", 6, 5, True),
    ],
)
def test_participation_threshold(strategy, total, target, expected):
    service = make_service()
    settings = ChatSettings(quorum_strategy=strategy, min_participation_count=5)
    assert service._participation_met(total, settings, target) is expected


@pytest.mark.asyncio
async def test_case_confirms_when_threshold_met(monkeypatch):
    service = make_service()
    case = _case_record(CaseStatus.OPEN)
    settings = ChatSettings(
        min_participation_count=3,
        min_participation_ratio=0.05,
        approval_ratio=0.6,
    )
    case.participant_target = 3

    enforced = AsyncMock()
    monkeypatch.setattr(service, "_enforce_actions", enforced)

    await service._maybe_resolve(
        case=case,
        settings=settings,
        spam_votes=3,
        not_spam_votes=0,
        total=3,
    )

    service.storage.set_case_status.assert_called_with(case.id, CaseStatus.CONFIRMED)
    assert case.status == CaseStatus.CONFIRMED
    enforced.assert_awaited()


@pytest.mark.asyncio
async def test_case_expires_when_timeout_passed(monkeypatch):
    service = make_service()
    case = _case_record(CaseStatus.OPEN, closes_in_seconds=-1)
    settings = ChatSettings()

    await service._maybe_resolve(
        case=case,
        settings=settings,
        spam_votes=0,
        not_spam_votes=0,
        total=0,
    )

    service.storage.set_case_status.assert_called_with(case.id, CaseStatus.EXPIRED)
    assert case.status == CaseStatus.EXPIRED

