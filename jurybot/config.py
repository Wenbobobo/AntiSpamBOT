from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
import tomllib

from dotenv import dotenv_values
from pydantic import BaseModel, Field, ValidationError


class ChatSettings(BaseModel):
    """
    Configurable parameters that define how voting behaves for a chat.
    """

    min_participation_ratio: float = Field(
        0.05, ge=0.0, le=1.0, description="Minimum voters / active members ratio."
    )
    min_participation_count: int = Field(
        5, ge=1, description="Absolute minimum number of voters required."
    )
    approval_ratio: float = Field(
        0.6, ge=0.0, le=1.0, description="Required ratio of 'Spam' votes."
    )
    quorum_strategy: Literal["ratio_only", "ratio_and_count", "count_only"] = (
        "ratio_and_count"
    )
    action_on_confirm: Literal["ban", "kick", "delete_only", "mute"] = "ban"
    mute_duration_sec: int = Field(
        3600, ge=60, description="Mute duration when action is 'mute'."
    )
    blacklist_enabled: bool = True
    vote_timeout_sec: int = Field(
        14400, ge=30, description="Voting window in seconds for each case."
    )
    allow_vote_retract: bool = True
    max_cases_per_user_hour: int = Field(
        3, ge=1, description="Rate limit for reporters to avoid abuse."
    )
    auto_close_on_deleted_msg: bool = True
    min_account_age_hours: int = Field(
        0, ge=0, description="Ignore reporters/voters with account younger than this."
    )

    model_config = {"validate_assignment": True}

    def merge(self, overrides: dict[str, Any] | None) -> "ChatSettings":
        return self.model_copy(update=overrides or {})


class BotConfig(BaseModel):
    token: str = Field(..., min_length=20)
    token_file: str = ".env"
    storage_url: str = "sqlite:///jurybot.db"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class AdminUIConfig(BaseModel):
    owner_ids: list[int] = Field(default_factory=list)


class AppConfig(BaseModel):
    bot: BotConfig
    defaults: ChatSettings = Field(default_factory=ChatSettings)
    admin_ui: AdminUIConfig = Field(default_factory=AdminUIConfig)


@dataclass(slots=True)
class LoadedConfig:
    """Wrapper storing parsed config and file paths."""

    config: AppConfig
    path: Path


def _load_toml(path: Path) -> dict[str, Any]:
    data = tomllib.loads(path.read_text("utf-8"))
    return data


def load_config(path: str | Path = "config.toml") -> LoadedConfig:
    """
    Parse the config file and resolve the bot token from the referenced env file.
    """

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    raw = _load_toml(config_path)
    bot_section = raw.get("bot", {})

    token_file = bot_section.get("token_file", ".env")
    token_path = (
        Path(token_file)
        if Path(token_file).is_absolute()
        else (config_path.parent / token_file)
    )
    env_values = dotenv_values(token_path)
    token = bot_section.get("token") or env_values.get("BOT_TOKEN")
    if not token:
        raise ValueError(
            f"BOT_TOKEN missing. Provide it in {token_path} or directly in config."
        )

    bot_section["token"] = token
    raw["bot"] = bot_section

    try:
        config = AppConfig(**raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid config: {exc}") from exc

    return LoadedConfig(config=config, path=config_path)
