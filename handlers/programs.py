# -*- coding: utf-8 -*-
"""
Long-term "program" management (e.g. a multi-day trip with its own mother
card): create a program, view its live status, request charges from
participants, and close it when done.

Also hosts two standalone (non-conversation) callback handlers that get
triggered from OTHER flows:
  - expense_approval_query_handler: the group admin approving/rejecting a
    pending program-linked expense (the "approve" button is sent from
    handlers/expenses.py when a program expense is submitted).
  - charge_confirmation_query_handler: the group admin confirming/rejecting a
    program charge (the button is sent from handlers/payments.py when
    someone charges a program's mother card).
"""
import re

import jalali_utils as ju
import keyboards as kb
import balances
from handlers.common import get_active_user_or_warn, get_db, cancel_conversation, send_main_menu
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler

PROG_MENU, PROG_NAME, PROG_PARTICIPANTS, PROG_WEIGHTS, PROG_CARD, PROG_CONFIRM, PROG_CHARGE_AMOUNT = range(7)


def _format_program_report(program: dict, report: dict) -> str:
    status_label = "فعال 🟢" if program["status"] == "active" else "بسته‌شده 🔒"
    lines = [
        f"📁 <b>{program['name']}</b> ({status_label})",
        f"💳 کارت مادر: {ju.format_card_number_html(program['mother_card'])}",
        "",
        f"💰 جمع شارژهای تاییدشده: {ju.format_money(report['total_charges'])}",
        f"🧾 جمع هزینه‌های تاییدشده: {ju.format_money(report['total_expenses'])}",
        f"💵 مانده‌ی کارت مادر: {ju.format_money(report['remaining'])}",
        "",
        "وضعیت هرکس نسبت به این برنامه:",
    ]
    for p in report["per_participant"]:
        if p["net"] > 0:
            lines.append(f"  • {p['name']}: طلبکار {ju.format_money(p['net'])}")
        elif p["net"] < 0:
            lines.append(f"  • {p['name']}: بدهکار {ju.format_money(-p['net'])}")
        else:
            lines.append(f"  • {p['name']}: تسویه")
    return "\n".join(lines)


async def _show_program_list(update: Update, context: ContextTypes.DEFAULT_TYPE, user: dict, edit: bool) -> int:
    db = get_db(context)
    programs = db.list_programs_for_user(user["id"], active_only=False)
    text = "📁 <b>برنامه‌های بلندمدت</b>" if programs else "هنوز عضو هیچ برنامه‌ای نیستی."
    if user["is_admin"] and not programs:
        text += "\n\nمی‌تونی یکی بسازی 👇"
    markup = kb.program_list_keyboard(programs, show_create_button=bool(user["is_admin"]))
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    return PROG_MENU


async def _show_program_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, program_id: int, requester: dict) -> int:
    db = get_db(context)
    program = db.get_program(program_id)
    if program is None:
        await update.callback_query.edit_message_text("این برنامه دیگه پیدا نشد.")
        return PROG_MENU
    report = balances.program_report(db, program_id)
    text = _format_program_report(program, report)
    pending_expenses = db.list_pending_program_expenses(program_id)
    pending_charges = db.list_pending_program_charges(program_id)
    if pending_expenses or pending_charges:
        text += f"\n\n⏳ در انتظار تایید: {len(pending_expenses)} هزینه، {len(pending_charges)} شارژ"
    await update.callback_query.edit_message_text(
        text, parse_mode="HTML",
        reply_markup=kb.program_detail_keyboard(program, bool(requester["is_admin"])),
    )
    return PROG_MENU


async def programs_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = await get_active_user_or_warn(update, context)
    if user is None:
        return ConversationHandler.END
    context.user_data.clear()
    return await _show_program_list(update, context, user, edit=False)


async def program_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    db = get_db(context)
    data = query.data
    requester = db.get_user_by_telegram_id(update.effective_user.id)

    if data == "prog_new":
        if not requester["is_admin"]:
            await query.answer("فقط مدیر گروه می‌تونه برنامه بسازه.", show_alert=True)
            return PROG_MENU
        context.user_data["prog_creator_id"] = requester["id"]
        await query.edit_message_text("اسم این برنامه چیه؟ 📝 (مثلاً: سفر شمال)")
        return PROG_NAME

    if data == "progview_back":
        return await _show_program_list(update, context, requester, edit=True)

    if data.startswith("progcharge_req_"):
        program_id = int(data.rsplit("_", 1)[1])
        if not requester["is_admin"]:
            await query.answer("فقط مدیر گروه می‌تونه درخواست شارژ بده.", show_alert=True)
            return PROG_MENU
        program = db.get_program(program_id)
        context.user_data["prog_charge_program_id"] = program_id
        await query.edit_message_text(
            f"می‌خوای بابت «{program['name']}» از هرنفر چقدر درخواست کنی؟ 💰 (فقط عدد، مثلاً 1000000)"
        )
        return PROG_CHARGE_AMOUNT

    if data.startswith("progclose_confirm_"):
        program_id = int(data.rsplit("_", 1)[1])
        if not requester["is_admin"]:
            await query.answer("فقط مدیر گروه می‌تونه برنامه رو ببنده.", show_alert=True)
            return PROG_MENU
        db.close_program(program_id)
        program = db.get_program(program_id)
        report = balances.program_report(db, program_id)
        summary = _format_program_report(program, report)
        await query.edit_message_text(f"🔒 برنامه بسته شد.\n\n{summary}", parse_mode="HTML")
        for pp in program["participants"]:
            if pp["user_id"] == requester["id"]:
                continue
            u = db.get_user_by_id(pp["user_id"])
            try:
                await context.bot.send_message(
                    u["telegram_id"], f"🔒 برنامه‌ی «{program['name']}» بسته شد.\n\n{summary}", parse_mode="HTML"
                )
            except Exception:
                pass
        return PROG_MENU

    if data.startswith("progclose_"):
        program_id = int(data.split("_", 1)[1])
        if not requester["is_admin"]:
            await query.answer("فقط مدیر گروه می‌تونه برنامه رو ببنده.", show_alert=True)
            return PROG_MENU
        await query.edit_message_text(
            "مطمئنی می‌خوای این برنامه رو ببندی؟ (بدهی‌های حل‌نشده همچنان توی حساب‌کتاب کلی می‌مونن)",
            reply_markup=kb.program_close_confirm_keyboard(program_id),
        )
        return PROG_MENU

    if data.startswith("progview_"):
        program_id = int(data.split("_", 1)[1])
        return await _show_program_detail(update, context, program_id, requester)

    return PROG_MENU


# ------------------------------------------------------------ create program
async def prog_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("یه اسم بفرست:")
        return PROG_NAME
    context.user_data["prog_name"] = name[:200]
    db = get_db(context)
    active_users = db.list_active_users()
    context.user_data["prog_selected"] = set()
    await update.message.reply_text(
        "چه کسایی توی این برنامه هستن؟ 👥", reply_markup=kb.program_participants_keyboard(active_users, set())
    )
    return PROG_PARTICIPANTS


async def prog_participants_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    db = get_db(context)
    active_users = db.list_active_users()
    selected: set = context.user_data.setdefault("prog_selected", set())

    if query.data == "progtoggle_all":
        selected.clear()
        selected.update(u["id"] for u in active_users)
    elif query.data == "progtoggle_none":
        selected.clear()
    elif query.data.startswith("progtoggle_"):
        uid = int(query.data.split("_", 1)[1])
        if uid in selected:
            selected.discard(uid)
        else:
            selected.add(uid)
    elif query.data == "progparticipants_done":
        if not selected:
            await query.answer("حداقل یه نفر باید انتخاب بشه!", show_alert=True)
            return PROG_PARTICIPANTS
        weights = {uid: db.get_user_by_id(uid)["weight"] for uid in selected}
        context.user_data["prog_weights"] = weights
        users = [db.get_user_by_id(uid) for uid in selected]
        await query.edit_message_text(
            "تعداد زیرمجموعه‌ی هرکس رو برای این برنامه مشخص کن 👨‍👩‍👧\n(پیش‌فرض از پروفایل خودشونه)",
            reply_markup=kb.program_weights_keyboard(users, weights),
        )
        return PROG_WEIGHTS

    await query.edit_message_reply_markup(reply_markup=kb.program_participants_keyboard(active_users, selected))
    return PROG_PARTICIPANTS


async def prog_weights_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    db = get_db(context)
    weights: dict = context.user_data["prog_weights"]
    selected = context.user_data["prog_selected"]
    users = [db.get_user_by_id(uid) for uid in selected]

    if query.data == "pweights_done":
        await query.answer()
        await query.edit_message_text("شماره‌ی کارت مادر این برنامه چیه؟ 💳 (۱۶ رقم)")
        return PROG_CARD

    if query.data.startswith("pwnoop_"):
        await query.answer()
        return PROG_WEIGHTS

    action, uid_str = query.data.split("_", 1)
    uid = int(uid_str)
    current = weights.get(uid, 1)
    if action == "pwinc":
        weights[uid] = min(20, current + 1)
    else:
        weights[uid] = max(1, current - 1)
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=kb.program_weights_keyboard(users, weights))
    return PROG_WEIGHTS


def _build_program_preview(db, context) -> str:
    name = context.user_data["prog_name"]
    card = context.user_data["prog_card"]
    weights = context.user_data["prog_weights"]
    lines = [
        "📁 <b>پیش‌نمایش برنامه</b>",
        f"اسم: {name}",
        f"کارت مادر: {ju.format_card_number_html(card)}",
        "",
        "افراد:",
    ]
    for uid, w in weights.items():
        u = db.get_user_by_id(uid)
        lines.append(f"  • {u['first_name']}: {w} نفر")
    return "\n".join(lines)


async def prog_card_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    card = ju.clean_card_number(update.message.text)
    if card is None:
        await update.message.reply_text("شماره کارت باید ۱۶ رقم باشه. دوباره بفرست:")
        return PROG_CARD
    context.user_data["prog_card"] = card
    db = get_db(context)
    await update.message.reply_text(
        _build_program_preview(db, context), parse_mode="HTML", reply_markup=kb.program_create_confirm_keyboard()
    )
    return PROG_CONFIRM


async def prog_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    db = get_db(context)

    if query.data == "progcreate_cancel":
        await query.edit_message_text("لغو شد. ❌")
        context.user_data.clear()
        return ConversationHandler.END

    name = context.user_data["prog_name"]
    program_id = db.create_program(
        name=name,
        mother_card=context.user_data["prog_card"],
        creator_id=context.user_data["prog_creator_id"],
        weights=context.user_data["prog_weights"],
    )
    await query.edit_message_text(f"✅ برنامه‌ی «{name}» ساخته شد!")

    requester = db.get_user_by_telegram_id(update.effective_user.id)
    for uid in context.user_data["prog_weights"]:
        if uid == requester["id"]:
            continue
        u = db.get_user_by_id(uid)
        try:
            await context.bot.send_message(
                u["telegram_id"],
                f"📁 به برنامه‌ی «{name}» اضافه شدی! برای دیدن جزئیات، از منوی «{kb.BTN_PROGRAMS}» استفاده کن.",
            )
        except Exception:
            pass

    context.user_data.clear()
    await send_main_menu(update, requester)
    return ConversationHandler.END


# --------------------------------------------------------------- charge req
async def prog_charge_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount = ju.parse_amount(update.message.text)
    if amount is None:
        await update.message.reply_text("فقط یه عدد مثبت بفرست:")
        return PROG_CHARGE_AMOUNT

    db = get_db(context)
    program_id = context.user_data["prog_charge_program_id"]
    program = db.get_program(program_id)
    text = (
        f"💰 <b>درخواست شارژ برای «{program['name']}»</b>\n"
        f"مدیر گروه درخواست کرده هرکس {ju.format_money(amount)} به کارت مادر واریز کنه:\n"
        f"{ju.format_card_number_html(program['mother_card'])}\n\n"
        f"وقتی واریز کردی، از «{kb.BTN_NEW_PAYMENT}» این برنامه رو به‌عنوان مقصد انتخاب کن."
    )
    sent = 0
    for pp in program["participants"]:
        u = db.get_user_by_id(pp["user_id"])
        try:
            await context.bot.send_message(u["telegram_id"], text, parse_mode="HTML")
            sent += 1
        except Exception:
            pass

    await update.message.reply_text(f"✅ درخواست شارژ برای {sent} نفر فرستاده شد.")
    context.user_data.clear()
    requester = db.get_user_by_telegram_id(update.effective_user.id)
    await send_main_menu(update, requester)
    return ConversationHandler.END


# ------------------------------------------------ standalone approval callbacks
async def expense_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = get_db(context)
    action, expense_id_str = query.data.rsplit("_", 1)
    expense_id = int(expense_id_str)
    expense = db.get_expense(expense_id)
    if expense is None or expense["approval_status"] != "pending":
        await query.edit_message_text("این هزینه قبلاً بررسی شده یا پیدا نشد.")
        return

    approver = db.get_user_by_telegram_id(update.effective_user.id)
    if not approver or not approver["is_admin"]:
        await query.answer("فقط مدیر گروه می‌تونه تایید کنه.", show_alert=True)
        return

    creator = db.get_user_by_id(expense["creator_id"])
    if action == "expapprove_confirm":
        db.set_expense_approval(expense_id, "approved")
        await query.edit_message_text(f"✅ هزینه‌ی «{expense['description']}» ({ju.format_money(expense['amount'])}) تایید شد.")
        try:
            await context.bot.send_message(
                creator["telegram_id"], f"✅ هزینه‌ی «{expense['description']}» که ثبت کرده بودی تایید شد."
            )
        except Exception:
            pass
    else:
        db.set_expense_approval(expense_id, "rejected")
        await query.edit_message_text(f"❌ هزینه‌ی «{expense['description']}» رد شد.")
        try:
            await context.bot.send_message(
                creator["telegram_id"], f"❌ هزینه‌ی «{expense['description']}» که ثبت کرده بودی رد شد."
            )
        except Exception:
            pass


async def charge_confirmation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = get_db(context)
    action, charge_id_str = query.data.rsplit("_", 1)
    charge_id = int(charge_id_str)
    charge = db.get_program_charge(charge_id)
    if charge is None or charge["status"] != "pending":
        await query.edit_message_text("این شارژ قبلاً بررسی شده یا پیدا نشد.")
        return

    approver = db.get_user_by_telegram_id(update.effective_user.id)
    if not approver or not approver["is_admin"]:
        await query.answer("فقط مدیر گروه می‌تونه تایید کنه.", show_alert=True)
        return

    charger = db.get_user_by_id(charge["user_id"])
    program = db.get_program(charge["program_id"])
    if action == "chargeconf_confirm":
        db.set_program_charge_status(charge_id, "confirmed")
        await query.edit_message_text(
            f"✅ شارژ {ju.format_money(charge['amount'])} از {charger['first_name']} برای «{program['name']}» تایید شد."
        )
        try:
            await context.bot.send_message(
                charger["telegram_id"], f"✅ شارژ {ju.format_money(charge['amount'])} تو برای «{program['name']}» تایید شد."
            )
        except Exception:
            pass
    else:
        db.set_program_charge_status(charge_id, "rejected")
        await query.edit_message_text(f"❌ شارژ {charger['first_name']} برای «{program['name']}» رد شد.")
        try:
            await context.bot.send_message(
                charger["telegram_id"], f"❌ شارژ {ju.format_money(charge['amount'])} تو برای «{program['name']}» رد شد."
            )
        except Exception:
            pass


program_conv = ConversationHandler(
    entry_points=[
        CommandHandler("programs", programs_entry),
        MessageHandler(filters.Regex(f"^{re.escape(kb.BTN_PROGRAMS)}$"), programs_entry),
    ],
    states={
        PROG_MENU: [CallbackQueryHandler(program_menu_callback, pattern=r"^prog")],
        PROG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, prog_name_received)],
        PROG_PARTICIPANTS: [CallbackQueryHandler(prog_participants_callback, pattern=r"^(progtoggle_|progparticipants_done$)")],
        PROG_WEIGHTS: [CallbackQueryHandler(prog_weights_callback, pattern=r"^(pwinc_|pwdec_|pwnoop_|pweights_done$)")],
        PROG_CARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, prog_card_received)],
        PROG_CONFIRM: [CallbackQueryHandler(prog_confirm_callback, pattern=r"^progcreate_(confirm|cancel)$")],
        PROG_CHARGE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, prog_charge_amount_received)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_conversation),
        MessageHandler(filters.COMMAND, cancel_conversation),
    ],
    name="program_conv",
    persistent=False,
)

expense_approval_query_handler = CallbackQueryHandler(expense_approval_callback, pattern=r"^expapprove_(confirm|reject)_\d+$")
charge_confirmation_query_handler = CallbackQueryHandler(charge_confirmation_callback, pattern=r"^chargeconf_(confirm|reject)_\d+$")
