# -*- coding: utf-8 -*-
"""Admin-only member management: list members, edit weight/card, toggle admin, remove."""
import re

import jalali_utils as ju
import keyboards as kb
from handlers.common import get_admin_or_warn, get_db, cancel_conversation
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler

MEM_MENU, MEM_AWAIT_WEIGHT, MEM_AWAIT_CARD = range(3)


def _member_detail_text(user: dict) -> str:
    role = "👑 مدیر گروه" if user["is_admin"] else "🙋 عضو عادی"
    card = ju.format_card_number_html(user["card_number"]) if user["card_number"] else "ثبت نشده"
    return (
        f"<b>{user['first_name']}</b>"
        + (f" (@{user['username']})" if user["username"] else "")
        + f"\n{role}\nتعداد نفرات: {user['weight']}\nشماره کارت: {card}"
    )


async def members_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin = await get_admin_or_warn(update, context)
    if admin is None:
        return ConversationHandler.END
    db = get_db(context)
    users = db.list_active_users()
    if not users:
        await update.effective_message.reply_text("هنوز عضو فعالی ثبت نشده.")
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "👥 روی یکی از اعضا بزن تا جزئیاتش رو ببینی و ویرایش کنی:",
        reply_markup=kb.members_list_keyboard(users),
    )
    return MEM_MENU


async def show_member_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    db = get_db(context)
    user = db.get_user_by_id(user_id)
    await update.callback_query.edit_message_text(
        _member_detail_text(user), parse_mode="HTML", reply_markup=kb.member_detail_keyboard(user)
    )


async def members_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    db = get_db(context)
    data = query.data

    if data == "memedit_back":
        users = db.list_active_users()
        await query.edit_message_text(
            "👥 روی یکی از اعضا بزن تا جزئیاتش رو ببینی و ویرایش کنی:",
            reply_markup=kb.members_list_keyboard(users),
        )
        return MEM_MENU

    if data.startswith("member_"):
        user_id = int(data.split("_", 1)[1])
        await show_member_detail(update, context, user_id)
        context.user_data["mem_target_id"] = user_id
        return MEM_MENU

    if data.startswith("memedit_weight_"):
        user_id = int(data.rsplit("_", 1)[1])
        context.user_data["mem_target_id"] = user_id
        await query.edit_message_text("تعداد نفرات جدید رو بفرست (فقط عدد):")
        return MEM_AWAIT_WEIGHT

    if data.startswith("memedit_card_"):
        user_id = int(data.rsplit("_", 1)[1])
        context.user_data["mem_target_id"] = user_id
        await query.edit_message_text("شماره کارت جدید رو بفرست (۱۶ رقم):")
        return MEM_AWAIT_CARD

    if data.startswith("memedit_makeadmin_"):
        user_id = int(data.rsplit("_", 1)[1])
        db.set_admin(user_id, True)
        await show_member_detail(update, context, user_id)
        return MEM_MENU

    if data.startswith("memedit_unadmin_"):
        user_id = int(data.rsplit("_", 1)[1])
        if len(db.list_admins()) <= 1:
            await query.answer("این تنها مدیر گروهه؛ اول یه نفر دیگه رو مدیر کن.", show_alert=True)
            return MEM_MENU
        db.set_admin(user_id, False)
        await show_member_detail(update, context, user_id)
        return MEM_MENU

    if data.startswith("memedit_remove_"):
        user_id = int(data.rsplit("_", 1)[1])
        target = db.get_user_by_id(user_id)
        if target["is_admin"] and len(db.list_admins()) <= 1:
            await query.answer("این تنها مدیر گروهه؛ اول یه نفر دیگه رو مدیر کن.", show_alert=True)
            return MEM_MENU
        db.remove_user(user_id)
        await query.edit_message_text(f"🗑 {target['first_name']} از جمع حذف شد (سابقه هزینه‌هاش محفوظ می‌مونه).")
        try:
            await context.bot.send_message(target["telegram_id"], "از این جمع حساب‌کتاب حذف شدی.")
        except Exception:
            pass
        return ConversationHandler.END

    return MEM_MENU


async def weight_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    weight = ju.parse_positive_int(update.message.text)
    if weight is None or weight > 20:
        await update.message.reply_text("عدد معتبر نیست. دوباره بفرست:")
        return MEM_AWAIT_WEIGHT
    db = get_db(context)
    user_id = context.user_data["mem_target_id"]
    db.set_weight(user_id, weight)
    user = db.get_user_by_id(user_id)
    await update.message.reply_text(
        f"✅ تعداد نفرات {user['first_name']} به {weight} تغییر کرد.",
        reply_markup=kb.member_detail_keyboard(user),
    )
    await update.message.reply_html(_member_detail_text(user))
    return MEM_MENU


async def card_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    card = ju.clean_card_number(update.message.text)
    if card is None:
        await update.message.reply_text("شماره کارت باید ۱۶ رقم باشه. دوباره بفرست:")
        return MEM_AWAIT_CARD
    db = get_db(context)
    user_id = context.user_data["mem_target_id"]
    db.set_card_number(user_id, card)
    user = db.get_user_by_id(user_id)
    await update.message.reply_html(
        f"✅ شماره کارت {user['first_name']} به‌روز شد.\n\n" + _member_detail_text(user),
        reply_markup=kb.member_detail_keyboard(user),
    )
    return MEM_MENU


async def pending_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = await get_admin_or_warn(update, context)
    if admin is None:
        return
    db = get_db(context)
    pending = db.list_pending_users()
    if not pending:
        await update.effective_message.reply_text("درخواست در انتظاری وجود نداره ✅")
        return
    for user in pending:
        text = (
            f"👤 <b>{user['first_name']}</b>"
            + (f" (@{user['username']})" if user["username"] else "")
            + f"\nتعداد نفرات: {user['weight']}\n"
            f"شماره کارت: {ju.format_card_number_html(user['card_number']) if user['card_number'] else 'ثبت نشده'}\n"
            f"تاریخ درخواست: {user['created_at']}"
        )
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb.approval_keyboard(user["id"]))


members_conv = ConversationHandler(
    entry_points=[
        CommandHandler("members", members_entry),
        MessageHandler(filters.Regex(f"^{re.escape(kb.BTN_ADMIN_MEMBERS)}$"), members_entry),
    ],
    states={
        MEM_MENU: [CallbackQueryHandler(members_menu_callback, pattern=r"^mem")],
        MEM_AWAIT_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, weight_received)],
        MEM_AWAIT_CARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, card_received)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_conversation),
        CommandHandler("members", members_entry),
        MessageHandler(filters.COMMAND, cancel_conversation),
    ],
    name="members_conv",
    persistent=False,
)

pending_command_handler = CommandHandler("pending", pending_entry)
pending_button_handler = MessageHandler(filters.Regex(f"^{re.escape(kb.BTN_ADMIN_PENDING)}$"), pending_entry)
