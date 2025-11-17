from __future__ import annotations

import logging
import time
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ChatMemberAdministrator, ChatMemberOwner

from ..config import ChatSettings
from ..storage import Storage

logger = logging.getLogger(__name__)


def _bool_from_str(value: str) -> bool:
    true_set = {"1", "true", "yes", "on", "enable"}
    false_set = {"0", "false", "no", "off", "disable"}
    val = value.strip().lower()
    if val in true_set:
        return True
    if val in false_set:
        return False
    raise ValueError("请输入 true/false 或 yes/no。")


class AdminService:
    def __init__(
        self,
        bot: Bot,
        storage: Storage,
        defaults: ChatSettings,
        owner_ids: list[int],
    ):
        self.bot = bot
        self.storage = storage
        self.defaults = defaults
        self.owner_ids = set(owner_ids or [])
        self._admin_cache: dict[int, tuple[float, set[int]]] = {}
        self._admin_cache_ttl = 300.0  # seconds

    async def list_chats_summary(self, user_id: int) -> str:
        chats = await self.manageable_chats(user_id)
        if not chats:
            return "尚未发现你可管理的群聊，请先在目标群授予管理员权限。"
        lines = ["你可以管理的群聊："]
        for chat_id, title in chats:
            display = title or str(chat_id)
            lines.append(f"- {display} (`{chat_id}`)")
        return "\n".join(lines)

    async def manageable_chats(self, user_id: int) -> list[tuple[int, str]]:
        chats = await self.storage.list_chats()
        result: list[tuple[int, str]] = []
        for chat_id, title in chats:
            if await self.ensure_admin(user_id, chat_id):
                result.append((chat_id, title or str(chat_id)))
        return result

    async def get_settings(
        self, chat_id: int
    ) -> tuple[str, ChatSettings, dict[str, Any]]:
        overrides = await self.storage.get_chat_settings(chat_id) or {}
        settings = self.defaults.merge(overrides)
        title = await self.storage.get_chat_title(chat_id) or str(chat_id)
        return title, settings, overrides

    async def show_config(self, chat_id: int) -> str:
        overrides = await self.storage.get_chat_settings(chat_id)
        settings = self.defaults.merge(overrides)
        title = await self.storage.get_chat_title(chat_id) or str(chat_id)
        return self._format_settings(title, settings, overrides or {})

    def _format_settings(
        self, chat_title: str, settings: ChatSettings, overrides: dict[str, Any]
    ) -> str:
        lines = [
            f"📋 `{chat_title}` 的当前设置：",
            f"- 最低参与人数：{settings.min_participation_count}",
            f"- 最低参与比例：{settings.min_participation_ratio}",
            f"- 通过票比例：{settings.approval_ratio}",
            f"- 阈值策略：{settings.quorum_strategy}",
            f"- 通过后动作：{settings.action_on_confirm}",
            f"- 黑名单：{'开启' if settings.blacklist_enabled else '关闭'}",
            f"- 投票限时：{settings.vote_timeout_sec}s",
            f"- 每小时举报上限：{settings.max_cases_per_user_hour}",
            f"- 可撤回：{'是' if settings.allow_vote_retract else '否'}",
        ]
        if overrides:
            lines.append("- 自定义字段：" + ", ".join(overrides.keys()))
        return "\n".join(lines)

    async def update_setting(
        self, chat_id: int, field: str, value: str
    ) -> str:
        field = field.strip()
        overrides = await self.storage.get_chat_settings(chat_id) or {}
        base = self.defaults.merge(overrides)
        if field not in ChatSettings.model_fields:
            raise ValueError(f"未知配置项：{field}")

        parsed = self._parse_value(field, value)
        overrides[field] = parsed
        # validate
        self.defaults.merge(overrides)
        await self.storage.set_chat_settings(chat_id, overrides)
        return f"已更新 {field} = {parsed}"

    def _parse_value(self, field: str, value: str) -> Any:
        info = ChatSettings.model_fields[field]
        annotation = info.annotation
        if annotation in (float, int):
            return annotation(value)
        if annotation is bool:
            return _bool_from_str(value)
        if field in {"quorum_strategy", "action_on_confirm"}:
            return value.strip()
        if field in {"min_participation_ratio", "approval_ratio"}:
            return float(value)
        if field.endswith("_sec") or field.endswith("_count"):
            return int(value)
        return value

    async def ensure_admin(self, user_id: int, chat_id: int) -> bool:
        if user_id in self.owner_ids:
            return True
        admin_ids = await self._get_admin_ids(chat_id)
        if user_id in admin_ids:
            return True
        if admin_ids:
            return False
        # fallback when admin list unavailable
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
        except TelegramBadRequest:
            return False
        status = getattr(member, "status", "")
        return status in {"creator", "administrator"}

    async def _get_admin_ids(self, chat_id: int) -> set[int]:
        now = time.time()
        cached = self._admin_cache.get(chat_id)
        if cached and cached[0] > now:
            return cached[1]

        try:
            admins = await self.bot.get_chat_administrators(chat_id)
        except TelegramBadRequest as exc:
            logger.warning("获取群管理员失败 chat_id=%s: %s", chat_id, exc.message)
            admin_ids: set[int] = set()
        else:
            admin_ids = {
                admin.user.id
                for admin in admins
                if isinstance(admin, (ChatMemberAdministrator, ChatMemberOwner))
            }
        self._admin_cache[chat_id] = (now + self._admin_cache_ttl, admin_ids)
        return admin_ids

    async def stats(self, chat_id: int) -> str:
        cases = await self.storage.list_cases(chat_id, limit=5)
        if not cases:
            return "暂无统计数据。"
        lines = ["最近 5 条案例："]
        for case in cases:
            lines.append(
                f"- 案件 #{case.id} 状态 {case.status}，举报者 {case.reporter_id}，目标 {case.offender_id}"
            )
        return "\n".join(lines)

    async def blacklist_action(
        self, chat_id: int, user_id: int, action: str, reason: str | None
    ) -> str:
        if action == "add":
            await self.storage.blacklist_add(chat_id, user_id, reason)
            return f"已将 {user_id} 加入黑名单。"
        if action == "remove":
            await self.storage.blacklist_remove(chat_id, user_id)
            return f"已将 {user_id} 移出黑名单。"
        raise ValueError("action 需要为 add/remove")
