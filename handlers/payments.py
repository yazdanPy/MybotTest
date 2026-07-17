# -*- coding: utf-8 -*-
"""
Record a settlement payment (کارت‌به‌کارت etc). The payment starts as
'pending' and only counts toward balances once the receiver taps
"✅ بله، دریافت کردم" -- this single-tap confirmation keeps the ledger
trustworthy without adding real friction.
"""
import re

import jalali_utils as ju
import keyboards as kb
from handlers.common import get_active_user_or_warn, get_db, cancel_conversation, send_main_menu
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler

PAY_TARGET, PAY_AMOUNT, PAY_NOTE, PAY_CONFIRM, PAY_RECEIPT = range(5)


async def payment_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = await get_active_user_or_warn(update, context)
    if user is None:
        return ConversationHandler.END
    db = get_db(context)
    others = [u for u in db.list_active_users() if u["id"] != user["id"]]
    if not others:
        await update.effective_message.reply_text("عضو دیگه‌ای در جمع نیست که بهش پرداخت ثبت کنی.")
        return ConversationHandler.END

    context.user_data.clear()
    programs = db.list_programs_for_user(user["id"], active_only=True)
    await update.effective_message.reply_text(
        "به کی پرداخت کردی؟ (یا کدوم برنامه رو شارژ کردی) 💸",
        reply_markup=kb.payment_target_keyboard(others, programs),
    )
    return PAY_TARGET


async def target_chosen_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data.startswith("paytargetprog_"):
        program_id = int(query.data.rsplit("_", 1)[1])
        context.user_data["pay_target_program_id"] = program_id
        context.user_data.pop("pay_target_id", None)
    else:
        target_id = int(query.data.split("_", 1)[1])
        context.user_data["pay_target_id"] = target_id
        context.user_data.pop("pay_target_program_id", None)
    await query.edit_message_text("چقدر پرداخت کردی؟ 💰 (فقط عدد، به تومان)")
    return PAY_AMOUNT


async def pay_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount = ju.parse_amount(update.message.text)
    if amount is None:
        await update.message.reply_text("فقط یه عدد مثبت بفرست، مثلاً 200000")
        return PAY_AMOUNT
    context.user_data["pay_amount"] = amount
    await update.message.reply_text(
        "توضیحی هست؟ (اختیاری - مثلاً «بابت شام هفته پیش») یا رد کن:",
        reply_markup=kb.skip_note_keyboard(),
    )
    return PAY_NOTE


def _payment_preview(db, context) -> str:
    amount = context.user_data["pay_amount"]
    note = context.user_data.get("pay_note")
    program_id = context.user_data.get("pay_target_program_id")
    if program_id:
        program = db.get_program(program_id)
        target_label = f"شارژ کارت مادر برنامه‌ی «{program['name']}»"
    else:
        target = db.get_user_by_id(context.user_data["pay_target_id"])
        target_label = target["first_name"]
    lines = [
        "💸 <b>پیش‌نمایش پرداخت</b>",
        f"به: {target_label}",
        f"مبلغ: {ju.format_money(amount)}",
        f"توضیح: {note if note else 'بدون توضیح'}",
        "",
        "تایید می‌کنی؟",
    ]
    return "\n".join(lines)


async def note_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["pay_note"] = None
    db = get_db(context)
    await query.edit_message_text(_payment_preview(db, context), parse_mode="HTML", reply_markup=kb.payment_confirm_keyboard())
    return PAY_CONFIRM


async def note_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["pay_note"] = update.message.text.strip()[:300]
    db = get_db(context)
    await update.message.reply_text(_payment_preview(db, context), parse_mode="HTML", reply_markup=kb.payment_confirm_keyboard())
    return PAY_CONFIRM


async def payment_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    db = get_db(context)

    if query.data == "payment_cancel":
        await query.edit_message_text("لغو شد. ❌")
        context.user_data.clear()
        return ConversationHandler.END

    sender = db.get_user_by_telegram_id(update.effective_user.id)
    amount = context.user_data["pay_amount"]
    note = context.user_data.get("pay_note")
    program_id = context.user_data.get("pay_target_program_id")

    if program_id:
        program = db.get_program(program_id)
        charge_id = db.create_program_charge(program_id, sender["id"], amount, note)
        await query.edit_message_text(
            f"ثبت شد ✅ منتظر تایید مدیر گروه هستیم که شارژ «{program['name']}» رو تایید کنه."
        )
        notify_text = (
            f"💰 <b>{sender['first_name']}</b> میگه {ju.format_money(amount)} برای «{program['name']}» شارژ کرده"
            + (f"\nتوضیح: {note}" if note else "")
            + "\n\nتایید می‌کنی؟"
        )
        for admin in db.list_admins():
            try:
                await context.bot.send_message(
                    chat_id=admin["telegram_id"], text=notify_text, parse_mode="HTML",
                    reply_markup=kb.program_charge_confirm_keyboard(charge_id),
                )
            except Exception:
                pass
        context.user_data["pay_id_for_receipt"] = charge_id
        context.user_data["pay_receipt_kind"] = "charge"
    else:
        target_id = context.user_data["pay_target_id"]
        payment_id = db.create_payment(sender["id"], target_id, amount, note)
        target = db.get_user_by_id(target_id)

        await query.edit_message_text(
            f"ثبت شد ✅ منتظر تایید {target['first_name']} هستیم که بگه دریافت کرده."
        )

        notify_text = (
            f"💰 <b>{sender['first_name']}</b> میگه بهت {ju.format_money(amount)} پرداخت کرده"
            + (f"\nتوضیح: {note}" if note else "")
            + "\n\nتایید می‌کنی که دریافتش کردی؟"
        )
        try:
            await context.bot.send_message(
                chat_id=target["telegram_id"], text=notify_text, parse_mode="HTML",
                reply_markup=kb.payment_receipt_keyboard(payment_id),
            )
        except Exception:
            await query.message.reply_text(
                "⚠️ نتونستم به طرف مقابل پیام بدم (شاید ربات رو بلاک کرده). "
                "بهش بگو دستی وارد ربات بشه و پرداخت رو تایید کنه."
            )
        context.user_data["pay_id_for_receipt"] = payment_id
        context.user_data["pay_receipt_kind"] = "payment"

    await query.message.reply_text(
        "می‌خوای رسید واریز رو هم ضمیمه کنی؟ 🧾 (عکس بفرست یا یه توضیح متنی بنویس)",
        reply_markup=kb.receipt_prompt_keyboard(),
    )
    return PAY_RECEIPT


async def receipt_attach_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("باشه، بدون رسید ثبت موند. ✅")
    db = get_db(context)
    sender = db.get_user_by_telegram_id(update.effective_user.id)
    context.user_data.clear()
    await send_main_menu(update, sender)
    return ConversationHandler.END


async def receipt_attach_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db = get_db(context)
    record_id = context.user_data["pay_id_for_receipt"]
    file_id = update.message.photo[-1].file_id
    if context.user_data.get("pay_receipt_kind") == "charge":
        db.set_program_charge_receipt(record_id, file_id=file_id)
    else:
        db.set_payment_receipt(record_id, file_id=file_id)
    await update.message.reply_text("رسید ضمیمه شد. ✅")
    sender = db.get_user_by_telegram_id(update.effective_user.id)
    context.user_data.clear()
    await send_main_menu(update, sender)
    return ConversationHandler.END


async def receipt_attach_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db = get_db(context)
    record_id = context.user_data["pay_id_for_receipt"]
    text = update.message.text.strip()[:1000]
    if context.user_data.get("pay_receipt_kind") == "charge":
        db.set_program_charge_receipt(record_id, text=text)
    else:
        db.set_payment_receipt(record_id, text=text)
    await update.message.reply_text("توضیح رسید ثبت شد. ✅")
    sender = db.get_user_by_telegram_id(update.effective_user.id)
    context.user_data.clear()
    await send_main_menu(update, sender)
    return ConversationHandler.END


async def receipt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = get_db(context)

    action, payment_id_str = query.data.rsplit("_", 1)
    payment_id = int(payment_id_str)
    payment = db.get_payment(payment_id)
    if payment is None or payment["status"] != "pending":
        await query.edit_message_text("این پرداخت قبلاً بررسی شده یا پیدا نشد.")
        return

    receiver = db.get_user_by_telegram_id(update.effective_user.id)
    if receiver is None or receiver["id"] != payment["to_user_id"]:
        await query.answer("این دکمه برای تو نیست.", show_alert=True)
        return

    sender = db.get_user_by_id(payment["from_user_id"])

    if action == "payrecv_confirm":
        db.set_payment_status(payment_id, "confirmed")
        await query.edit_message_text("✅ تایید شد. حساب‌کتابتون به‌روز شد.")
        try:
            await context.bot.send_message(
                sender["telegram_id"],
                f"✅ {receiver['first_name']} پرداخت {ju.format_money(payment['amount'])} تو رو تایید کرد.",
            )
        except Exception:
            pass
    else:
        db.set_payment_status(payment_id, "rejected")
        await query.edit_message_text("پرداخت رد شد. لطفاً با طرف مقابل هماهنگ کن.")
        try:
            await context.bot.send_message(
                sender["telegram_id"],
                f"❌ {receiver['first_name']} گفت پرداخت {ju.format_money(payment['amount'])} رو دریافت نکرده. "
                "لطفاً باهم چک کنید.",
            )
        except Exception:
            pass


payment_conv = ConversationHandler(
    entry_points=[
        CommandHandler("payment", payment_entry),
        MessageHandler(filters.Regex(f"^{re.escape(kb.BTN_NEW_PAYMENT)}$"), payment_entry),
    ],
    states={
        PAY_TARGET: [CallbackQueryHandler(target_chosen_callback, pattern=r"^(paytarget_|paytargetprog_)")],
        PAY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_amount_received)],
        PAY_NOTE: [
            CallbackQueryHandler(note_skip_callback, pattern=r"^note_skip$"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, note_text_received),
        ],
        PAY_CONFIRM: [CallbackQueryHandler(payment_confirm_callback, pattern=r"^payment_(confirm|cancel)$")],
        PAY_RECEIPT: [
            CallbackQueryHandler(receipt_attach_skip_callback, pattern=r"^receipt_skip$"),
            MessageHandler(filters.PHOTO, receipt_attach_photo_received),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_attach_text_received),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_conversation),
        MessageHandler(filters.COMMAND, cancel_conversation),
    ],
    name="payment_conv",
    persistent=False,
)

receipt_query_handler = CallbackQueryHandler(receipt_callback, pattern=r"^payrecv_(confirm|reject)_\d+$")
