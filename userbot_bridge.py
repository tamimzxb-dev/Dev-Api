import asyncio
import logging
import os
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from contextlib import closing
from typing import Optional, Tuple, List, Dict

from telethon import TelegramClient, events

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("userbot-bridge")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TG_API_ID = int(os.getenv("TG_API_ID", "0") or 0)
TG_API_HASH = os.getenv("TG_API_HASH", "")
TG_SESSION = os.getenv("TG_SESSION", "userbot")

DB_NAME = os.getenv("DB_NAME", "number_store.db")
MONITOR_CHAT_ID = int(os.getenv("MONITOR_CHAT_ID", "-1003528209997"))

MASKED_RE = re.compile(r"\b\d{3,}SHU\d{3,}\b")
PHONE_RE = re.compile(r"(?:\+|00)?\d[\d\s\-\(\)]{6,}\d")


def escape_html(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def clean_number(number_str: str) -> str:
    s = str(number_str).strip().lstrip("+")
    return "".join(ch for ch in s if ch.isdigit())


def fmt_user_number(raw_number: str, prefix_enabled: bool) -> str:
    n = clean_number(raw_number)
    return f"+{n}" if prefix_enabled else n


def get_prefix_enabled(user_id: int) -> bool:
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT prefix_enabled FROM users WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return bool(row[0]) if row else False


def find_user_by_masked(masked: str) -> Optional[Tuple[int, str]]:
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, raw_number FROM used_logs WHERE masked_number=?", (masked,))
        row = cur.fetchone()
        return (int(row[0]), row[1]) if row else None


def find_user_by_raw(raw: str) -> Optional[Tuple[int, str]]:
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, raw_number FROM used_logs WHERE raw_number=? ORDER BY id DESC LIMIT 1", (raw,))
        row = cur.fetchone()
        return (int(row[0]), row[1]) if row else None


def extract_raw_candidates(text: str) -> List[Tuple[str, str]]:
    candidates: List[Tuple[str, str]] = []
    for m in PHONE_RE.finditer(text):
        matched = m.group(0)
        raw = clean_number(matched)
        if 8 <= len(raw) <= 15:
            candidates.append((raw, matched))

    uniq: List[Tuple[str, str]] = []
    seen = set()
    for raw, matched in candidates:
        if raw in seen:
            continue
        seen.add(raw)
        uniq.append((raw, matched))
    return uniq


def remove_tokens(text: str, tokens: List[str]) -> str:
    out = text
    for t in tokens:
        if t:
            out = out.replace(t, " ")
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def bot_send_message_sync(chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


async def bot_send_message(chat_id: int, text: str):
    await asyncio.to_thread(bot_send_message_sync, chat_id, text)


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")
    if not TG_API_ID or not TG_API_HASH:
        raise RuntimeError("TG_API_ID / TG_API_HASH is not set")

    client = TelegramClient(TG_SESSION, TG_API_ID, TG_API_HASH)

    last_seen: Dict[Tuple[int, str], float] = {}
    dedup_ttl = 30.0

    @client.on(events.NewMessage(chats=[MONITOR_CHAT_ID]))
    async def handler(event):
        text = event.raw_text or ""
        if not text:
            return

        masked_matches = list(dict.fromkeys(MASKED_RE.findall(text)))
        raw_candidates = extract_raw_candidates(text)

        if not masked_matches and not raw_candidates:
            return

        # 1) masked match
        for masked in masked_matches:
            found = find_user_by_masked(masked)
            if not found:
                continue

            user_id, raw_number = found

            key = (user_id, masked)
            now = time.time()
            if now - last_seen.get(key, 0.0) < dedup_ttl:
                continue
            last_seen[key] = now

            prefix = get_prefix_enabled(user_id)
            display_num = fmt_user_number(raw_number, prefix)

            content = remove_tokens(text, [masked])
            msg = (
                f"✅ Message received for <code>{escape_html(display_num)}</code>\n\n"
                f"💬 Content:\n<code>{escape_html(content or '(no text)')}</code>"
            )

            await bot_send_message(user_id, msg)

        # 2) raw number match
        for raw, matched_text in raw_candidates:
            found = find_user_by_raw(raw)
            if not found:
                continue

            user_id, raw_number = found

            key = (user_id, raw)
            now = time.time()
            if now - last_seen.get(key, 0.0) < dedup_ttl:
                continue
            last_seen[key] = now

            prefix = get_prefix_enabled(user_id)
            display_num = fmt_user_number(raw_number, prefix)

            content = remove_tokens(text, [matched_text])
            msg = (
                f"✅ Message received for <code>{escape_html(display_num)}</code>\n\n"
                f"💬 Content:\n<code>{escape_html(content or '(no text)')}</code>"
            )

            await bot_send_message(user_id, msg)

    logger.info("Userbot bridge running. monitor_chat_id=%s", MONITOR_CHAT_ID)
    await client.start()
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
