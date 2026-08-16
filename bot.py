import asyncio
import logging
import os
from pathlib import Path
from typing import Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from telethon import TelegramClient, functions
from telethon.errors import RPCError
from telethon.tl.types import User, Chat, Channel
from telethon.tl.functions.contacts import DeleteContactsRequest

from config import (
    BOT_TOKEN,
    ADMIN_ID,
    TELEGRAM_API_ID,
    TELEGRAM_API_HASH,
    SESSIONS_DIR,
    LOGS_DIR,
    DATABASE_URL,
)

from database import (
    init_db,
    close_db,
    upsert_account,
    list_accounts,
    get_account,
    save_maintenance_result,
)


# ============================================================
# LOGGING
# ============================================================

Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)
Path(SESSIONS_DIR).mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(
            os.path.join(LOGS_DIR, "bot.log"),
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger(__name__)


# ============================================================
# GLOBAL STATE
# ============================================================

active_clients: Dict[int, TelegramClient] = {}

maintenance_tasks: Dict[int, asyncio.Task] = {}


# ============================================================
# AUTHORIZATION
# ============================================================

def is_admin(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id == ADMIN_ID


def admin_only(update: Update) -> bool:
    user = update.effective_user

    if user is None:
        return False

    return is_admin(user.id)


# ============================================================
# SESSION HELPERS
# ============================================================

def normalize_phone(phone: str) -> str:
    return phone.strip().replace(" ", "").replace("-", "")


def get_session_path(phone: str) -> str:
    clean = normalize_phone(phone)

    if clean.startswith("+"):
        clean = clean[1:]

    return os.path.join(
        SESSIONS_DIR,
        f"account_{clean}",
    )


async def connect_existing_account(
    account_id: int,
    phone: str,
    session_path: str,
) -> tuple[bool, str]:
    """
    Подключает уже существующую авторизованную Telethon-сессию.

    Авторизация через OTP/2FA здесь намеренно отсутствует.
    """

    try:
        client = TelegramClient(
            session_path,
            TELEGRAM_API_ID,
            TELEGRAM_API_HASH,
        )

        await client.connect()

        authorized = await client.is_user_authorized()

        if not authorized:
            await client.disconnect()

            return (
                False,
                "Сессия существует, но аккаунт не авторизован.",
            )

        active_clients[account_id] = client

        me = await client.get_me()

        name = (
            getattr(me, "first_name", None)
            or getattr(me, "username", None)
            or phone
        )

        logger.info(
            "Connected account id=%s phone=%s name=%s",
            account_id,
            phone,
            name,
        )

        return True, str(name)

    except Exception as exc:
        logger.exception(
            "Unable to connect account %s",
            phone,
        )

        return False, str(exc)


async def get_client_for_account(
    account_id: int,
) -> Optional[TelegramClient]:

    existing = active_clients.get(account_id)

    if existing is not None:
        try:
            if existing.is_connected():
                if await existing.is_user_authorized():
                    return existing
        except Exception:
            logger.exception(
                "Existing client check failed: %s",
                account_id,
            )

    account = await get_account(account_id)

    if account is None:
        return None

    phone = account["phone"]
    session_path = account["session_path"]

    success, _ = await connect_existing_account(
        account_id,
        phone,
        session_path,
    )

    if not success:
        return None

    return active_clients.get(account_id)


# ============================================================
# UI
# ============================================================

def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📋 Мои аккаунты",
                    callback_data="accounts",
                )
            ],
            [
                InlineKeyboardButton(
                    "🧹 Обслуживание",
                    callback_data="maintenance",
                )
            ],
            [
                InlineKeyboardButton(
                    "📊 Статистика",
                    callback_data="stats",
                )
            ],
            [
                InlineKeyboardButton(
                    "❓ Помощь",
                    callback_data="help",
                )
            ],
        ]
    )


def accounts_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []

    for row in rows:
        account_id = int(row["id"])
        name = row["name"]

        buttons.append(
            [
                InlineKeyboardButton(
                    f"📱 {name}",
                    callback_data=f"account:{account_id}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton(
                "◀️ Назад",
                callback_data="back",
            )
        ]
    )

    return InlineKeyboardMarkup(buttons)


def account_keyboard(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🧹 Обслуживание",
                    callback_data=f"maintenance:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔌 Подключить",
                    callback_data=f"connect:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "◀️ Назад",
                    callback_data="accounts",
                )
            ],
        ]
    )


def maintenance_keyboard(
    account_id: int,
) -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🛡 Завершить остальные сессии",
                    callback_data=f"confirm:sessions:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "👤 Очистить контакты",
                    callback_data=f"confirm:contacts:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🗑 Очистить диалоги",
                    callback_data=f"confirm:dialogs:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "👥 Выйти из групп",
                    callback_data=f"confirm:groups:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "📢 Выйти из каналов",
                    callback_data=f"confirm:channels:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "💣 ПОЛНАЯ ОЧИСТКА",
                    callback_data=f"confirm:full:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "◀️ Назад",
                    callback_data=f"account:{account_id}",
                )
            ],
        ]
    )


def confirmation_keyboard(
    action: str,
    account_id: int,
) -> InlineKeyboardMarkup:

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "⚠️ ПОДТВЕРДИТЬ",
                    callback_data=f"execute:{action}:{account_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Отмена",
                    callback_data=f"maintenance:{account_id}",
                )
            ],
        ]
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not admin_only(update):
        if update.message:
            await update.message.reply_text(
                "❌ Access denied"
            )
        return

    await update.message.reply_text(
        "🎯 <b>Telegram Account Manager</b>\n\n"
        "Выбери действие:",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )


# ============================================================
# ACCOUNTS
# ============================================================

async def show_accounts(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer(
            "❌ Access denied",
            show_alert=True,
        )
        return

    await query.answer()

    rows = await list_accounts()

    if not rows:
        await query.edit_message_text(
            "📭 Аккаунтов пока нет.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "◀️ Назад",
                            callback_data="back",
                        )
                    ]
                ]
            ),
        )
        return

    text = "📋 <b>Аккаунты</b>\n\n"

    for row in rows:
        text += (
            f"🆔 <code>{row['id']}</code>\n"
            f"📝 {row['name']}\n"
            f"📱 {row['phone']}\n"
            f"🕒 {row['created_at']}\n\n"
        )

    await query.edit_message_text(
        text,
        reply_markup=accounts_keyboard(rows),
        parse_mode="HTML",
    )


async def show_account(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer(
            "❌ Access denied",
            show_alert=True,
        )
        return

    await query.answer()

    account_id = int(
        query.data.split(":")[1]
    )

    account = await get_account(account_id)

    if account is None:
        await query.edit_message_text(
            "❌ Аккаунт не найден.",
            reply_markup=main_keyboard(),
        )
        return

    text = (
        "📱 <b>Аккаунт</b>\n\n"
        f"🆔 ID: <code>{account['id']}</code>\n"
        f"📝 Имя: {account['name']}\n"
        f"📞 Телефон: {account['phone']}\n"
        f"📁 Сессия: <code>{account['session_path']}</code>\n"
        f"🕒 Добавлен: {account['created_at']}\n"
    )

    if account["last_cleanup_at"]:
        text += (
            f"\n🧹 Последняя очистка: "
            f"{account['last_cleanup_at']}\n"
        )

    await query.edit_message_text(
        text,
        reply_markup=account_keyboard(account_id),
        parse_mode="HTML",
    )


# ============================================================
# CONNECT
# ============================================================

async def connect_account(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer(
            "❌ Access denied",
            show_alert=True,
        )
        return

    await query.answer()

    account_id = int(
        query.data.split(":")[1]
    )

    account = await get_account(account_id)

    if account is None:
        await query.edit_message_text(
            "❌ Аккаунт не найден."
        )
        return

    await query.edit_message_text(
        "⏳ Подключаю существующую сессию..."
    )

    success, result = await connect_existing_account(
        account_id,
        account["phone"],
        account["session_path"],
    )

    if success:
        text = (
            "✅ <b>Подключено</b>\n\n"
            f"Аккаунт: {account['name']}\n"
            f"Пользователь: {result}"
        )
    else:
        text = (
            "❌ <b>Не удалось подключить</b>\n\n"
            f"<code>{result}</code>"
        )

    await query.edit_message_text(
        text,
        reply_markup=account_keyboard(account_id),
        parse_mode="HTML",
    )


# ============================================================
# MAINTENANCE MENU
# ============================================================

async def show_maintenance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer(
            "❌ Access denied",
            show_alert=True,
        )
        return

    await query.answer()

    account_id = int(
        query.data.split(":")[1]
    )

    account = await get_account(account_id)

    if account is None:
        await query.edit_message_text(
            "❌ Аккаунт не найден."
        )
        return

    await query.edit_message_text(
        "🧹 <b>Обслуживание аккаунта</b>\n\n"
        f"📱 {account['name']}\n"
        f"☎️ {account['phone']}\n\n"
        "⚠️ Операции ниже могут быть необратимыми.",
        reply_markup=maintenance_keyboard(account_id),
        parse_mode="HTML",
    )


# ============================================================
# CONFIRMATION
# ============================================================

ACTION_NAMES = {
    "sessions": "завершение остальных сессий",
    "contacts": "удаление контактов",
    "dialogs": "удаление диалогов",
    "groups": "выход из групп",
    "channels": "выход из каналов",
    "full": "ПОЛНУЮ ОЧИСТКУ",
}


async def confirm_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer(
            "❌ Access denied",
            show_alert=True,
        )
        return

    await query.answer()

    _, action, account_id_raw = query.data.split(":")

    account_id = int(account_id_raw)

    account = await get_account(account_id)

    if account is None:
        await query.edit_message_text(
            "❌ Аккаунт не найден."
        )
        return

    action_name = ACTION_NAMES.get(
        action,
        action,
    )

    await query.edit_message_text(
        "⚠️ <b>Подтверждение</b>\n\n"
        f"Аккаунт: <b>{account['name']}</b>\n"
        f"Операция: <b>{action_name}</b>\n\n"
        "После запуска отменить операцию может быть невозможно.",
        reply_markup=confirmation_keyboard(
            action,
            account_id,
        ),
        parse_mode="HTML",
    )


# ============================================================
# TERMINATE OTHER SESSIONS
# ============================================================

async def terminate_other_sessions(
    account_id: int,
    client: TelegramClient,
) -> dict:

    terminated = 0
    failed = 0
    current = 0

    result = await client(
        functions.account.GetAuthorizationsRequest()
    )

    for authorization in result.authorizations:
        if authorization.current:
            current += 1
            continue

        try:
            await client(
                functions.account.ResetAuthorizationRequest(
                    hash=authorization.hash
                )
            )

            terminated += 1

        except RPCError as exc:
            failed += 1

            logger.warning(
                "Failed to terminate session: %s",
                exc,
            )

    details = (
        f"terminated={terminated};"
        f"failed={failed};"
        f"current={current}"
    )

    await save_maintenance_result(
        account_id,
        "terminate_other_sessions",
        "ok" if failed == 0 else "partial",
        details,
    )

    return {
        "terminated": terminated,
        "failed": failed,
        "current": current,
    }


# ============================================================
# CONTACT CLEANUP
# ============================================================

async def clean_contacts(
    account_id: int,
    client: TelegramClient,
) -> dict:

    users = []

    async for dialog in client.iter_dialogs():
        entity = dialog.entity

        if not isinstance(entity, User):
            continue

        if getattr(entity, "bot", False):
            continue

        if getattr(entity, "deleted", False):
            continue

        if getattr(entity, "access_hash", None) is None:
            continue

        users.append(entity)

    deleted = 0
    failed = 0

    batch_size = 100

    for index in range(
        0,
        len(users),
        batch_size,
    ):
        batch = users[
            index:index + batch_size
        ]

        try:
            await client(
                DeleteContactsRequest(
                    id=batch
                )
            )

            deleted += len(batch)

        except RPCError as exc:
            failed += len(batch)

            logger.warning(
                "Contact batch failed: %s",
                exc,
            )

    details = (
        f"deleted={deleted};"
        f"failed={failed}"
    )

    await save_maintenance_result(
        account_id,
        "delete_contacts",
        "ok" if failed == 0 else "partial",
        details,
    )

    return {
        "deleted": deleted,
        "failed": failed,
    }


# ============================================================
# DELETE DIALOGS
# ============================================================

async def clean_dialogs(
    account_id: int,
    client: TelegramClient,
) -> dict:

    deleted = 0
    failed = 0

    dialogs = []

    async for dialog in client.iter_dialogs():
        dialogs.append(dialog)

    for dialog in dialogs:
        try:
            await client.delete_dialog(
                dialog.entity
            )

            deleted += 1

        except RPCError as exc:
            failed += 1

            logger.warning(
                "Dialog deletion failed for %s: %s",
                dialog.name,
                exc,
            )

    details = (
        f"deleted={deleted};"
        f"failed={failed}"
    )

    await save_maintenance_result(
        account_id,
        "delete_dialogs",
        "ok" if failed == 0 else "partial",
        details,
    )

    return {
        "deleted": deleted,
        "failed": failed,
    }


# ============================================================
# LEAVE GROUPS / CHANNELS
# ============================================================

async def leave_groups(
    account_id: int,
    client: TelegramClient,
) -> dict:

    left = 0
    failed = 0

    dialogs = []

    async for dialog in client.iter_dialogs():
        entity = dialog.entity

        if isinstance(entity, Chat):
            dialogs.append(entity)

        elif isinstance(entity, Channel):
            if getattr(entity, "megagroup", False):
                dialogs.append(entity)

    for entity in dialogs:
        try:
            await client.delete_dialog(entity)
            left += 1

        except RPCError as exc:
            failed += 1

            logger.warning(
                "Group leave failed: %s",
                exc,
            )

    details = (
        f"left={left};"
        f"failed={failed}"
    )

    await save_maintenance_result(
        account_id,
        "leave_groups",
        "ok" if failed == 0 else "partial",
        details,
    )

    return {
        "left": left,
        "failed": failed,
    }


async def leave_channels(
    account_id: int,
    client: TelegramClient,
) -> dict:

    left = 0
    failed = 0

    dialogs = []

    async for dialog in client.iter_dialogs():
        entity = dialog.entity

        if isinstance(entity, Channel):
            if not getattr(entity, "megagroup", False):
                dialogs.append(entity)

    for entity in dialogs:
        try:
            await client.delete_dialog(entity)
            left += 1

        except RPCError as exc:
            failed += 1

            logger.warning(
                "Channel leave failed: %s",
                exc,
            )

    details = (
        f"left={left};"
        f"failed={failed}"
    )

    await save_maintenance_result(
        account_id,
        "leave_channels",
        "ok" if failed == 0 else "partial",
        details,
    )

    return {
        "left": left,
        "failed": failed,
    }


# ============================================================
# FULL CLEANUP
# ============================================================

async def full_cleanup(
    account_id: int,
    client: TelegramClient,
) -> dict:

    result = {}

    result["sessions"] = await terminate_other_sessions(
        account_id,
        client,
    )

    result["contacts"] = await clean_contacts(
        account_id,
        client,
    )

    result["dialogs"] = await clean_dialogs(
        account_id,
        client,
    )

    return result


# ============================================================
# EXECUTE MAINTENANCE
# ============================================================

async def execute_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer(
            "❌ Access denied",
            show_alert=True,
        )
        return

    await query.answer()

    _, action, account_id_raw = query.data.split(":")

    account_id = int(account_id_raw)

    if account_id in maintenance_tasks:
        task = maintenance_tasks[account_id]

        if not task.done():
            await query.edit_message_text(
                "⏳ Для этого аккаунта уже выполняется операция."
            )
            return

    account = await get_account(account_id)

    if account is None:
        await query.edit_message_text(
            "❌ Аккаунт не найден."
        )
        return

    client = await get_client_for_account(
        account_id
    )

    if client is None:
        await query.edit_message_text(
            "❌ Не удалось подключить аккаунт.\n\n"
            "Проверь существующую Telethon-сессию."
        )
        return

    await query.edit_message_text(
        "⏳ <b>Операция запущена...</b>\n\n"
        f"📱 {account['name']}",
        parse_mode="HTML",
    )

    async def runner():
        try:
            if action == "sessions":
                result = await terminate_other_sessions(
                    account_id,
                    client,
                )

                text = (
                    "✅ <b>Сессии обработаны</b>\n\n"
                    f"Закрыто: {result['terminated']}\n"
                    f"Ошибок: {result['failed']}\n"
                    f"Текущая: сохранена"
                )

            elif action == "contacts":
                result = await clean_contacts(
                    account_id,
                    client,
                )

                text = (
                    "✅ <b>Контакты обработаны</b>\n\n"
                    f"Удалено: {result['deleted']}\n"
                    f"Ошибок: {result['failed']}"
                )

            elif action == "dialogs":
                result = await clean_dialogs(
                    account_id,
                    client,
                )

                text = (
                    "✅ <b>Диалоги обработаны</b>\n\n"
                    f"Удалено: {result['deleted']}\n"
                    f"Ошибок: {result['failed']}"
                )

            elif action == "groups":
                result = await leave_groups(
                    account_id,
                    client,
                )

                text = (
                    "✅ <b>Группы обработаны</b>\n\n"
                    f"Покинуто: {result['left']}\n"
                    f"Ошибок: {result['failed']}"
                )

            elif action == "channels":
                result = await leave_channels(
                    account_id,
                    client,
                )

                text = (
                    "✅ <b>Каналы обработаны</b>\n\n"
                    f"Покинуто: {result['left']}\n"
                    f"Ошибок: {result['failed']}"
                )

            elif action == "full":
                result = await full_cleanup(
                    account_id,
                    client,
                )

                sessions = result["sessions"]
                contacts = result["contacts"]
                dialogs = result["dialogs"]

                text = (
                    "💣 <b>Полная очистка завершена</b>\n\n"
                    "🛡 Сессии:\n"
                    f"  закрыто: {sessions['terminated']}\n"
                    f"  ошибок: {sessions['failed']}\n\n"
                    "👤 Контакты:\n"
                    f"  удалено: {contacts['deleted']}\n"
                    f"  ошибок: {contacts['failed']}\n\n"
                    "🗑 Диалоги:\n"
                    f"  удалено: {dialogs['deleted']}\n"
                    f"  ошибок: {dialogs['failed']}"
                )

            else:
                text = "❌ Неизвестная операция."

            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🧹 Обслуживание",
                                callback_data=(
                                    f"maintenance:{account_id}"
                                ),
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "◀️ Аккаунт",
                                callback_data=(
                                    f"account:{account_id}"
                                ),
                            )
                        ],
                    ]
                ),
                parse_mode="HTML",
            )

        except Exception as exc:
            logger.exception(
                "Maintenance operation failed"
            )

            await save_maintenance_result(
                account_id,
                action,
                "error",
                str(exc),
            )

            await query.edit_message_text(
                "❌ <b>Операция завершилась ошибкой</b>\n\n"
                f"<code>{str(exc)}</code>",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "◀️ Назад",
                                callback_data=(
                                    f"maintenance:{account_id}"
                                ),
                            )
                        ]
                    ]
                ),
                parse_mode="HTML",
            )

        finally:
            maintenance_tasks.pop(
                account_id,
                None,
            )

    task = asyncio.create_task(
        runner()
    )

    maintenance_tasks[account_id] = task


# ============================================================
# STATS
# ============================================================

async def stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer(
            "❌ Access denied",
            show_alert=True,
        )
        return

    await query.answer()

    rows = await list_accounts()

    connected = 0

    for account in rows:
        client = active_clients.get(
            int(account["id"])
        )

        if client is not None:
            try:
                if (
                    client.is_connected()
                    and await client.is_user_authorized()
                ):
                    connected += 1
            except Exception:
                pass

    await query.edit_message_text(
        "📊 <b>Статистика</b>\n\n"
        f"📱 Аккаунтов в БД: {len(rows)}\n"
        f"🟢 Активных подключений: {connected}\n"
        f"🔴 Не подключено: {len(rows) - connected}",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "◀️ Назад",
                        callback_data="back",
                    )
                ]
            ]
        ),
        parse_mode="HTML",
    )


# ============================================================
# HELP
# ============================================================

async def help_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer(
            "❌ Access denied",
            show_alert=True,
        )
        return

    await query.answer()

    await query.edit_message_text(
        "❓ <b>Telegram Account Manager</b>\n\n"
        "📋 Мои аккаунты — список аккаунтов.\n"
        "🔌 Подключить — подключение уже существующей сессии.\n"
        "🛡 Сессии — завершение других авторизаций.\n"
        "👤 Контакты — очистка контактов.\n"
        "🗑 Диалоги — удаление диалогов.\n"
        "👥 Группы — выход из групп.\n"
        "📢 Каналы — выход из каналов.\n"
        "💣 Полная очистка — последовательная очистка.\n\n"
        "Все операции доступны только ADMIN_ID.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "◀️ Назад",
                        callback_data="back",
                    )
                ]
            ]
        ),
        parse_mode="HTML",
    )


# ============================================================
# BACK
# ============================================================

async def back(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer(
            "❌ Access denied",
            show_alert=True,
        )
        return

    await query.answer()

    await query.edit_message_text(
        "🎯 <b>Telegram Account Manager</b>\n\n"
        "Выбери действие:",
        reply_markup=main_keyboard(),
        parse_mode="HTML",
    )


# ============================================================
# STARTUP
# ============================================================

async def post_init(
    application: Application,
):
    await init_db()

    logger.info(
        "PostgreSQL initialized"
    )


async def post_shutdown(
    application: Application,
):
    for account_id, client in list(
        active_clients.items()
    ):
        try:
            await client.disconnect()
        except Exception:
            logger.exception(
                "Failed to disconnect account %s",
                account_id,
            )

    active_clients.clear()

    await close_db()

    logger.info(
        "Shutdown complete"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not configured"
        )

    if not ADMIN_ID:
        raise RuntimeError(
            "ADMIN_ID is not configured"
        )

    if not TELEGRAM_API_ID:
        raise RuntimeError(
            "TELEGRAM_API_ID is not configured"
        )

    if not TELEGRAM_API_HASH:
        raise RuntimeError(
            "TELEGRAM_API_HASH is not configured"
        )

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not configured"
        )

    logger.info(
        "🚀 Starting Telegram Account Manager"
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # ----------------------------
    # Commands
    # ----------------------------

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # ----------------------------
    # Main menu
    # ----------------------------

    app.add_handler(
        CallbackQueryHandler(
            show_accounts,
            pattern=r"^accounts$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            show_maintenance,
            pattern=r"^maintenance:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            stats,
            pattern=r"^stats$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            help_handler,
            pattern=r"^help$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            back,
            pattern=r"^back$",
        )
    )

    # ----------------------------
    # Account
    # ----------------------------

    app.add_handler(
        CallbackQueryHandler(
            show_account,
            pattern=r"^account:\d+$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            connect_account,
            pattern=r"^connect:\d+$",
        )
    )

    # ----------------------------
    # Maintenance confirmation
    # ----------------------------

    app.add_handler(
        CallbackQueryHandler(
            confirm_action,
            pattern=(
                r"^confirm:"
                r"(sessions|contacts|dialogs|groups|channels|full)"
                r":\d+$"
            ),
        )
    )

    # ----------------------------
    # Maintenance execution
    # ----------------------------

    app.add_handler(
        CallbackQueryHandler(
            execute_action,
            pattern=(
                r"^execute:"
                r"(sessions|contacts|dialogs|groups|channels|full)"
                r":\d+$"
            ),
        )
    )

    logger.info(
        "✅ Bot is ready"
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
