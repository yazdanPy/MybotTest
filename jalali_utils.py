# -*- coding: utf-8 -*-
"""
Utilities for Jalali (Persian/Shamsi) dates and Persian-friendly number formatting.

Important: the bot server might run in any timezone (most VPS boxes default to UTC),
but this bot is for Iranian users, so "today" must always mean "today in Iran".
Iran Standard Time is a fixed UTC+03:30 offset (no daylight saving since 2022),
so we compute it explicitly instead of trusting the server's local timezone.
"""
from datetime import datetime, timezone, timedelta

import jdatetime

# Make jdatetime render month/weekday names in Persian script (not Finglish).
jdatetime.set_locale("fa_IR")

IRAN_OFFSET = timedelta(hours=3, minutes=30)

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
ENGLISH_DIGITS = "0123456789"
_FA_TO_EN = str.maketrans(PERSIAN_DIGITS + "٠١٢٣٤٥٦٧٨٩", ENGLISH_DIGITS + ENGLISH_DIGITS)


def now_iran() -> datetime:
    """Current naive datetime in Iran local time, regardless of server timezone."""
    return datetime.now(timezone.utc).astimezone(timezone.utc).replace(tzinfo=None) + IRAN_OFFSET


def today_jalali() -> jdatetime.date:
    return jdatetime.date.fromgregorian(date=now_iran().date())


def jalali_date_str(dt: datetime | None = None) -> str:
    """Numeric Jalali date string, e.g. '1405/04/17'. Used for storage & sorting."""
    g = dt if dt is not None else now_iran()
    j = jdatetime.date.fromgregorian(date=g.date())
    return j.strftime("%Y/%m/%d")


def jalali_pretty(dt_or_str) -> str:
    """
    Pretty Jalali date with Persian month name, e.g. '17 تیر 1405'.
    Accepts either a datetime or a numeric jalali string 'YYYY/MM/DD'.
    """
    if isinstance(dt_or_str, str):
        y, m, d = (int(x) for x in dt_or_str.split("/"))
        j = jdatetime.date(y, m, d)
    else:
        j = jdatetime.date.fromgregorian(date=dt_or_str.date())
    return j.strftime("%d %B %Y")


def jalali_pretty_with_weekday(dt_or_str) -> str:
    if isinstance(dt_or_str, str):
        y, m, d = (int(x) for x in dt_or_str.split("/"))
        j = jdatetime.date(y, m, d)
    else:
        j = jdatetime.date.fromgregorian(date=dt_or_str.date())
    return j.strftime("%A %d %B %Y")


def normalize_digits(text: str) -> str:
    """Convert Persian/Arabic-Indic digits in a string to plain ASCII digits."""
    return text.translate(_FA_TO_EN)


def parse_amount(text: str) -> int | None:
    """
    Parse a Toman amount from free-form user input.
    Accepts Persian digits, thousands separators (, . space), and trims whitespace.
    Returns None if not a valid positive integer.
    """
    if not text:
        return None
    cleaned = normalize_digits(text.strip())
    cleaned = cleaned.replace(",", "").replace(" ", "").replace("٬", "")
    # allow trailing/inline "تومان" or "تومن" that people might type out of habit
    cleaned = cleaned.replace("تومان", "").replace("تومن", "").strip()
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    if value <= 0:
        return None
    return value


def parse_positive_int(text: str) -> int | None:
    """Parse a small positive integer (used for household weight)."""
    if not text:
        return None
    cleaned = normalize_digits(text.strip())
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    if value <= 0:
        return None
    return value


def format_money(amount: int) -> str:
    """Format a Toman amount with thousands separators, e.g. 1500000 -> '1,500,000 تومان'."""
    return f"{amount:,} تومان"


def format_card_number(raw: str) -> str:
    """
    Format a 16-digit card number with dashes: 6037xxxxxxxxxxxx -> 6037-xxxx-xxxx-xxxx.

    Wrapped in Unicode LRI/PDI isolate marks (U+2066 / U+2069). Without this, a
    dash-separated LTR number embedded in an RTL (Persian) paragraph can have its
    dash-separated groups visually REORDERED by the bidi algorithm -- e.g.
    displaying as 7890-3456-9912-6037 instead of 6037-9912-3456-7890, because the
    dashes are treated as neutral characters whose direction gets resolved from
    the surrounding RTL context. Isolating the whole number as one LTR unit fixes
    this regardless of what Persian text surrounds it.
    """
    digits = normalize_digits(raw).replace(" ", "").replace("-", "")
    if len(digits) != 16 or not digits.isdigit():
        return raw  # fall back to whatever was stored
    grouped = "-".join(digits[i:i + 4] for i in range(0, 16, 4))
    return f"\u2066{grouped}\u2069"


def format_card_number_html(raw: str) -> str:
    """
    Bidi-safe card number wrapped in Telegram HTML <code> tags, which renders as
    a monospace block the user can tap once to copy. Use this (not the plain
    format_card_number) in any message sent with parse_mode="HTML".
    """
    return f"<code>{format_card_number(raw)}</code>"


def clean_card_number(raw: str) -> str | None:
    """Validate & normalize a card number for storage. Returns None if invalid."""
    digits = normalize_digits(raw).replace(" ", "").replace("-", "")
    if len(digits) != 16 or not digits.isdigit():
        return None
    return digits
