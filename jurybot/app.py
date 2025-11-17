from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
import contextlib

from .config import ChatSettings, load_config
from .services.admin import AdminService
from .services.case import CaseService
from .storage import Storage

FIELD_TOKEN_MAP = {
    "mpc": "min_participation_count",
    "mpr": "min_participation_ratio",
    "apr": "approval_ratio",
    "vts": "vote_timeout_sec",
    "mds": "mute_duration_sec",
    "mcu": "max_cases_per_user_hour",
}

TOGGLE_TOKENS = {
    "ble": "blacklist_enabled",
    "avr": "allow_vote_retract",
}

ACTION_OPTIONS = ["ban", "kick", "delete_only", "mute"]
ACTION_LABELS = {
    "ban": "封禁",
    "kick": "踢出",
    "delete_only": "仅删帖",
    "mute": "禁言",
}

logger = logging.getLogger(__name__)


class JuryBotApp:
    def __init__(self, config_path: str | Path = "config.toml"):
        loaded = load_config(config_path)
        self.config = loaded.config
        self.config_path = loaded.path
        self.storage = Storage(self.config.bot.storage_url)
        self.bot = Bot(self.config.bot.token, parse_mode=None)
        self.case_service = CaseService(self.bot, self.storage, self.config.defaults)
        self.admin_service = AdminService(
            self.bot,
            self.storage,
            self.config.defaults,
            self.config.admin_ui.owner_ids,
        )
        self.dp = Dispatcher()
        self._register_routes()

    async def connect(self) -> None:
        await self.storage.connect()
        await self.case_service.expire_overdue_cases()

    async def run(self) -> None:
        await self.connect()
        logger.info("JuryBot started with config %s", self.config_path)
        try:
            await self.dp.start_polling(self.bot)
        finally:
            await self.storage.close()

    def _register_routes(self) -> None:
        report_router = Router(name="reports")

        @report_router.message(Command("spam"))
        async def handle_spam(message: Message) -> None:
            if message.chat.type not in {"group", "supergroup"}:
                await message.answer("该命令仅在群组中有效。")
                return
            response = await self.case_service.handle_report(message)
            if response:
                await message.reply(response)

        @report_router.callback_query(F.data.startswith("jury:"))
        async def handle_vote(callback: CallbackQuery) -> None:
            await self.case_service.handle_vote_callback(callback)

        admin_router = Router(name="admin")

        @admin_router.message(Command("config"))
        async def admin_config(message: Message) -> None:
            await self._handle_config_command(message)

        @admin_router.message(Command("set"))
        async def admin_set(message: Message) -> None:
            if message.chat.type != "private":
                return
            if not self._is_owner(message.from_user.id):
                await message.answer("仅 Bot 超级管理员可用。")
                return
            await self._handle_set_command(message)

        @admin_router.message(Command("stats"))
        async def admin_stats(message: Message) -> None:
            if message.chat.type != "private":
                return
            if not self._is_owner(message.from_user.id):
                await message.answer("仅 Bot 超级管理员可用。")
                return
            await self._handle_stats_command(message)

        @admin_router.message(Command("blacklist"))
        async def admin_blacklist(message: Message) -> None:
            if message.chat.type != "private":
                return
            if not self._is_owner(message.from_user.id):
                await message.answer("仅 Bot 超级管理员可用。")
                return
            await self._handle_blacklist_command(message)

        @admin_router.callback_query(F.data.startswith("cfg:"))
        async def config_callback(callback: CallbackQuery) -> None:
            await self._handle_config_callback(callback)

        self.dp.include_routers(report_router, admin_router)

    async def _handle_config_command(self, message: Message) -> None:
        # Restrict configuration to being run inside the target group/supergroup.
        if message.chat.type not in {"group", "supergroup"}:
            await message.answer("请在需要管理的群组内发送 /config。")
            return

        chat_id = message.chat.id
        await self._ensure_chat_record(chat_id, message.chat.title or "")

        if not await self._ensure_admin(message.from_user.id, chat_id):
            await message.reply("你不是该群的管理员。")
            return

        await self._try_fetch_and_store_chat(chat_id)
        await self._send_config_panel(message, chat_id)

    async def _handle_set_command(self, message: Message) -> None:
        if message.chat.type != "private":
            return
        args = (message.text or "").split(maxsplit=3)
        if len(args) < 4:
            await message.answer("用法：/set <chat_id> <字段> <值>")
            return
        chat_id = self._parse_int(args[1])
        if chat_id is None:
            await message.answer("请输入正确的 chat_id。")
            return
        if not await self._ensure_admin(message.from_user.id, chat_id):
            await message.answer("你不是该群的管理员。")
            return
        field, value = args[2], args[3]
        try:
            result = await self.admin_service.update_setting(chat_id, field, value)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await message.answer(result)

    async def _handle_stats_command(self, message: Message) -> None:
        if message.chat.type != "private":
            return
        args = (message.text or "").split()
        if len(args) < 2:
            await message.answer("用法：/stats <chat_id>")
            return
        chat_id = self._parse_int(args[1])
        if chat_id is None:
            await message.answer("请输入正确的 chat_id。")
            return
        if not await self._ensure_admin(message.from_user.id, chat_id):
            await message.answer("你不是该群的管理员。")
            return
        stats = await self.admin_service.stats(chat_id)
        await message.answer(stats)

    async def _handle_blacklist_command(self, message: Message) -> None:
        if message.chat.type != "private":
            return
        args = (message.text or "").split(maxsplit=4)
        if len(args) < 4:
            await message.answer("用法：/blacklist <add|remove> <chat_id> <user_id> [reason]")
            return
        action = args[1]
        chat_id = self._parse_int(args[2])
        user_id = self._parse_int(args[3])
        reason = args[4] if len(args) == 5 else None
        if chat_id is None or user_id is None:
            await message.answer("请输入正确的 chat_id 和 user_id。")
            return
        if not await self._ensure_admin(message.from_user.id, chat_id):
            await message.answer("你不是该群的管理员。")
            return
        try:
            result = await self.admin_service.blacklist_action(
                chat_id, user_id, action, reason
            )
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await message.answer(result)

    async def _handle_config_callback(self, callback: CallbackQuery) -> None:
        data = (callback.data or "").split(":")
        if len(data) < 2:
            await callback.answer("无效操作。", show_alert=True)
            return
        action = data[1]

        if action == "list":
            if callback.message:
                await self._send_chat_picker(
                    callback.message, callback.from_user.id, edit=True
                )
            await callback.answer()
            return
        if action == "close":
            if callback.message:
                with contextlib.suppress(Exception):
                    await callback.message.delete()
            await callback.answer()
            return

        if len(data) < 3:
            await callback.answer("缺少 chat_id。", show_alert=True)
            return

        chat_id = self._parse_int(data[2])
        if chat_id is None:
            await callback.answer("无效群组。", show_alert=True)
            return

        if not await self._ensure_admin(callback.from_user.id, chat_id):
            await callback.answer("你不是该群管理员。", show_alert=True)
            return

        try:
            if action == "select":
                await self._try_fetch_and_store_chat(chat_id)
                if callback.message:
                    await self._render_config_panel(callback.message, chat_id)
            elif action == "adj":
                await self._apply_adjustment(chat_id, data)
                if callback.message:
                    await self._render_config_panel(callback.message, chat_id)
            elif action == "toggle":
                await self._apply_toggle(chat_id, data)
                if callback.message:
                    await self._render_config_panel(callback.message, chat_id)
            elif action == "act":
                if len(data) < 4:
                    raise ValueError("缺少参数。")
                await self.admin_service.update_setting(chat_id, "action_on_confirm", data[3])
                if callback.message:
                    await self._render_config_panel(callback.message, chat_id)
            else:
                await callback.answer("未知操作。", show_alert=True)
                return
        except ValueError as exc:
            await callback.answer(str(exc), show_alert=True)
            return

        await callback.answer("已更新。")

    async def _apply_adjustment(self, chat_id: int, parts: list[str]) -> None:
        if len(parts) < 5:
            raise ValueError("缺少参数。")
        token = parts[3]
        delta_raw = parts[4]
        field = FIELD_TOKEN_MAP.get(token)
        if not field:
            raise ValueError("未知配置项。")
        _, settings, _ = await self.admin_service.get_settings(chat_id)
        current = getattr(settings, field)
        try:
            delta = float(delta_raw)
        except ValueError:
            raise ValueError("增减值无效。") from None
        new_value = current + delta
        if isinstance(current, int):
            new_value = int(new_value)
            value_str = str(new_value)
        else:
            new_value = round(new_value, 4)
            value_str = str(new_value)
        await self.admin_service.update_setting(chat_id, field, value_str)

    async def _apply_toggle(self, chat_id: int, parts: list[str]) -> None:
        if len(parts) < 4:
            raise ValueError("缺少参数。")
        token = parts[3]
        field = TOGGLE_TOKENS.get(token)
        if not field:
            raise ValueError("未知开关。")
        _, settings, _ = await self.admin_service.get_settings(chat_id)
        current = getattr(settings, field)
        await self.admin_service.update_setting(chat_id, field, "false" if current else "true")

    async def _send_chat_picker(
        self, message: Message, user_id: int, edit: bool = False
    ) -> None:
        chats = await self.admin_service.manageable_chats(user_id)
        if not chats:
            text = "尚未发现你管理的群聊，请先邀请并授予管理员权限。"
            if edit and message:
                await message.edit_text(text)
            else:
                await message.answer(text)
            return
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=title[:30],
                        callback_data=f"cfg:select:{chat_id}",
                    )
                ]
                for chat_id, title in chats
            ]
        )
        text = "请选择要管理的群聊："
        if edit and message:
            await message.edit_text(text, reply_markup=keyboard)
        else:
            await message.answer(text, reply_markup=keyboard)

    async def _send_config_panel(self, message: Message, chat_id: int) -> None:
        text, keyboard = await self._build_config_panel(chat_id)
        await message.answer(text, reply_markup=keyboard)

    async def _render_config_panel(self, message: Message, chat_id: int) -> None:
        text, keyboard = await self._build_config_panel(chat_id)
        await message.edit_text(text, reply_markup=keyboard)

    async def _build_config_panel(
        self, chat_id: int
    ) -> tuple[str, InlineKeyboardMarkup]:
        title, settings, _ = await self.admin_service.get_settings(chat_id)
        text = self._config_panel_text(title, settings, chat_id)
        keyboard = self._config_panel_keyboard(chat_id, settings)
        return text, keyboard

    def _config_panel_text(
        self, title: str, settings: ChatSettings, chat_id: int
    ) -> str:
        return (
            f"📋 {title} (`{chat_id}`)\n"
            f"- 最低参与人数：{settings.min_participation_count}\n"
            f"- 最低参与比例：{settings.min_participation_ratio:.2f}\n"
            f"- 通过票比例：{settings.approval_ratio:.2f}\n"
            f"- 投票限时：{settings.vote_timeout_sec}s\n"
            f"- 每小时举报上限：{settings.max_cases_per_user_hour}\n"
            f"- 通过动作：{ACTION_LABELS.get(settings.action_on_confirm, settings.action_on_confirm)}\n"
            f"- 黑名单：{'开启' if settings.blacklist_enabled else '关闭'}\n"
            f"- 可撤回：{'开启' if settings.allow_vote_retract else '关闭'}\n"
            "使用下方按钮即可在线调整，无需修改配置文件。"
        )

    def _config_panel_keyboard(
        self, chat_id: int, settings: ChatSettings
    ) -> InlineKeyboardMarkup:
        def adj_button(label: str, token: str, delta: str) -> InlineKeyboardButton:
            return InlineKeyboardButton(
                text=label,
                callback_data=f"cfg:adj:{chat_id}:{token}:{delta}",
            )

        toggles = [
            InlineKeyboardButton(
                text=f"黑名单 {'✅' if settings.blacklist_enabled else '❌'}",
                callback_data=f"cfg:toggle:{chat_id}:ble",
            ),
            InlineKeyboardButton(
                text=f"可撤回 {'✅' if settings.allow_vote_retract else '❌'}",
                callback_data=f"cfg:toggle:{chat_id}:avr",
            ),
        ]

        action_row = [
            InlineKeyboardButton(
                text=(
                    ("✅ " if settings.action_on_confirm == option else "")
                    + ACTION_LABELS[option]
                ),
                callback_data=f"cfg:act:{chat_id}:{option}",
            )
            for option in ACTION_OPTIONS
        ]

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    adj_button("人数 -1", "mpc", "-1"),
                    adj_button("+1", "mpc", "1"),
                ],
                [
                    adj_button("比例 -0.01", "mpr", "-0.01"),
                    adj_button("+0.01", "mpr", "0.01"),
                ],
                [
                    adj_button("通过率 -0.05", "apr", "-0.05"),
                    adj_button("+0.05", "apr", "0.05"),
                ],
                [
                    adj_button("时限 -30m", "vts", str(-30 * 60)),
                    adj_button("+30m", "vts", str(30 * 60)),
                ],
                [
                    adj_button("举报 -1", "mcu", "-1"),
                    adj_button("+1", "mcu", "1"),
                ],
                action_row[:2],
                action_row[2:],
                toggles,
                [
                    InlineKeyboardButton(
                        text="返回群列表",
                        callback_data="cfg:list",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="关闭面板",
                        callback_data="cfg:close",
                    )
                ],
            ]
        )
        return keyboard

    async def _ensure_admin(self, user_id: int, chat_id: int) -> bool:
        return await self.admin_service.ensure_admin(user_id, chat_id)

    async def _ensure_chat_record(self, chat_id: int, title: str | None) -> None:
        await self.storage.upsert_chat(chat_id, title or "")

    async def _try_fetch_and_store_chat(self, chat_id: int) -> None:
        title = await self.storage.get_chat_title(chat_id)
        if title:
            return
        try:
            chat: Chat = await self.bot.get_chat(chat_id)
        except TelegramBadRequest:
            return
        await self.storage.upsert_chat(chat_id, chat.title or "")

    @staticmethod
    def _parse_int(value: str) -> Optional[int]:
        try:
            return int(value)
        except ValueError:
            return None

    def _is_owner(self, user_id: int) -> bool:
        return user_id in self.config.admin_ui.owner_ids


async def run_bot(config_path: str | Path = "config.toml") -> None:
    app = JuryBotApp(config_path)
    logging.getLogger().setLevel(app.config.bot.log_level)
    await app.run()
