# config.py

import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")

DATABASE_URL = os.getenv("DATABASE_URL", "")

SESSIONS_DIR = os.getenv("SESSIONS_DIR", "./sessions")
LOGS_DIR = os.getenv("LOGS_DIR", "./logs")
