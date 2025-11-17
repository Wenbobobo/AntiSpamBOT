from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..config import ChatSettings
from ..models import CaseRecord, CaseStatus, VoteDecision
from ..storage import Storage

logger = logging.getLogger(__name__)


def _build_keyboard(case_id: int, allow_retract: bool) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text="✅ 是 Spam",
                callback_data=f"jury:vote:{case_id}:spam",
            ),
            InlineKeyboardButton(
                text="❌ 不是 Spam",
                callback_data=f"jury:vote:{case_id}:not",
            ),
        ]
    ]
    if allow_retract:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="↩ 撤回投票",
                    callback_data=f"jury:vote:{case_id}:retract",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


class CaseService:
    def __init__(self, bot: Bot, storage: Storage, defaults: ChatSettings):
        self.bot = bot
        self.storage = storage
        self.defaults = defaults

    async def load_settings(self, chat_id: int) -> ChatSettings:
        overrides = await self.storage.get_chat_settings(chat_id)
        return self.defaults.merge(overrides)

    async def handle_report(self, message: Message) -> str:
        """
        Process `/spam` reports inside a chat.
        """

        if not message.reply_to_message:
            return "请回复需要举报的消息并发送 /spam。"

        chat = message.chat
        reporter = message.from_user
        flagged_message = message.reply_to_message
        offender = flagged_message.from_user

        if not reporter:
            return "无法识别举报者。"
        if not offender:
            return "无法识别消息发送者，无法创建案例。"

        await self.storage.upsert_chat(chat.id, chat.title or "")
        settings = await self.load_settings(chat.id)
        now = datetime.now(tz=timezone.utc)
        since = now - timedelta(hours=1)

        recent_count = await self.storage.count_recent_reports(
            chat.id, reporter.id, since
        )
        if recent_count >= settings.max_cases_per_user_hour:
            return "举报频率过高，请稍后再试。"

        existing_case = await self.storage.get_case_by_message(
            chat.id, flagged_message.message_id
        )
        if existing_case:
            if existing_case.status == CaseStatus.OPEN:
                return "该消息已经在投票中，请直接参与投票。"
            status_text = {
                CaseStatus.CONFIRMED: "已被判定为 Spam",
                CaseStatus.REJECTED: "已被判定为非 Spam",
                CaseStatus.EXPIRED: "已超时",
            }.get(existing_case.status, "已处理")
            return (
                f"该消息已有案件 #{existing_case.id}（{status_text}），"
                "无需再次发起。"
            )

        chat_member_count = await self._fetch_member_count(chat.id)
        participant_target = max(
            settings.min_participation_count,
            math.ceil(settings.min_participation_ratio * chat_member_count),
        )

        closes_at = now + timedelta(seconds=settings.vote_timeout_sec)
        case = await self.storage.create_case(
            chat_id=chat.id,
            message_id=flagged_message.message_id,
            offender_id=offender.id,
            reporter_id=reporter.id,
            closes_at=closes_at,
            config_snapshot=settings.model_dump(),
            participant_target=participant_target,
        )

        poll_text = self._format_poll_text(
            case=case,
            settings=settings,
            spam_votes=0,
            not_spam_votes=0,
            total_voters=0,
        )

        sent = await flagged_message.reply(
            poll_text,
            reply_markup=_build_keyboard(case.id, settings.allow_vote_retract),
            allow_sending_without_reply=True,
        )

        await self.storage.update_case_poll(
            case_id=case.id,
            poll_chat_id=sent.chat.id,
            poll_message_id=sent.message_id,
        )
        case.poll_chat_id = sent.chat.id
        case.poll_message_id = sent.message_id

        asyncio.create_task(self._schedule_expiry_check(case.id, closes_at))

        return "已创建投票，邀请成员判断是否为 Spam 消息。"

    async def _schedule_expiry_check(self, case_id: int, closes_at: datetime) -> None:
        delay = max((closes_at - datetime.now(tz=timezone.utc)).total_seconds(), 0)
        await asyncio.sleep(delay)
        case = await self.storage.get_case(case_id)
        if not case or case.status != CaseStatus.OPEN:
            return
        settings = ChatSettings(**case.config_snapshot)
        await self._close_case(case, settings, expired=True)

    async def expire_overdue_cases(self) -> None:
        now = datetime.now(tz=timezone.utc)
        cases = await self.storage.list_open_cases()
        for case in cases:
            if now >= case.closes_at:
                settings = ChatSettings(**case.config_snapshot)
                await self._close_case(case, settings, expired=True)

    async def handle_vote_callback(self, callback: CallbackQuery) -> None:
        data = callback.data or ""
        parts = data.split(":")
        if len(parts) < 4:
            await callback.answer("无效的投票数据。", show_alert=True)
            return
        _, action, case_id_str, decision_str = parts
        if action != "vote":
            await callback.answer("未知操作。", show_alert=True)
            return

        try:
            case_id = int(case_id_str)
        except ValueError:
            await callback.answer("无效案例。", show_alert=True)
            return

        case = await self.storage.get_case(case_id)
        if not case:
            await callback.answer("案例不存在或已关闭。", show_alert=True)
            return

        if case.status != CaseStatus.OPEN:
            await callback.answer("此案例已结束。", show_alert=False)
            return

        settings = ChatSettings(**case.config_snapshot)
        voter = callback.from_user

        vote_updated = False
        if decision_str == "retract":
            if not settings.allow_vote_retract:
                await callback.answer("当前群聊未开启撤回投票。", show_alert=True)
                return
            await self.storage.retract_vote(case.id, voter.id)
            vote_updated = True
        else:
            decision = (
                VoteDecision.SPAM if decision_str == "spam" else VoteDecision.NOT_SPAM
            )
            await self.storage.record_vote(case.id, voter.id, decision)
            vote_updated = True

        if not vote_updated:
            await callback.answer("未能更新投票。", show_alert=True)
            return

        await callback.answer("投票已记录。")
        await self._refresh_poll(case, settings)

    async def _refresh_poll(self, case: CaseRecord, settings: ChatSettings) -> None:
        votes = await self.storage.get_votes(case.id)
        spam_votes = sum(1 for v in votes if v.decision == VoteDecision.SPAM)
        not_spam_votes = sum(1 for v in votes if v.decision == VoteDecision.NOT_SPAM)
        total = len(votes)

        await self._maybe_resolve(case, settings, spam_votes, not_spam_votes, total)

        if case.status != CaseStatus.OPEN:
            return

        if case.poll_chat_id and case.poll_message_id:
            text = self._format_poll_text(
                case, settings, spam_votes, not_spam_votes, total
            )
            try:
                await self.bot.edit_message_text(
                    chat_id=case.poll_chat_id,
                    message_id=case.poll_message_id,
                    text=text,
                    reply_markup=_build_keyboard(case.id, settings.allow_vote_retract),
                )
            except TelegramBadRequest as exc:
                logger.warning("编辑投票消息失败: %s", exc)

    async def _maybe_resolve(
        self,
        case: CaseRecord,
        settings: ChatSettings,
        spam_votes: int,
        not_spam_votes: int,
        total: int,
    ) -> None:
        if case.status != CaseStatus.OPEN:
            return
        now = datetime.now(tz=timezone.utc)
        if now >= case.closes_at:
            await self._close_case(case, settings, expired=True)
            return

        if total == 0:
            return

        participation_ok = self._participation_met(
            total, settings, case.participant_target
        )
        ratio = spam_votes / total
        ratio_ok = ratio >= settings.approval_ratio

        if participation_ok and ratio_ok:
            await self._confirm_case(case, settings)

    async def _close_case(
        self, case: CaseRecord, settings: ChatSettings, expired: bool = False
    ) -> None:
        new_status = CaseStatus.EXPIRED if expired else CaseStatus.REJECTED
        await self.storage.set_case_status(case.id, new_status)
        case.status = new_status
        status_text = "投票超时，判定未成立。" if expired else "票数不足，判定未成立。"
        if case.poll_chat_id and case.poll_message_id:
            try:
                await self.bot.edit_message_text(
                    chat_id=case.poll_chat_id,
                    message_id=case.poll_message_id,
                    text=f"{status_text}\n\n案件 #{case.id}",
                )
            except TelegramBadRequest:
                pass

    async def _confirm_case(self, case: CaseRecord, settings: ChatSettings) -> None:
        await self.storage.set_case_status(case.id, CaseStatus.CONFIRMED)
        case.status = CaseStatus.CONFIRMED
        await self._enforce_actions(case, settings)
        if case.poll_chat_id and case.poll_message_id:
            try:
                await self.bot.edit_message_text(
                    chat_id=case.poll_chat_id,
                    message_id=case.poll_message_id,
                    text=f"✅ 多数判定该消息为 Spam。案件 #{case.id} 已执行。"
                    "\n感谢参与投票！",
                )
            except TelegramBadRequest:
                pass

    async def _enforce_actions(self, case: CaseRecord, settings: ChatSettings) -> None:
        try:
            await self.bot.delete_message(case.chat_id, case.message_id)
        except TelegramBadRequest as exc:
            logger.info("删除消息失败或消息已删除: %s", exc)

        if settings.action_on_confirm == "delete_only":
            return

        if settings.action_on_confirm == "ban":
            await self._ban_user(case.chat_id, case.offender_id)
        elif settings.action_on_confirm == "kick":
            await self._kick_user(case.chat_id, case.offender_id)
        elif settings.action_on_confirm == "mute":
            await self._mute_user(case.chat_id, case.offender_id, settings)

        if settings.blacklist_enabled:
            await self.storage.blacklist_add(
                case.chat_id, case.offender_id, reason=f"Case #{case.id}"
            )

    async def _ban_user(self, chat_id: int, user_id: int) -> None:
        try:
            await self.bot.ban_chat_member(chat_id, user_id)
        except TelegramBadRequest as exc:
            logger.warning("封禁失败: %s", exc)

    async def _kick_user(self, chat_id: int, user_id: int) -> None:
        try:
            await self.bot.ban_chat_member(chat_id, user_id)
            await asyncio.sleep(1)
            await self.bot.unban_chat_member(chat_id, user_id)
        except TelegramBadRequest as exc:
            logger.warning("踢出失败: %s", exc)

    async def _mute_user(
        self, chat_id: int, user_id: int, settings: ChatSettings
    ) -> None:
        until_date = datetime.now(tz=timezone.utc) + timedelta(
            seconds=settings.mute_duration_sec
        )
        try:
            await self.bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=until_date,
            )
        except TelegramBadRequest as exc:
            logger.warning("禁言失败: %s", exc)

    async def _fetch_member_count(self, chat_id: int) -> int:
        try:
            return await self.bot.get_chat_member_count(chat_id)
        except TelegramBadRequest:
            return 100  # fallback heuristic

    def _participation_met(
        self, total: int, settings: ChatSettings, participant_target: int
    ) -> bool:
        count_ok = total >= settings.min_participation_count
        ratio_ok = total >= participant_target
        match settings.quorum_strategy:
            case "ratio_only":
                return ratio_ok
            case "count_only":
                return count_ok
            case _:
                return ratio_ok and count_ok

    def _format_poll_text(
        self,
        case: CaseRecord,
        settings: ChatSettings,
        spam_votes: int,
        not_spam_votes: int,
        total_voters: int,
    ) -> str:
        remaining = max(case.participant_target - total_voters, 0)
        ratio_display = f"{settings.approval_ratio * 100:.0f}%"
        return (
            "⚖️ 该消息被举报为疑似 Spam，请投票\n"
            f"• 案件编号：#{case.id}\n"
            f"• 需要至少 {case.participant_target} 人参与，赞成率需达到 {ratio_display}\n"
            f"• 当前赞成 {spam_votes}，反对 {not_spam_votes}，总票 {total_voters}\n"
            f"• 还需 {remaining} 人参与决议\n"
            f"• 投票将在 {case.closes_at.strftime('%H:%M:%S')} (UTC) 自动结束"
        )
