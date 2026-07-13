# -*- coding: utf-8 -*-
"""Admin conversation: manage long-term trip programs, request card top-ups, view reports."""

import re

from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Update
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
)

import jalali_utils as ju
import keyboards as kb
from handlers.common import get_admin_or_warn, get_db, cancel_conversation

(
    PROG_MENU,
    PROG_AWAIT_NAME,
    PROG_AWAIT_DESC,
    PROG_SELECT_MOTHER_CARD,
    PROG_SELECT_MEMBERS,
    PROG_CHARGE_AMOUNT,
    PROG_CHARGE_DESC,
) = range(7)


async def programs_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    admin = await get_admin_or_warn(update, context)
    if admin is None:
        return ConversationHandler.END
    return await _show_program_list(update, context)


async def _show_program_list(update, context):
    db = get_db(context)
    programs = db.list_programs()
    if not programs:
        text = "هنوز هیچ برنامه سفری تعریف نشده. می‌تونی اولین رو بسازی:"
    else:
        text = "📋 لیست برنامه‌های سفر:"
    await update.effective_message.reply_text(
        text,
        reply_markup=kb.programs_list_keyboard(programs),
    )
    return PROG_MENU


async def program_menu_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    db = get_db(context)

    if data == "prognew":
        await query.edit_message_text("اسم برنامه سفر رو بفرست (مثلاً سفر شمال مرداد):")
        return PROG_AWAIT_NAME

    if data == "progback":
        return await _show_program_list(update, context)

    if data.startswith("progview_"):
        prog_id = int(data.split("_", 1)[1])
        prog = db.get_program(prog_id)
        if not prog:
            await query.edit_message_text("برنامه پیدا نشد.")
            return ConversationHandler.END
        user = (
            db.get_user_by_id(prog["mother_card_user_id"])
            if prog["mother_card_user_id"]
            else None
        )
        card_str = (
            ju.format_card_number_html(prog["mother_card"])
            if prog["mother_card"]
            else "تعریف نشده"
        )
        members = [db.get_user_by_id(uid) for uid in prog["member_ids"]]
        member_names = ", ".join(m["first_name"] for m in members if m)
        text = (
            f"📌 <b>{prog['name']}</b>\n"
            + (f"📝 توضیح: {prog['description']}\n" if prog["description"] else "")
            + f"💳 کارت مادر: {card_str}"
            + (f" (از {user['first_name']})" if user else "")
            + f"\n👥 اعضا: {member_names if member_names else 'هیچکس'}"
        )
        await query.edit_message_text(
            text, parse_mode="HTML", reply_markup=kb.program_detail_keyboard(prog)
        )
        return PROG_MENU

    if data.startswith("progdel_"):
        prog_id = int(data.split("_", 1)[1])
        db.delete_program(prog_id)
        await query.edit_message_text("برنامه حذف شد ✅")
        return await _show_program_list(update, context)

    if data.startswith("progcharge_"):
        prog_id = int(data.split("_", 1)[1])
        prog = db.get_program(prog_id)
        if not prog or not prog["member_ids"]:
            await query.answer("این برنامه عضو نداره.", show_alert=True)
            return PROG_MENU
        context.user_data["charge_prog_id"] = prog_id
        await query.edit_message_text(
            "مبلغ شارژ به ازای هر واحد (نفر) رو بفرست (تومان):"
        )
        return PROG_CHARGE_AMOUNT

    if data.startswith("progreport_"):
        prog_id = int(data.split("_", 1)[1])
        return await _send_program_report(update, context, prog_id)

    return PROG_MENU


async def program_name_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("اسم نمی‌تونه خالی باشه. دوباره بفرست:")
        return PROG_AWAIT_NAME
    context.user_data["prog_name"] = name[:200]
    await update.message.reply_text("توضیح کوتاه (اختیاری - بفرست یا /skip بزن):")
    return PROG_AWAIT_DESC


async def program_desc_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    text = update.message.text.strip()
    if text and text != "/skip":
        context.user_data["prog_desc"] = text[:500]
    else:
        context.user_data["prog_desc"] = None

    db = get_db(context)
    users = db.list_active_users()
    rows = [
        [
            InlineKeyboardButton(
                f"{u['first_name']} - {ju.format_card_number_html(u.get('card_number') or 'نامشخص')}",
                callback_data=f"progcard_{u['id']}",
            )
        ]
        for u in users
        if u.get("card_number")
    ]
    await update.message.reply_text(
        "حالا کارت مادر این برنامه رو انتخاب کن (کسی که معمولاً با کارتش خرج می‌کنید):",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
    )
    return PROG_SELECT_MOTHER_CARD


async def mother_card_selected(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    user_id = int(query.data.split("_", 1)[1])
    context.user_data["prog_mother_user_id"] = user_id

    db = get_db(context)
    active_users = db.list_active_users()
    selected = {u["id"] for u in active_users}
    context.user_data["prog_selected_members"] = selected
    await query.edit_message_text(
        "اعضای شرکت‌کننده در این برنامه رو انتخاب کن (پیش‌فرض همه):",
        reply_markup=kb.participants_keyboard(active_users, selected),
    )
    return PROG_SELECT_MEMBERS


async def members_selection_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    await query.answer()
    db = get_db(context)
    active_users = db.list_active_users()
    selected: set = context.user_data.setdefault(
        "prog_selected_members", {u["id"] for u in active_users}
    )

    if query.data == "toggle_all":
        selected.clear()
        selected.update(u["id"] for u in active_users)
    elif query.data == "toggle_none":
        selected.clear()
    elif query.data.startswith("toggle_"):
        uid = int(query.data.split("_", 1)[1])
        if uid in selected:
            selected.discard(uid)
        else:
            selected.add(uid)
    elif query.data == "participants_done":
        if not selected:
            await query.answer("حداقل یه نفر باید باشه!", show_alert=True)
            return PROG_SELECT_MEMBERS
        name = context.user_data["prog_name"]
        desc = context.user_data.get("prog_desc")
        mother_id = context.user_data["prog_mother_user_id"]
        member_ids = list(selected)
        prog_id = db.create_program(name, desc, mother_id, member_ids)
        await query.edit_message_text(
            f"✅ برنامه «{name}» با {len(member_ids)} عضو ساخته شد."
        )
        context.user_data.clear()
        return ConversationHandler.END

    await query.edit_message_reply_markup(
        reply_markup=kb.participants_keyboard(active_users, selected)
    )
    return PROG_SELECT_MEMBERS


# ----- charge flow -----
async def charge_amount_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    amount = ju.parse_amount(update.message.text)
    if amount is None:
        await update.message.reply_text("فقط یه عدد مثبت بفرست:")
        return PROG_CHARGE_AMOUNT
    context.user_data["charge_amount_per_unit"] = amount
    await update.message.reply_text("توضیح این شارژ رو بفرست (مثلاً شارژ اولیه):")
    return PROG_CHARGE_DESC


async def charge_desc_received(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    desc = update.message.text.strip() or "شارژ کارت مادر"
    context.user_data["charge_desc"] = desc[:300]
    db = get_db(context)
    prog_id = context.user_data["charge_prog_id"]
    prog = db.get_program(prog_id)
    amount_per_unit = context.user_data["charge_amount_per_unit"]
    mother_user_id = prog["mother_card_user_id"]
    mother_user = db.get_user_by_id(mother_user_id)

    shares = {}
    weights_used = {}
    for uid in prog["member_ids"]:
        user = db.get_user_by_id(uid)
        w = user["weight"] if user else 1
        share = amount_per_unit * w
        shares[uid] = share
        weights_used[uid] = w

    total_amount = sum(shares.values())
    expense_id = db.create_expense(
        payer_id=mother_user_id,
        creator_id=db.get_user_by_telegram_id(update.effective_user.id)["id"],
        amount=total_amount,
        description=f"شارژ: {desc} ({prog['name']})",
        split_mode="custom",
        shares=shares,
        weights_used=weights_used,
        expense_type="charge",
        program_id=prog_id,
    )

    # Notify members
    for uid, amt in shares.items():
        member = db.get_user_by_id(uid)
        if not member:
            continue
        try:
            text = (
                f"💰 <b>درخواست شارژ کارت مادر</b>\n"
                f"برنامه: {prog['name']}\n"
                f"مبلغ سهم شما: {ju.format_money(amt)}\n"
                f"به کارت {mother_user['first_name']}: {ju.format_card_number_html(mother_user['card_number'])}\n"
                f"لطفاً واریز کنید و سپس در ربات «ثبت پرداخت» رو بزنید."
            )
            await context.bot.send_message(
                member["telegram_id"], text, parse_mode="HTML"
            )
        except Exception:
            pass

    await update.message.reply_text(
        f"✅ شارژ با موفقیت ثبت شد و به {len(shares)} عضو اطلاع داده شد.\n"
        f"مجموع: {ju.format_money(total_amount)}",
    )
    context.user_data.clear()
    return ConversationHandler.END


# ----- report -----
async def _send_program_report(update, context, prog_id):
    db = get_db(context)
    prog = db.get_program(prog_id)
    if not prog:
        await update.effective_message.reply_text("برنامه پیدا نشد.")
        return ConversationHandler.END

    expenses = db.list_expenses_for_program(prog_id)
    charges = [e for e in expenses if e["expense_type"] == "charge"]
    regulars = [e for e in expenses if e["expense_type"] != "charge"]
    total_charged = sum(e["amount"] for e in charges)
    total_spent = sum(e["amount"] for e in regulars)
    balance = total_charged - total_spent

    mother_user = db.get_user_by_id(prog["mother_card_user_id"])
    card_str = (
        ju.format_card_number_html(prog["mother_card"]) if prog["mother_card"] else "—"
    )

    lines = [
        f"📊 <b>گزارش برنامه: {prog['name']}</b>",
        f"💳 کارت مادر: {card_str} ({mother_user['first_name'] if mother_user else '?'})",
        f"👥 اعضا: {', '.join(db.get_user_by_id(uid)['first_name'] for uid in prog['member_ids'] if db.get_user_by_id(uid))}",
        "",
        f"💰 مجموع شارژها: {ju.format_money(total_charged)}",
        f"💸 مجموع هزینه‌ها: {ju.format_money(total_spent)}",
        f"📈 موجودی فعلی: {ju.format_money(balance)}",
        "",
        "📋 جزئیات تراکنش‌ها:",
    ]
    for e in expenses:
        type_emoji = "🔋" if e["expense_type"] == "charge" else "🧾"
        lines.append(
            f"{type_emoji} #{e['id']} {e['description']} — {ju.format_money(e['amount'])}"
        )

    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")
    return ConversationHandler.END


programs_conv = ConversationHandler(
    entry_points=[
        CommandHandler("programs", programs_entry),
        MessageHandler(
            filters.Regex(f"^{re.escape(kb.BTN_PROGRAMS)}$"), programs_entry
        ),
    ],
    states={
        PROG_MENU: [CallbackQueryHandler(program_menu_callback, pattern=r"^prog")],
        PROG_AWAIT_NAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, program_name_received)
        ],
        PROG_AWAIT_DESC: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, program_desc_received)
        ],
        PROG_SELECT_MOTHER_CARD: [
            CallbackQueryHandler(mother_card_selected, pattern=r"^progcard_")
        ],
        PROG_SELECT_MEMBERS: [
            CallbackQueryHandler(
                members_selection_callback, pattern=r"^(toggle_|participants_done$)"
            )
        ],
        PROG_CHARGE_AMOUNT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, charge_amount_received)
        ],
        PROG_CHARGE_DESC: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, charge_desc_received)
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_conversation),
        MessageHandler(filters.COMMAND, cancel_conversation),
    ],
    name="programs_conv",
    persistent=False,
)
