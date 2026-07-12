# -*- coding: utf-8 -*-
import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DB_PATH = os.environ.get("DB_PATH", "hesabkitab.db").strip()

# Optional: comma-separated Telegram numeric user IDs that should become admin
# instantly on their first /start, bypassing the pending-approval step.
# Example in .env:  INITIAL_ADMIN_IDS=123456789,987654321
_raw_admins = os.environ.get("INITIAL_ADMIN_IDS", "").strip()
INITIAL_ADMIN_IDS = [int(x) for x in _raw_admins.split(",") if x.strip().isdigit()]

GROUP_CHAT_ID_SETTING_KEY = "announcement_group_chat_id"

# Optional: if Telegram is filtered where the bot runs (e.g. from Iran without
# a server abroad), point this at a local proxy your circumvention tool exposes
# -- most tools (v2rayN, V2Box, Shadowrocket, ClashX, ...) expose a SOCKS5 proxy,
# commonly at something like socks5://127.0.0.1:1080. HTTP proxies also work,
# e.g. http://127.0.0.1:8080. Leave empty if you don't need this (e.g. running
# on a VPS outside Iran, which is the more reliable long-term setup anyway).
PROXY_URL = os.environ.get("PROXY_URL", "").strip()

# Optional: Veryfi OCR API credentials (https://hub.veryfi.com/) -- if all three
# are set, the bot offers "extract items from a receipt photo automatically" as
# a split mode. If left empty, that option is simply hidden; everything else
# works normally without it.
VERYFI_CLIENT_ID = os.environ.get("VERYFI_CLIENT_ID", "").strip()
VERYFI_USERNAME = os.environ.get("VERYFI_USERNAME", "").strip()
VERYFI_API_KEY = os.environ.get("VERYFI_API_KEY", "").strip()
VERYFI_ENABLED = bool(VERYFI_CLIENT_ID and VERYFI_USERNAME and VERYFI_API_KEY)
