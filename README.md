# JuryBot Product + Technical Specification

## 1. Vision
Create a Telegram anti-spam assistant that lets group members collectively decide whether a message is spam. Any user can reply `/spam` to a suspicious message; the bot posts an inline poll (`Yes / No / Retract`) and, once the configured threshold is met, it removes the target message and optionally kicks or bans the sender. The system must be configurable per chat, easy for admins to manage via private messages, and ready to evolve from a single-machine demo into a cloud-ready service.

Success signals:
- Sub-second response for poll creation in chats < 2K members.
- Threshold evaluation finishes < 2 seconds after each vote.
- Zero manual configuration on the host machine besides editing the config file and providing the bot token.

## 2. Personas & Roles
- **Group Member** – flags spam, participates in votes.
- **Group Admin** – tunes thresholds, actions, blacklists, and reviews cases through PM with the bot.
- **Operator** – deploys the bot, manages config files, and later runs cloud migrations.

## 3. High-Level Flow
1. Member replies `/spam` to a message.
2. Bot creates/updates a `Case` record, posts inline buttons: `✅ Spam`, `❌ Not Spam`, `↩ Retract Vote`, and optionally shows vote counts + timer text (configurable).
3. Each button press is stored; duplicate votes update the existing ballot.
4. After each vote, thresholds are recomputed:
   - **Participation threshold** – e.g., `>= 5 voters` *and* `>= 1/20 of active members`.
   - **Affirmative ratio** – e.g., `>= 60%` “Spam” votes.
5. If thresholds satisfied before the timer expires:
   - Bot deletes the flagged message (if still present).
   - Executes action (`ban`, `kick`, or `mute`).
   - Optionally adds sender to a blacklist.
6. Outcome is announced via inline update and log channel (optional).
7. If timer expires without meeting criteria, the case closes as “Not Proven.”

## 4. Configuration Model
The bot uses hierarchical configuration: `config.toml` (global defaults) + per-chat overrides stored in DB and editable via admin PM.

| Option | Description | Example Default |
| --- | --- | --- |
| `min_participation_ratio` | Fraction of recent active users that must vote before a case can pass. `0.05` equals “one vote per 20 members”. | `0.05` |
| `min_participation_count` | Hard floor for participant count regardless of ratio; protects small chats. | `5` |
| `approval_ratio` | Share of “Spam” votes required to convict; `0.6` = 60%. | `0.6` |
| `quorum_strategy` | Determines whether ratio, count, or both must be satisfied (`ratio_only`, `count_only`, `ratio_and_count`). | `ratio_and_count` |
| `action_on_confirm` | Enforcement after conviction: `ban`, `kick`, `delete_only`, or `mute`. | `ban` |
| `mute_duration_sec` | Length of mute when `action_on_confirm = "mute"`. | `3600` |
| `blacklist_enabled` | When true, convicted users are added to the blacklist for future automation. | `true` |
| `vote_timeout_sec` | Lifetime of each case before it auto-expires (seconds). | `14400` |
| `allow_vote_retract` | Enables “retract vote” button so members can change their mind. | `true` |
| `max_cases_per_user_hour` | Per-user rate limit for `/spam` reports to prevent abuse. | `3` |
| `auto_close_on_deleted_msg` | Whether to invalidate a case if the target message disappears. | `true` |
| `min_account_age_hours` | Ignore reporters/voters whose accounts are younger than the threshold; `0` disables the rule. | `0` |

### Example `config.toml`
```toml
[bot]
token_file = ".env"          # read BOT_TOKEN key from file
storage_url = "sqlite:///jurybot.db"
log_level = "INFO"

[defaults]
min_participation_ratio = 0.05
min_participation_count = 5
approval_ratio = 0.6
vote_timeout_sec = 14400
action_on_confirm = "ban"
blacklist_enabled = true
allow_vote_retract = true

[admin_ui]
owner_ids = [123456789]
```

### 配置项详解
- `BOT_TOKEN`（`.env` 中）：Telegram BotFather 提供的密钥，JuryBot 通过它调用 API。
- `token_file`：指向 `.env` 或其他包含 `BOT_TOKEN=...` 的文本文件，便于在不同环境复用。
- `storage_url`：数据库连接串，目前支持 `sqlite:///path/to.db`，未来可拓展到 Cloudflare D1。
- `log_level`：Python `logging` 级别，用于调试（`DEBUG`）或生产（`INFO`）。
- `owner_ids`：Bot 绝对管理员 ID（数组），即使不是群管理员也能远程修改配置。
- 其余字段参考上表，可在 `config.toml` 或群聊专属的数据库覆盖项中逐一细化。

`.env` 示例：
```
BOT_TOKEN=1234567890:ABCDEF_your_token_here
```

## 5. Admin PM Control Surface
- `/start` – lists chats where the bot is admin; buttons to manage each.
- `/config <chat_id>` – shows current settings; inline buttons to edit each field.
- Inline edit patterns:
  - Ratio sliders (predefined values 1/20, 1/10, 1/2, custom input).
  - Action selection (ban/kick/mute duration).
  - Toggle switches for blacklist, timers, auto-close.
- `/stats <chat_id>` – displays pending cases, recent decisions, participation stats.
- `/blacklist add|remove <chat_id> <user_id>` – manual override.
- `/cases <chat_id>` – shows open cases with quick resolve buttons (force close, forgive).

All PM commands require the requester to be an admin of the chat; verification done via Telegram `getChatAdministrators`.

## 6. Data & Storage
### Demo (Windows)
- **DB**: SQLite file `jurybot.db` placed next to `config.toml`.
- Tables:
  - `chats` (id, title, settings JSON, last_config_sync).
  - `cases` (id, chat_id, message_id, offender_id, reporter_id, status, opened_at, closes_at, config_snapshot JSON).
  - `votes` (case_id, voter_id, decision, updated_at).
  - `blacklist` (chat_id, user_id, reason, added_at).

### Future (Debian + Cloudflare)
- Cloudflare **D1** (relational SQLite-compatible) for structured data (cases, votes, configs).
- Cloudflare **R2** or Workers KV (the “R series”) for archival logs / attachments if ever needed. D1 handles transactional data; R2/KV handles cheap blob/kv storage.
- Abstract storage behind a repository layer so switching from SQLite ➜ D1 is a connection-string change.

## 7. Architecture
- Language: Python 3.12+ managed by `uv`.
- Telegram SDK: `aiogram` v3 (async, supports inline buttons, callback handling).
- Layers:
  1. **Bot Entrypoint (`main.py`)** – loads config file, initializes services, starts polling or webhook server.
  2. **Config Service** – merges TOML defaults, `.env` token, and per-chat overrides from DB.
  3. **Case Service** – encapsulates case lifecycle (open, vote, evaluate, close) with transactional safety.
  4. **Admin Service** – manages chat settings via PM commands.
  5. **Storage Adapter** – SQLite repository implementing interfaces for chats, cases, votes, blacklist.
  6. **Action Executor** – wraps Telegram API calls (delete message, ban, etc.) with retry handling and audit logging.
- Background task scheduler (async loop) handles vote timeouts and stale-case clean-up.

Sequence (vote):
```
report /spam -> CaseService.ensure_case() -> inline message -> user clicks
vote callback -> CaseService.record_vote() -> ThresholdEvaluator.evaluate()
if passed -> ActionExecutor.enforce() -> close case -> update inline text/log
```

## 8. Security & Abuse Mitigation
- Rate-limit `/spam` reports per user per hour.
- Ignore votes from recently joined accounts (< config.min_account_age, optional).
- Store hashed user IDs if privacy demanded.
- Admin override commands require double confirmation before banning via PM.
- Apply Telegram `anti_flood_timeout` when deleting/banning in bulk.

## 9. Deployment Plan
### Demo (local Windows)
1. Install uv + Python 3.12.
2. `uv init` already done; run `uv venv && uv pip install aiogram python-dotenv toml`.
3. Ensure `.env` contains `BOT_TOKEN=...`.
4. Create `config.toml` using template above.
5. `uv run python main.py` (long-poll mode).

### Staging/Production (Debian)
1. Use systemd service running `uv run python -m jurybot`.
2. Mount config + SQLite (or connect to Cloudflare D1 via HTTP API).
3. Set webhook with HTTPS endpoint behind Cloudflare Tunnel or Workers if needed.
4. Add observability (structured logs shipped to Loki/Cloudflare Logs).

## 10. Roadmap
1. **MVP**
   - Implement config loader, SQLite adapter, `/spam` flow, inline buttons, threshold logic.
   - Basic admin PM: `/start`, `/config` view, manual YAML/TOML edits only.
2. **Config UI**
   - Inline editing, validation, persistence per chat.
3. **Advanced Enforcement**
   - Blacklist service, action choices (mute duration), logging channel notifications.
4. **Scalability**
   - Switch to webhook mode, add caching for chat admin lists, deploy on Debian.
5. **Cloudflare Migration**
   - Abstract storage, add D1 migrations, evaluate R2/KV for log storage.
6. **Polish**
   - Localization, analytics, backups, integration tests.

## 11. Next Steps
- Scaffold package layout (`jurybot/` package with services).
- Implement config loader + storage layer.
- Build core `/spam` detection flow with inline voting.
- Add admin PM command surface.
- Write unit tests for threshold evaluator and case transitions.

## 12. Repository Guide
- `jurybot/` 包含应用源码：`config.py`（配置解析）、`storage.py`（SQLite 适配器）、`services/`（投票与管理员逻辑）、`app.py`（Aiogram 装配）。
- `config.example.toml` 提供了完整的默认配置，复制为 `config.toml` 后按需修改。
- `tests/` 持有 Pytest 用例，覆盖阈值判断与案例状态流转。
- `DEPLOY.md` 记录了 Windows 与 Debian 的逐条部署命令，可按图索骥上线。

### 本地运行
1. `uv pip install -e .[test]` 安装依赖（含测试插件）。
2. 复制 `config.example.toml` 为 `config.toml`，在 `.env` 中写入 `BOT_TOKEN=...`。
3. `.\.venv\Scripts\python main.py` 或 `uv run python -m jurybot.app --config config.toml` 启动长轮询 Bot。（确保已 `uv pip install -e .`，或在仓库根目录运行以便 Python 直接找到 `jurybot` 包。）

### 运行测试
```
.\.venv\Scripts\python -m pytest
```
