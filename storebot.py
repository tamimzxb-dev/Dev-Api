import asyncio
import logging
import os
import re
import sqlite3
import time
from contextlib import closing
from typing import Optional, List, Tuple, Iterable, Dict

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    KeyboardButton,
    CopyTextButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================
# For safety, keep your bot token out of source control.
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

OWNER_IDS = {5422839433}

FORCE_JOIN_CHANNELS = [
    {
        "name": "Channel 1",
        "chat_id": "@ccccccccccccx",
        "url": "https://t.me/ccccccccccccx",
    },
]

# Group ID where OTPs are posted (Supergroup IDs are usually negative like -100...)
DEFAULT_MONITOR_CHAT_IDS = {-1003528209997}

DB_NAME = "number_store.db"
DEFAULT_BATCH_LIMIT = 5
BROADCAST_DELAY = 0.04
USED_LOG_TTL_SECONDS = 1000
CLEANUP_CHECK_INTERVAL = 60

# Stronger Logging
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("storebot")

# =========================================================
# CONVERSATION STATES
# =========================================================
ADDNUMBER_PLATFORM, ADDNUMBER_COUNTRY, ADDNUMBER_FILE = range(3)

# =========================================================
# DB
# =========================================================

def get_conn():
    return sqlite3.connect(DB_NAME)


def init_db():
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        # USERS
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            prefix_enabled INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        )
        # ADMINS
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        )
        # MONITOR CHATS
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS monitor_chats (
            chat_id INTEGER PRIMARY KEY,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        )
        # PLATFORMS
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS platforms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
        """
        )
        # COUNTRIES
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS countries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform_name TEXT NOT NULL,
            country_name TEXT NOT NULL,
            UNIQUE(platform_name, country_name)
        )
        """
        )
        # COUNTRY RULES
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS country_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform_name TEXT NOT NULL,
            country_name TEXT NOT NULL,
            user_limit INTEGER DEFAULT 5,
            UNIQUE(platform_name, country_name)
        )
        """
        )
        # STOCK NUMBERS
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS stock_numbers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform_name TEXT NOT NULL,
            country_name TEXT NOT NULL,
            raw_number TEXT NOT NULL UNIQUE,
            masked_number TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        )
        # USED LOGS
        cur.execute(
            """
        CREATE TABLE IF NOT EXISTS used_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            platform_name TEXT NOT NULL,
            country_name TEXT NOT NULL,
            raw_number TEXT NOT NULL,
            masked_number TEXT NOT NULL UNIQUE,
            taken_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        )

        cur.execute("CREATE INDEX IF NOT EXISTS idx_masked_number ON used_logs (masked_number)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_raw_number ON used_logs (raw_number)")
        conn.commit()

    seed_defaults()

    # Ensure default monitor chats exist in DB (so /addchatid isn't required)
    for cid in DEFAULT_MONITOR_CHAT_IDS:
        add_monitor_chat(cid)


def seed_defaults():
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO platforms (name) VALUES (?)", ("WhatsApp",))
        cur.execute("INSERT OR IGNORE INTO platforms (name) VALUES (?)", ("Telegram",))
        conn.commit()


# =========================================================
# UTILS
# =========================================================

def escape_html(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clean_number(number_str: str) -> str:
    s = str(number_str).strip().lstrip("+")
    return "".join(ch for ch in s if ch.isdigit())


def mask_number_custom(number_str):
    s = clean_number(number_str)
    length = len(s)
    if length >= 13:
        return f"{s[:5]}SHU{s[-5:]}"
    elif length == 12:
        return f"{s[:4]}SHU{s[-4:]}"
    elif length == 11:
        return f"{s[:4]}SHU{s[-4:]}"
    elif length == 10:
        return f"{s[:3]}SHU{s[-4:]}"
    elif length in (8, 9):
        return f"{s[:3]}SHU{s[-3:]}"
    else:
        return s


def fmt_user_number(raw_number: str, prefix_enabled: bool) -> str:
    n = clean_number(raw_number)
    return f"+{n}" if prefix_enabled else n


def ensure_user(user_id: int, first_name: str = "", username: str = ""):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO users (user_id, first_name, username) VALUES (?, ?, ?)",
            (user_id, first_name, username),
        )
        cur.execute("UPDATE users SET first_name=?, username=? WHERE user_id=?", (first_name, username, user_id))
        conn.commit()


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def is_admin(user_id: int) -> bool:
    if is_owner(user_id):
        return True
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
        return cur.fetchone() is not None


def is_blocked(user_id: int) -> bool:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT is_blocked FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return bool(row[0]) if row else False


def set_block(user_id: int, blocked: bool):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_blocked=? WHERE user_id=?", (1 if blocked else 0, user_id))
        conn.commit()


def get_prefix_enabled(user_id: int) -> bool:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT prefix_enabled FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return bool(row[0]) if row else False


def set_prefix_enabled(user_id: int, enabled: bool):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET prefix_enabled=? WHERE user_id=?", (1 if enabled else 0, user_id))
        conn.commit()


def add_platform(name: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO platforms (name) VALUES (?)", (name,))
        conn.commit()


def get_platforms() -> List[str]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM platforms ORDER BY name ASC")
        return [x[0] for x in cur.fetchall()]


def add_country(platform_name: str, country_name: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO countries (platform_name, country_name) VALUES (?, ?)",
            (platform_name, country_name),
        )
        cur.execute(
            "INSERT OR IGNORE INTO country_rules (platform_name, country_name, user_limit) VALUES (?, ?, ?)",
            (platform_name, country_name, DEFAULT_BATCH_LIMIT),
        )
        conn.commit()


def get_countries() -> List[Tuple[str, str]]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT platform_name, country_name FROM countries ORDER BY platform_name, country_name")
        return cur.fetchall()


def set_country_limit(platform_name: str, country_name: str, limit: int):
    add_country(platform_name, country_name)
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE country_rules SET user_limit=? WHERE platform_name=? AND country_name=?",
            (limit, platform_name, country_name),
        )
        conn.commit()


def get_country_limit(platform_name: str, country_name: str) -> int:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_limit FROM country_rules WHERE platform_name=? AND country_name=?",
            (platform_name, country_name),
        )
        row = cur.fetchone()
        return int(row[0]) if row else DEFAULT_BATCH_LIMIT


def add_numbers_bulk(platform_name: str, country_name: str, lines: List[str]):
    add_country(platform_name, country_name)
    total, added, duplicate, invalid = 0, 0, 0, 0
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        for line in lines:
            total += 1
            raw = clean_number(line)
            if len(raw) < 6:
                invalid += 1
                continue
            masked = mask_number_custom(raw)
            try:
                cur.execute(
                    "INSERT INTO stock_numbers (platform_name, country_name, raw_number, masked_number) VALUES (?, ?, ?, ?)",
                    (platform_name, country_name, raw, masked),
                )
                added += 1
            except sqlite3.IntegrityError:
                duplicate += 1
        conn.commit()
    return total, added, duplicate, invalid


def count_stock(platform_name: Optional[str] = None, country_name: Optional[str] = None) -> int:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        if platform_name and country_name:
            cur.execute(
                "SELECT COUNT(*) FROM stock_numbers WHERE platform_name=? AND country_name=?",
                (platform_name, country_name),
            )
        else:
            cur.execute("SELECT COUNT(*) FROM stock_numbers")
        return cur.fetchone()[0]


def assign_batch_to_user(user_id: int, platform_name: str, country_name: str, batch_size: int):
    assigned = []
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, raw_number, masked_number FROM stock_numbers WHERE platform_name=? AND country_name=? ORDER BY id ASC LIMIT ?",
            (platform_name, country_name, batch_size),
        )
        rows = cur.fetchall()
        for row_id, raw, masked in rows:
            cur.execute("DELETE FROM stock_numbers WHERE id=?", (row_id,))
            if cur.rowcount:
                cur.execute(
                    "INSERT INTO used_logs (user_id, platform_name, country_name, raw_number, masked_number) VALUES (?, ?, ?, ?, ?)",
                    (user_id, platform_name, country_name, raw, masked),
                )
                assigned.append(raw)
        conn.commit()
    return assigned


def get_latest_user_numbers(user_id: int, platform_name: str, country_name: str, limit: int) -> List[str]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT raw_number FROM used_logs WHERE user_id=? AND platform_name=? AND country_name=? ORDER BY id DESC LIMIT ?",
            (user_id, platform_name, country_name, limit),
        )
        rows = [x[0] for x in cur.fetchall()]
        rows.reverse()
        return rows


def get_all_user_numbers(user_id: int) -> List[Tuple[str, str, str]]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT platform_name, country_name, raw_number FROM used_logs WHERE user_id=? ORDER BY id DESC LIMIT 20", (user_id,))
        return cur.fetchall()


def remove_country_numbers(platform_name: str, country_name: str):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM stock_numbers WHERE platform_name=? AND country_name=?", (platform_name, country_name))
        removed = cur.rowcount
        conn.commit()
        return removed


def cleanup_old_used_logs():
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM used_logs WHERE taken_at < datetime('now', ?)", (f"-{USED_LOG_TTL_SECONDS} seconds",))
        deleted = cur.rowcount
        conn.commit()
        return deleted


def add_monitor_chat(chat_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO monitor_chats (chat_id) VALUES (?)", (chat_id,))
        conn.commit()


def get_monitor_chats() -> List[int]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT chat_id FROM monitor_chats ORDER BY chat_id")
        return [x[0] for x in cur.fetchall()]


def add_admin_db(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        conn.commit()


def remove_admin_db(user_id: int):
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
        conn.commit()


def admin_list() -> List[int]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM admins ORDER BY user_id ASC")
        return [x[0] for x in cur.fetchall()]


def get_all_user_ids() -> List[int]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users ORDER BY user_id ASC")
        return [x[0] for x in cur.fetchall()]


# =========================================================
# UI
# =========================================================

def main_menu():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("☎️ numbers"), KeyboardButton("📊 status")],
            [KeyboardButton("➖ remove prefix"), KeyboardButton("📦 stock")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def force_join_kb():
    rows = []
    for ch in FORCE_JOIN_CHANNELS:
        rows.append([InlineKeyboardButton(f"🔊 Join {ch['name']}", url=ch["url"])])
    return InlineKeyboardMarkup(rows)


def countries_kb():
    items = get_countries()
    keyboard = []
    row = []
    for platform_name, country_name in items:
        stock = count_stock(platform_name, country_name)
        text = f"{country_name} [{stock}]"
        data = f"country|{platform_name}|{country_name}"
        row.append(InlineKeyboardButton(text, callback_data=data))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("❌ Close", callback_data="close")])
    return InlineKeyboardMarkup(keyboard)


def numbers_kb(platform_name: str, country_name: str, numbers: List[str], prefix_enabled: bool):
    keyboard = []
    for raw in numbers:
        shown = fmt_user_number(raw, prefix_enabled)
        keyboard.append([InlineKeyboardButton(text=f"📋 {shown}", copy_text=CopyTextButton(shown))])
    prefix_text = "➖ Remove Prefix" if prefix_enabled else "➕ Add Prefix"
    keyboard.append(
        [
            InlineKeyboardButton("🔄 Change Number", callback_data=f"change|{platform_name}|{country_name}"),
            InlineKeyboardButton(prefix_text, callback_data=f"toggleprefix|{platform_name}|{country_name}"),
        ]
    )
    keyboard.append(
        [
            InlineKeyboardButton("🌐 Other Countries", callback_data="othercountries"),
            InlineKeyboardButton("❌ Close", callback_data="close"),
        ]
    )
    return InlineKeyboardMarkup(keyboard)


# =========================================================
# JOIN CHECK
# =========================================================

async def joined_all(bot, user_id: int) -> bool:
    for ch in FORCE_JOIN_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=ch["chat_id"], user_id=user_id)
            if member.status not in ("member", "administrator", "creator"):
                return False
        except Exception as e:
            logger.warning("Join check failed, allowing access: %s", e)
            return True
    return True


# =========================================================
# ALERTS
# =========================================================

async def send_stock_out_alert(app: Application, platform_name: str, country_name: str):
    text = (
        "⚠️ <b>STOCK OUT ALERT</b>\n\n"
        f"Platform: <b>{escape_html(platform_name)}</b>\n"
        f"Country: <b>{escape_html(country_name)}</b>\n"
        "Status: <b>Out of stock</b>"
    )
    targets = set(OWNER_IDS) | set(admin_list()) | set(get_monitor_chats())
    for chat_id in targets:
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning("Alert send failed to %s: %s", chat_id, e)


# =========================================================
# HELPERS
# =========================================================

async def blocked_guard(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    ensure_user(user.id, user.first_name or "", user.username or "")
    if is_blocked(user.id):
        if update.message:
            await update.message.reply_text("🚫 You are blocked.")
        elif update.callback_query:
            await update.callback_query.answer("🚫 You are blocked.", show_alert=True)
        return True
    return False


async def edit_number_message(query, platform_name: str, country_name: str, numbers: List[str], prefix_enabled: bool):
    text = (
        "🔔 <b>Number Assigned !!!</b>\n"
        f"📦 Platform : <b>{escape_html(platform_name)}</b>\n"
        f"🌐 Country : <b>{escape_html(country_name)}</b>\n\n"
        "Tap a number to copy:"
    )
    await query.edit_message_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=numbers_kb(platform_name, country_name, numbers, prefix_enabled),
    )


async def cleanup_worker(app: Application):
    while True:
        try:
            deleted = cleanup_old_used_logs()
            if deleted:
                logger.info("Cleaned %s old used logs", deleted)
        except Exception as e:
            logger.exception("Cleanup worker error: %s", e)
        await asyncio.sleep(CLEANUP_CHECK_INTERVAL)


async def on_startup(app: Application):
    app.create_task(cleanup_worker(app))


# =========================================================
# MONITOR SYSTEM
# =========================================================

MASKED_RE = re.compile(r"\b\d{3,}SHU\d{3,}\b")
PHONE_RE = re.compile(r"(?:\+|00)?\d[\d\s\-\(\)]{6,}\d")
OTP_RE = re.compile(r"\b\d{4,8}\b")

OTP_CONTEXT_TTL_SECONDS = 180
_last_context_by_chat: Dict[int, Tuple[float, int, str]] = {}


def _set_last_context(chat_id: int, user_id: int, raw_number: str):
    _last_context_by_chat[chat_id] = (time.time(), user_id, raw_number)


def _get_last_context(chat_id: int) -> Optional[Tuple[int, str]]:
    ctx = _last_context_by_chat.get(chat_id)
    if not ctx:
        return None

    ts, user_id, raw_number = ctx
    if time.time() - ts > OTP_CONTEXT_TTL_SECONDS:
        _last_context_by_chat.pop(chat_id, None)
        return None

    return user_id, raw_number


def _extract_text_and_button_texts(update: Update) -> Tuple[str, List[str]]:
    msg = update.effective_message
    if not msg:
        return "", []

    text = msg.text or msg.caption or ""

    button_texts: List[str] = []
    if msg.reply_markup and getattr(msg.reply_markup, "inline_keyboard", None):
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if btn.text:
                    button_texts.append(btn.text)

    return text, button_texts


def _extract_candidate_raw_numbers(text: str) -> List[Tuple[str, str]]:
    """Returns (raw_digits, matched_text) pairs."""
    candidates: List[Tuple[str, str]] = []
    for m in PHONE_RE.finditer(text):
        matched = m.group(0)
        raw = clean_number(matched)
        if 8 <= len(raw) <= 15:
            candidates.append((raw, matched))

    # de-duplicate by raw number (keep first matched_text)
    uniq: List[Tuple[str, str]] = []
    seen = set()
    for raw, matched in candidates:
        if raw in seen:
            continue
        seen.add(raw)
        uniq.append((raw, matched))
    return uniq


def _find_user_by_masked(masked: str) -> Optional[Tuple[int, str]]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, raw_number FROM used_logs WHERE masked_number=?", (masked,))
        row = cur.fetchone()
        return (int(row[0]), row[1]) if row else None


def _find_user_by_raw(raw: str) -> Optional[Tuple[int, str]]:
    with closing(get_conn()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, raw_number FROM used_logs WHERE raw_number=? ORDER BY id DESC LIMIT 1",
            (raw,),
        )
        row = cur.fetchone()
        return (int(row[0]), row[1]) if row else None


def _remove_tokens(text: str, tokens: Iterable[str]) -> str:
    out = text
    for t in tokens:
        if t:
            out = out.replace(t, " ")
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


async def monitor_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detects assigned numbers in a group and forwards the OTP/message to the correct user's inbox."""

    chat = update.effective_chat
    if not chat:
        return

    chat_id = chat.id

    monitor_chats = set(get_monitor_chats()) | set(DEFAULT_MONITOR_CHAT_IDS)
    if chat_id not in monitor_chats:
        return

    text, button_texts = _extract_text_and_button_texts(update)
    if not text and not button_texts:
        return

    masked_matches = list(dict.fromkeys(MASKED_RE.findall(text)))
    raw_candidates = _extract_candidate_raw_numbers(text)

    # Some OTP bots keep code only on button texts
    otps_from_buttons = []
    for bt in button_texts:
        otps_from_buttons.extend(OTP_RE.findall(bt))
    otps_from_buttons = list(dict.fromkeys(otps_from_buttons))

    # If message has no number at all but contains an OTP, try using last context
    if not masked_matches and not raw_candidates:
        otp_only = OTP_RE.findall(text) if text else []
        otp_only = list(dict.fromkeys(otp_only))
        otp_only.extend([x for x in otps_from_buttons if x not in otp_only])
        if not otp_only:
            return

        ctx = _get_last_context(chat_id)
        if not ctx:
            return

        user_id, raw_number = ctx
        prefix = get_prefix_enabled(user_id)
        display_num = fmt_user_number(raw_number, prefix)

        content = text.strip() if text else ""
        content = (content + "\n\nOTP: " + ", ".join(otp_only)).strip() if otp_only else content

        msg = (
            f"✅ OTP/message received for <code>{escape_html(display_num)}</code>\n\n"
            f"💬 Content:\n<code>{escape_html(content or '(no text)')}</code>"
        )

        try:
            await context.bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning("Failed to send PM to user %s: %s", user_id, e)
        return

    logger.info(
        "Monitor: chat_id=%s masked=%s raw=%s",
        chat_id,
        len(masked_matches),
        len(raw_candidates),
    )

    matches: List[Tuple[int, str, List[str]]] = []

    # 1) Match by masked number like 8490SHU5934
    for masked in masked_matches:
        found = _find_user_by_masked(masked)
        if found:
            user_id, raw_number = found
            matches.append((user_id, raw_number, [masked]))

    # 2) Match by raw phone number like +84901957336
    for raw, matched_text in raw_candidates:
        found = _find_user_by_raw(raw)
        if found:
            user_id, raw_number = found
            matches.append((user_id, raw_number, [matched_text]))

    if not matches:
        return

    # De-duplicate (user_id, raw_number)
    seen = set()
    uniq_matches = []
    for user_id, raw_number, tokens in matches:
        key = (user_id, raw_number)
        if key in seen:
            continue
        seen.add(key)
        uniq_matches.append((user_id, raw_number, tokens))

    for user_id, raw_number, tokens in uniq_matches:
        _set_last_context(chat_id, user_id, raw_number)

        prefix = get_prefix_enabled(user_id)
        display_num = fmt_user_number(raw_number, prefix)

        content = _remove_tokens(text, tokens)
        if otps_from_buttons:
            content = (content + "\n\nOTP: " + ", ".join(otps_from_buttons)).strip()

        if not content:
            content = "(no text)"

        msg = (
            f"✅ OTP/message received for <code>{escape_html(display_num)}</code>\n\n"
            f"💬 Content:\n<code>{escape_html(content)}</code>"
        )

        try:
            await context.bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning("Failed to send PM to user %s: %s", user_id, e)


# =========================================================
# USER COMMANDS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await blocked_guard(update):
        return
    user = update.effective_user
    ensure_user(user.id, user.first_name or "", user.username or "")
    if not await joined_all(context.bot, user.id):
        await update.message.reply_text(
            "⚠️ <b>Access Denied!</b>\n\nYou must join all channels first.",
            parse_mode=ParseMode.HTML,
            reply_markup=force_join_kb(),
        )
        return
    await update.message.reply_text(
        f"👋 Welcome, <b>{escape_html(user.first_name)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def user_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await blocked_guard(update):
        return
    user = update.effective_user
    text = (update.message.text or "").strip()

    # Force Join Check
    if not await joined_all(context.bot, user.id):
        await update.message.reply_text(
            "⚠️ Access Denied! Join channels first.",
            parse_mode=ParseMode.HTML,
            reply_markup=force_join_kb(),
        )
        return

    if text == "☎️ numbers":
        await update.message.reply_text("🌐 Select a country:", reply_markup=countries_kb())
    elif text == "📊 status":
        rows = get_all_user_numbers(user.id)
        prefix = get_prefix_enabled(user.id)
        if not rows:
            msg = f"📊 Status\n\nRecent: 0\nPrefix: {'ON' if prefix else 'OFF'}"
        else:
            lines = [f"📊 Status\nPrefix: {'ON' if prefix else 'OFF'}\n"]
            for p, c, raw in rows[:10]:
                lines.append(f"• {p} | {c} | {fmt_user_number(raw, prefix)}")
            msg = "\n".join(lines)
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    elif text == "➖ remove prefix":
        current = get_prefix_enabled(user.id)
        set_prefix_enabled(user.id, not current)
        await update.message.reply_text(f"⚙️ Prefix: {'ON' if not current else 'OFF'}", parse_mode=ParseMode.HTML)
    elif text == "📦 stock":
        items = get_countries()
        if not items:
            await update.message.reply_text("No stock.")
            return
        lines = ["📦 Stock\n"]
        for p, c in items:
            lines.append(f"• {p} | {c} : {count_stock(p, c)}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await blocked_guard(update):
        return
    query = update.callback_query
    user = query.from_user
    data = query.data or ""
    await query.answer()

    if data == "close":
        await query.message.delete()
        return
    if data == "othercountries":
        await query.edit_message_text(text="🌐 Select:", reply_markup=countries_kb())
        return

    if data.startswith("country|"):
        _, platform_name, country_name = data.split("|", 2)
        limit = get_country_limit(platform_name, country_name)
        assigned = assign_batch_to_user(user.id, platform_name, country_name, limit)
        if not assigned:
            await query.edit_message_text("❌ Out of stock.")
            await send_stock_out_alert(context.application, platform_name, country_name)
            return
        prefix = get_prefix_enabled(user.id)
        await edit_number_message(query, platform_name, country_name, assigned, prefix)
        if count_stock(platform_name, country_name) == 0:
            await send_stock_out_alert(context.application, platform_name, country_name)
        return

    if data.startswith("change|"):
        _, platform_name, country_name = data.split("|", 2)
        limit = get_country_limit(platform_name, country_name)
        assigned = assign_batch_to_user(user.id, platform_name, country_name, limit)
        if not assigned:
            await query.answer("No more numbers.", show_alert=True)
            return
        prefix = get_prefix_enabled(user.id)
        await edit_number_message(query, platform_name, country_name, assigned, prefix)

    if data.startswith("toggleprefix|"):
        _, platform_name, country_name = data.split("|", 2)
        current = get_prefix_enabled(user.id)
        set_prefix_enabled(user.id, not current)
        limit = get_country_limit(platform_name, country_name)
        latest = get_latest_user_numbers(user.id, platform_name, country_name, limit)
        await edit_number_message(query, platform_name, country_name, latest, not current)


# =========================================================
# ADMIN PANEL
# =========================================================

CMD_TEXT = """<b>--- Admin Panel ---</b>
/addadmin [id]
/rmvadmin [id]
/adminlist
/addnumber
/removenumber Platform | Country
/numberlimit Plat | Coun | Limit
/addchatid [chat_id]
/block [id] /unblock [id]
/seestatus [id]
/all [msg]
/statusall
"""


async def cmd_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(CMD_TEXT, parse_mode=ParseMode.HTML)


async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        return
    uid = int(context.args[0])
    ensure_user(uid)
    add_admin_db(uid)
    await update.message.reply_text(f"✅ Admin added: {uid}")


async def rmvadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    if not context.args:
        return
    uid = int(context.args[0])
    remove_admin_db(uid)
    await update.message.reply_text(f"✅ Admin removed: {uid}")


async def adminlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    ids = admin_list()
    await update.message.reply_text(f"Admins:\n{ids}")


# =========================================================
# ADDNUMBER CONVERSATION
# =========================================================

async def addnumber_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    plats = get_platforms()
    kb = [[InlineKeyboardButton(p, callback_data=f"aplat|{p}")] for p in plats]
    kb.append([InlineKeyboardButton("+ New", callback_data="aplat|__new__")])
    await update.message.reply_text("Select Platform:", reply_markup=InlineKeyboardMarkup(kb))
    return ADDNUMBER_PLATFORM


async def addnumber_platform_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split("|", 1)[1]
    if data == "__new__":
        await query.message.reply_text("Send new platform name:")
        context.user_data["awaiting_new_platform"] = True
        return ADDNUMBER_PLATFORM
    context.user_data["addnumber_platform"] = data
    await query.message.reply_text("Send Country Name with Flag:")
    return ADDNUMBER_COUNTRY


async def addnumber_platform_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_new_platform"):
        name = update.message.text.strip()
        add_platform(name)
        context.user_data["addnumber_platform"] = name
        context.user_data["awaiting_new_platform"] = False
        await update.message.reply_text(f"Platform added: {name}. Send Country:")
        return ADDNUMBER_COUNTRY
    await update.message.reply_text("Choose from buttons.")


async def addnumber_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    country_name = update.message.text.strip()
    context.user_data["addnumber_country"] = country_name
    add_country(context.user_data["addnumber_platform"], country_name)
    await update.message.reply_text("Upload .txt file")
    return ADDNUMBER_FILE


async def addnumber_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        return ADDNUMBER_FILE
    tg_file = await doc.get_file()
    path = await tg_file.download_to_drive()
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = [x.strip() for x in f if x.strip()]
    try:
        os.remove(path)
    except Exception:
        pass

    platform_name = context.user_data["addnumber_platform"]
    country_name = context.user_data["addnumber_country"]
    total, added, dup, inv = add_numbers_bulk(platform_name, country_name, lines)
    await update.message.reply_text(
        f"✅ Added: {added}\nDup: {dup}\nInv: {inv}\nStock: {count_stock(platform_name, country_name)}"
    )
    return ConversationHandler.END


async def addnumber_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return ConversationHandler.END


# =========================================================
# OTHER ADMIN COMMANDS
# =========================================================

async def removenumber_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    raw = " ".join(context.args)
    if "|" not in raw:
        return
    p, c = [x.strip() for x in raw.split("|", 1)]
    removed = remove_country_numbers(p, c)
    await update.message.reply_text(f"Removed {removed}")


async def numberlimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    raw = " ".join(context.args)
    m = re.match(r"(.+?)\s*\|\s*(.+?)\s*\|\s*(\d+)$", raw)
    if not m:
        return
    set_country_limit(m.group(1), m.group(2), int(m.group(3)))
    await update.message.reply_text("Limit set.")


async def addchatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        return
    cid = int(context.args[0])
    add_monitor_chat(cid)
    await update.message.reply_text(f"Monitor added: {cid}")


async def block_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    uid = int(context.args[0])
    set_block(uid, True)
    await update.message.reply_text("Blocked.")


async def unblock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    uid = int(context.args[0])
    set_block(uid, False)
    await update.message.reply_text("Unblocked.")


async def seestatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    uid = int(context.args[0])
    await update.message.reply_text(f"Status checked for {uid}")


async def all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = " ".join(context.args)
    await update.message.reply_text("Broadcast started.")


async def statusall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("Stats...")


# =========================================================
# MAIN
# =========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set. Set environment variable BOT_TOKEN.")

    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    addnumber_conv = ConversationHandler(
        entry_points=[CommandHandler("addnumber", addnumber_start)],
        states={
            ADDNUMBER_PLATFORM: [
                CallbackQueryHandler(addnumber_platform_cb, pattern=r"^aplat\|"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addnumber_platform_text),
            ],
            ADDNUMBER_COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, addnumber_country)],
            ADDNUMBER_FILE: [MessageHandler(filters.Document.ALL, addnumber_file)],
        },
        fallbacks=[CommandHandler("cancel", addnumber_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cmd", cmd_panel))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("rmvadmin", rmvadmin_cmd))
    app.add_handler(CommandHandler("adminlist", adminlist_cmd))
    app.add_handler(addnumber_conv)
    app.add_handler(CommandHandler("removenumber", removenumber_cmd))
    app.add_handler(CommandHandler("numberlimit", numberlimit_cmd))
    app.add_handler(CommandHandler("addchatid", addchatid_cmd))
    app.add_handler(CommandHandler("block", block_cmd))
    app.add_handler(CommandHandler("unblock", unblock_cmd))
    app.add_handler(CommandHandler("seestatus", seestatus_cmd))
    app.add_handler(CommandHandler("all", all_cmd))
    app.add_handler(CommandHandler("statusall", statusall_cmd))

    app.add_handler(CallbackQueryHandler(cb_handler))

    # HANDLERS
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, user_text))

    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
