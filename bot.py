# bot.py — ИСПРАВЛЕННЫЙ

import os
import logging
import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ContextTypes,
    CallbackQueryHandler, ConversationHandler
)

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, FloodWaitError

from config import (
    BOT_TOKEN, ADMIN_ID, TELEGRAM_API_ID, TELEGRAM_API_HASH,
    SESSIONS_DIR, LOGS_DIR
)

# ==================== ЛОГИРОВАНИЕ ====================

Path(LOGS_DIR).mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(f'{LOGS_DIR}/bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==================== ЗАЩИТА ====================

def check_admin(func):
    """Декоратор для проверки ADMIN_ID"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if user_id != ADMIN_ID:
            logger.warning(f"❌ Unauthorized: {user_id}")
            
            if update.message:
                await update.message.reply_text("❌ Access denied")
            elif update.callback_query:
                await update.callback_query.answer("❌ Access denied", show_alert=True)
            
            return
        
        return await func(update, context)
    
    return wrapper

# ==================== ПЕРЕМЕННЫЕ ====================

Path(SESSIONS_DIR).mkdir(exist_ok=True)

active_clients: Dict[int, TelegramClient] = {}
captured_codes: Dict[int, Dict] = {}
pending_auth: Dict[str, Dict] = {}
admin_accounts: Dict[str, Dict] = {}
account_counter = 0

(
    AUTH_PHONE, AUTH_CODE, AUTH_2FA, 
    ACCOUNT_NAME
) = range(4)

# ==================== HELPER FUNCTIONS ====================

def get_session_path(phone: str) -> str:
    """Получить путь сессии"""
    clean_phone = phone.replace('+', '').replace(' ', '')
    return os.path.join(SESSIONS_DIR, f"account_{clean_phone}")

def extract_code_from_text(text: str) -> Optional[str]:
    """Парсить 5-значный код"""
    patterns = [
        r'(?:код|code)[\s:]*(\d{5})',
        r'(\d{5})\s+is\s+your',
        r'telegram[\s:]*(\d{5})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None

# ==================== TELETHON ====================

async def request_code(account_id: int, phone: str) -> Tuple[bool, str]:
    """Шаг 1: Отправить код"""
    try:
        session_path = get_session_path(phone)
        logger.info(f"🔐 Requesting code for {phone}")
        
        client = TelegramClient(session_path, TELEGRAM_API_ID, TELEGRAM_API_HASH)
        await client.connect()
        
        if await client.is_user_authorized():
            logger.info(f"✅ Already authorized: {phone}")
            active_clients[account_id] = client
            captured_codes[account_id] = {'code': None, 'timestamp': None, 'expires_at': None}
            start_listening(account_id, client)
            return True, "Already authorized"
        
        try:
            result = await client.send_code_request(phone)
            phone_code_hash = result.phone_code_hash
            
            pending_auth[phone] = {
                'account_id': account_id,
                'client': client,
                'phone_code_hash': phone_code_hash,
                'created_at': datetime.now()
            }
            
            logger.info(f"📨 Code sent to {phone}")
            return True, f"Code sent"
            
        except FloodWaitError as e:
            logger.error(f"❌ Flood: {e.seconds}s")
            return False, f"Wait {e.seconds}s"
        
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        return False, str(e)

async def verify_code(phone: str, code: str) -> Tuple[bool, str]:
    """Шаг 2: Проверить код"""
    if phone not in pending_auth:
        return False, "No pending code"
    
    try:
        auth_data = pending_auth[phone]
        client = auth_data['client']
        phone_code_hash = auth_data['phone_code_hash']
        account_id = auth_data['account_id']
        
        logger.info(f"🔐 Verifying {code}")
        
        try:
            await client.sign_in(phone, code, phone_code_hash=phone_code_hash)
            
            logger.info(f"✅ Signed in")
            
            me = await client.get_me()
            
            active_clients[account_id] = client
            del pending_auth[phone]
            
            captured_codes[account_id] = {'code': None, 'timestamp': None, 'expires_at': None}
            start_listening(account_id, client)
            
            return True, f"OK! {me.first_name}"
            
        except SessionPasswordNeededError:
            return False, "2FA_REQUIRED"
            
    except Exception as e:
        logger.error(f"❌ Error: {e}")
        return False, str(e)

async def verify_2fa(phone: str, password: str) -> Tuple[bool, str]:
    """Шаг 3: Проверить 2FA"""
    if phone not in pending_auth:
        return False, "No pending"
    
    try:
        auth_data = pending_auth[phone]
        client = auth_data['client']
        account_id = auth_data['account_id']
        
        logger.info(f"🔐 2FA check")
        
        await client.sign_in(password=password)
        
        active_clients[account_id] = client
        del pending_auth[phone]
        
        captured_codes[account_id] = {'code': None, 'timestamp': None, 'expires_at': None}
        start_listening(account_id, client)
        
        return True, "2FA OK!"
        
    except Exception as e:
        logger.error(f"❌ 2FA error: {e}")
        return False, str(e)

def start_listening(account_id: int, client: TelegramClient):
    """Запустить слушатель"""
    
    async def on_message(event):
        try:
            text = event.message.message
            if not text:
                return
            
            code = extract_code_from_text(text)
            
            if code:
                logger.info(f"🎯 CODE: {code}")
                
                captured_codes[account_id] = {
                    'code': code,
                    'timestamp': datetime.now(),
                    'expires_at': datetime.now() + timedelta(minutes=10)
                }
                
        except Exception as e:
            logger.error(f"Error: {e}")
    
    client.add_event_handler(on_message, events.NewMessage(incoming=True))
    logger.info(f"📡 Listening {account_id}")

def get_code(account_id: int) -> Optional[str]:
    """Получить код"""
    if account_id not in captured_codes:
        return None
    
    data = captured_codes[account_id]
    
    if not data['code']:
        return None
    
    if data['expires_at'] < datetime.now():
        captured_codes[account_id]['code'] = None
        return None
    
    return data['code']

# ==================== BOT HANDLERS ====================

@check_admin
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню"""
    keyboard = [
        [InlineKeyboardButton("➕ Add", callback_data='add_account')],
        [InlineKeyboardButton("📋 Accounts", callback_data='list_accounts')],
        [InlineKeyboardButton("❓ Help", callback_data='help')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "🎯 <b>Account Manager</b>",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

@check_admin
async def add_account_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавить аккаунт"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "📱 <b>Enter phone</b>\n\n"
        "+7XXXXXXXXXX",
        parse_mode='HTML'
    )
    
    return AUTH_PHONE

@check_admin
async def receive_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить номер"""
    phone = update.message.text.strip()
    
    if not phone.startswith('+') or len(phone) < 10:
        await update.message.reply_text("❌ Invalid:")
        return AUTH_PHONE
    
    global account_counter
    account_counter += 1
    account_id = account_counter
    
    context.user_data['phone'] = phone
    context.user_data['account_id'] = account_id
    
    await update.message.reply_text(f"⏳ Sending...")
    
    success, message = await request_code(account_id, phone)
    
    if success:
        await update.message.reply_text(
            f"✅ {message}\n\n"
            "📝 Enter code",
            parse_mode='HTML'
        )
        return AUTH_CODE
    else:
        await update.message.reply_text(f"❌ {message}")
        return ConversationHandler.END

@check_admin
async def receive_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить код"""
    code = update.message.text.strip()
    
    if not code.isdigit() or len(code) != 5:
        await update.message.reply_text("❌ 5 digits:")
        return AUTH_CODE
    
    phone = context.user_data['phone']
    account_id = context.user_data['account_id']
    
    await update.message.reply_text("⏳ Checking...")
    
    success, message = await verify_code(phone, code)
    
    if success == True:
        await update.message.reply_text(
            f"✅ {message}\n\n"
            "📝 Enter name",
            parse_mode='HTML'
        )
        return ACCOUNT_NAME
    elif message == "2FA_REQUIRED":
        await update.message.reply_text(
            "🔐 <b>2FA</b>\n\n"
            "Enter password",
            parse_mode='HTML'
        )
        return AUTH_2FA
    else:
        await update.message.reply_text(f"❌ {message}")
        return ConversationHandler.END

@check_admin
async def receive_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить 2FA"""
    password = update.message.text.strip()
    phone = context.user_data['phone']
    
    await update.message.reply_text("⏳ Checking...")
    
    success, message = await verify_2fa(phone, password)
    
    if success:
        await update.message.reply_text(
            f"✅ {message}\n\n"
            "📝 Enter name",
            parse_mode='HTML'
        )
        return ACCOUNT_NAME
    else:
        await update.message.reply_text(f"❌ {message}")
        return ConversationHandler.END

@check_admin
async def receive_account_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить имя"""
    name = update.message.text.strip()
    phone = context.user_data['phone']
    account_id = context.user_data['account_id']
    
    admin_accounts[phone] = {
        'name': name,
        'account_id': account_id,
        'added_at': datetime.now()
    }
    
    logger.info(f"✅ Added: {name}")
    
    await update.message.reply_text(
        f"✅ <b>Done!</b>\n\n"
        f"📝 {name}\n"
        f"📱 {phone}",
        parse_mode='HTML'
    )
    
    context.user_data.clear()
    return ConversationHandler.END

@check_admin
async def list_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список аккаунтов"""
    query = update.callback_query
    await query.answer()
    
    if not admin_accounts:
        await query.edit_message_text("📭 Empty")
        return
    
    text = "📋 <b>Accounts:</b>\n\n"
    keyboard = []
    
    for phone, data in admin_accounts.items():
        text += f"📱 <b>{data['name']}</b>\n   {phone}\n\n"
        account_id = data['account_id']
        keyboard.append([
            InlineKeyboardButton(
                f"Get - {data['name']}", 
                callback_data=f"get_code_{account_id}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("◀️ Back", callback_data='back')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')

@check_admin
async def get_code_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получить код"""
    query = update.callback_query
    await query.answer()
    
    account_id = int(query.data.split('_')[2])
    code = get_code(account_id)
    
    if code:
        text = (
            f"✅ <b>CODE:</b>\n\n"
            f"<code>{code}</code>\n\n"
            f"⏱️ 10min"
        )
    else:
        text = (
            f"⏳ <b>Waiting...</b>\n\n"
            f"Enter phone in Telegram"
        )
    
    keyboard = [
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"get_code_{account_id}")],
        [InlineKeyboardButton("◀️ Back", callback_data='list_accounts')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')

@check_admin
async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помощь"""
    query = update.callback_query
    await query.answer()
    
    text = (
        "❓ <b>How:</b>\n\n"
        "1. Add Account\n"
        "2. Enter phone\n"
        "3. Enter code\n"
        "4. 2FA password (if needed)\n"
        "5. Enter name\n"
        "6. My Accounts\n"
        "7. Get Code"
    )
    
    keyboard = [[InlineKeyboardButton("◀️ Back", callback_data='back')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='HTML')

@check_admin
async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вернуться"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("➕ Add", callback_data='add_account')],
        [InlineKeyboardButton("📋 Accounts", callback_data='list_accounts')],
        [InlineKeyboardButton("❓ Help", callback_data='help')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🎯 <b>Account Manager</b>",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

# ==================== ЗАПУСК ====================

def main():
    """Запуск БЕЗ asyncio.run()"""
    logger.info("🚀 Bot starting...")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    add_account_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_account_start, pattern='add_account')],
        states={
            AUTH_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)],
            AUTH_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_code)],
            AUTH_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_2fa)],
            ACCOUNT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_account_name)],
        },
        fallbacks=[],
        per_message=True
    )
    
    app.add_handler(CommandHandler('start', start))
    app.add_handler(add_account_conv)
    
    app.add_handler(CallbackQueryHandler(list_accounts, pattern='list_accounts'))
    app.add_handler(CallbackQueryHandler(get_code_button, pattern='get_code_'))
    app.add_handler(CallbackQueryHandler(help_handler, pattern='help'))
    app.add_handler(CallbackQueryHandler(back_to_main, pattern='back'))
    
    logger.info("✅ Ready")
    app.run_polling()

if __name__ == '__main__':
    main()
