# -*- coding: utf-8 -*-
"""All Telegram keyboard builders live here, kept separate from handler logic."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton

import jalali_utils as ju

# ----------------------------------------------------------------- main menu
BTN_NEW_EXPENSE = "➕ ثبت هزینه جدید"
BTN_NEW_PAYMENT = "💸 ثبت پرداخت"
BTN_MY_BALANCE = "📊 وضعیت حساب من"
BTN_HISTORY = "🧾 تاریخچه هزینه‌ها"
BTN_HELP = "❓ راهنما"
BTN_ADMIN_MEMBERS = "👥 مدیریت اعضا"
BTN_ADMIN_PENDING = "⏳ درخواست‌های در انتظار"
BTN_ADMIN_REPORT = "📋 گزارش کامل گروه"


def main_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_NEW_EXPENSE), KeyboardButton(BTN_NEW_PAYMENT)],
        [KeyboardButton(BTN_MY_BALANCE), KeyboardButton(BTN_HISTORY)],
    ]
    if is_admin:
        rows.append([KeyboardButton(BTN_ADMIN_MEMBERS), KeyboardButton(BTN_ADMIN_PENDING)])
        rows.append([KeyboardButton(BTN_ADMIN_REPORT), KeyboardButton(BTN_HELP)])
    else:
        rows.append([KeyboardButton(BTN_HELP)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# ------------------------------------------------------------- registration
def approval_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تایید عضویت", callback_data=f"reg_approve_{user_id}"),
        InlineKeyboardButton("❌ رد کردن", callback_data=f"reg_reject_{user_id}"),
    ]])


# ------------------------------------------------------------------ expense
def split_mode_keyboard(show_ocr: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("👨‍👩‍👧 بر اساس تعداد نفرات هرکس (پیش‌فرض)", callback_data="split_weighted")],
        [InlineKeyboardButton("➗ تقسیم مساوی بین همه", callback_data="split_equal")],
        [InlineKeyboardButton("✍️ مبلغ دلخواه برای هرکس", callback_data="split_custom")],
    ]
    if show_ocr:
        rows.append([InlineKeyboardButton("🧾 استخراج خودکار از عکس رسید (AI)", callback_data="split_ocr")])
    return InlineKeyboardMarkup(rows)


def item_assign_keyboard(users: list[dict], selected_ids: set[int], progress_label: str) -> InlineKeyboardMarkup:
    """Multi-select which participant(s) an OCR-extracted line item belongs to."""
    rows = []
    for u in users:
        mark = "✅" if u["id"] in selected_ids else "⬜"
        rows.append([InlineKeyboardButton(f"{mark} {u['first_name']}", callback_data=f"itemtag_{u['id']}")])
    rows.append([InlineKeyboardButton(f"➡️ تایید ({progress_label})", callback_data="item_confirm")])
    rows.append([InlineKeyboardButton("🚫 مال هیچ‌کس نبود، ردش کن", callback_data="item_none")])
    return InlineKeyboardMarkup(rows)


def remainder_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ آره، مساوی بین همه تقسیم کن", callback_data="remainder_split")],
        [InlineKeyboardButton("❌ نه، نادیده بگیرش", callback_data="remainder_skip")],
    ])


def receipt_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("رد کردن، بدون رسید", callback_data="receipt_skip")]])


def participants_keyboard(all_active_users: list[dict], selected_ids: set[int]) -> InlineKeyboardMarkup:
    rows = []
    for u in all_active_users:
        mark = "✅" if u["id"] in selected_ids else "⬜"
        label = f"{mark} {u['first_name']} ({u['weight']} نفر)"
        rows.append([InlineKeyboardButton(label, callback_data=f"toggle_{u['id']}")])
    rows.append([
        InlineKeyboardButton("✅ انتخاب همه", callback_data="toggle_all"),
        InlineKeyboardButton("🗑 هیچ‌کدام", callback_data="toggle_none"),
    ])
    rows.append([InlineKeyboardButton("➡️ تایید و ادامه", callback_data="participants_done")])
    return InlineKeyboardMarkup(rows)


def weights_keyboard(users: list[dict], weights: dict[int, int]) -> InlineKeyboardMarkup:
    """One row per participant to bump their headcount up/down for THIS expense only."""
    rows = []
    for u in users:
        count = weights.get(u["id"], u["weight"])
        rows.append([
            InlineKeyboardButton("➖", callback_data=f"wdec_{u['id']}"),
            InlineKeyboardButton(f"{u['first_name']}: {count} نفر", callback_data=f"wnoop_{u['id']}"),
            InlineKeyboardButton("➕", callback_data=f"winc_{u['id']}"),
        ])
    rows.append([InlineKeyboardButton("✅ تایید و ادامه", callback_data="weights_done")])
    return InlineKeyboardMarkup(rows)


def expense_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ ثبت نهایی", callback_data="expense_confirm")],
        [InlineKeyboardButton("🔁 تغییر پرداخت‌کننده", callback_data="expense_change_payer")],
        [InlineKeyboardButton("❌ لغو", callback_data="expense_cancel")],
    ])


def payer_choice_keyboard(all_active_users: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(u["first_name"], callback_data=f"payer_{u['id']}")] for u in all_active_users]
    rows.append([InlineKeyboardButton("↩️ بازگشت", callback_data="payer_cancel_change")])
    return InlineKeyboardMarkup(rows)


# ------------------------------------------------------------------ payment
def payment_target_keyboard(other_active_users: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(u["first_name"], callback_data=f"paytarget_{u['id']}")] for u in other_active_users]
    return InlineKeyboardMarkup(rows)


def skip_note_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("رد کردن (بدون توضیح)", callback_data="note_skip")]])


def payment_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ ثبت پرداخت", callback_data="payment_confirm")],
        [InlineKeyboardButton("❌ لغو", callback_data="payment_cancel")],
    ])


def payment_receipt_keyboard(payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ بله، دریافت کردم", callback_data=f"payrecv_confirm_{payment_id}"),
        InlineKeyboardButton("❌ دریافت نکردم", callback_data=f"payrecv_reject_{payment_id}"),
    ]])


# -------------------------------------------------------------- member mgmt
def members_list_keyboard(users: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for u in users:
        tag = "👑" if u["is_admin"] else "🙋"
        rows.append([InlineKeyboardButton(f"{tag} {u['first_name']} ({u['weight']} نفر)", callback_data=f"member_{u['id']}")])
    return InlineKeyboardMarkup(rows)


def member_detail_keyboard(user: dict) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✏️ تغییر تعداد نفرات", callback_data=f"memedit_weight_{user['id']}")],
        [InlineKeyboardButton("💳 تغییر شماره کارت", callback_data=f"memedit_card_{user['id']}")],
    ]
    if user["is_admin"]:
        rows.append([InlineKeyboardButton("👑 حذف دسترسی مدیریت", callback_data=f"memedit_unadmin_{user['id']}")])
    else:
        rows.append([InlineKeyboardButton("👑 تبدیل به مدیر", callback_data=f"memedit_makeadmin_{user['id']}")])
    rows.append([InlineKeyboardButton("🗑 حذف از گروه", callback_data=f"memedit_remove_{user['id']}")])
    rows.append([InlineKeyboardButton("↩️ بازگشت به لیست", callback_data="memedit_back")])
    return InlineKeyboardMarkup(rows)


def pending_list_keyboard(users: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"👤 بررسی {u['first_name']}", callback_data=f"pendview_{u['id']}")] for u in users]
    return InlineKeyboardMarkup(rows)


# ----------------------------------------------------------------- history
def history_page_keyboard(offset: int, has_more: bool, expense_ids_for_delete: list[int], expense_ids_with_receipt: list[int] | None = None) -> InlineKeyboardMarkup:
    rows = []
    for eid in (expense_ids_with_receipt or []):
        rows.append([InlineKeyboardButton(f"🧾 دیدن رسید هزینه #{eid}", callback_data=f"viewreceipt_{eid}")])
    for eid in expense_ids_for_delete:
        rows.append([InlineKeyboardButton(f"🗑 حذف هزینه #{eid}", callback_data=f"delexp_{eid}")])
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"histpage_{max(0, offset - 5)}"))
    if has_more:
        nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"histpage_{offset + 5}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def cancel_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="generic_cancel")]])
