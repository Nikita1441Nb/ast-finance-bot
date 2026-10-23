import os
import sys
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo
from datetime import datetime
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Almaty")
TIMEZONE = ZoneInfo(TIMEZONE_NAME)

_admins_raw = os.getenv("SUPER_ADMINS", "")
SUPER_ADMINS = [u.strip().lstrip("@").lower() for u in _admins_raw.split(",") if u.strip()]

USERS_FILE = BASE_DIR / "users.txt"
DB_PATH = BASE_DIR / "bot.db"
LOG_FILE = BASE_DIR / "bot.log"

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] (%(name)s): %(message)s",
    level=logging.INFO,
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("ASTGroupBot")

def get_current_time() -> datetime:
    return datetime.now(TIMEZONE)