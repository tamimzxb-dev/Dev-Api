## StoreBot + Group OTP Forward (Group-only)

Telegram Bot API cannot read messages sent by **other bots** inside a group.
So to detect numbers from other bots in a group, you must run a **Userbot bridge**.

This repo provides:

- `storebot.py` (your main bot): manages numbers & DMs users
- `userbot_bridge.py` (user account listener): reads the group and forwards matched messages to users via the bot

### Setup

1) Install deps

```bash
pip install -r requirements.txt
```

2) Export env vars

```bash
export BOT_TOKEN="<YOUR_BOT_TOKEN>"
export TG_API_ID="<YOUR_API_ID>"
export TG_API_HASH="<YOUR_API_HASH>"
export MONITOR_CHAT_ID="-1003528209997"
```

3) Run both processes

Terminal 1:
```bash
python storebot.py
```

Terminal 2:
```bash
python userbot_bridge.py
```

On first run, Telethon will ask for login code in terminal and create a `.session` file.

### Behavior

- Works in the specified **group**.
- Detects **masked numbers** like `2278SHU1478` and also raw numbers like `+84901957336`.
- Forwards the message content to the assigned user's inbox.
- Does not try to extract OTP (only number-based routing).
