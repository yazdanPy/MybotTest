# -*- coding: utf-8 -*-
"""
/start + registration conversation (ask card number, then household weight)
+ the admin approve/reject callback that activates a pending member.

Bootstrap rule: if no admin exists yet in the whole database, OR the
telegram_id is listed in config.INITIAL_ADMIN_IDS, the very first /start
from that person makes them an admin instantly (no approval needed) --
otherwise there'd be nobody around to approve the first person in ever.
"""
import config
import jalali_utils as ju
import keyboards as kb
from handlers.common import send_main_menu, get_db, cancel_conversation
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler

REG_CARD, REG_WEIGHT = range(2)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db = get_db(context)
    tg = update.effective_user
    user = db.get_user_by_telegram_id(tg.id)

    if user is not None:
        # keep username fresh
        if user["username"] != tg.username:
            db.update_username(user["id"], tg.username)

        if user["status"] == "active":
            await update.message.reply_text(f"سلام {user['first_name']} 👋 خوش برگشتی!")
            await send_main_menu(update, user)
            return ConversationHandler.END
        elif user["status"] == "pending":
            await update.message.reply_text("درخواست عضویتت ثبت شده و منتظر تایید مدیر گروهه ⏳")
            return ConversationHandler.END
        else:  # removed
            await update.message.reply_text(
                "قبلاً از این جمع حذف شدی. اگه فکر می‌کنی اشتباهه با مدیر گروه صحبت کن."
            )
            return ConversationHandler.END

    # brand-new person
    user_id = db.create_pending_user(tg.id, tg.username, tg.first_name or "دوست جدید")
    context.user_data["reg_user_id"] = user_id
    await update.message.reply_text(
        f"سلام {tg.first_name} 👋 به ربات حساب‌کتاب خوش اومدی!\n\n"
        "برای عضویت دو تا چیز نیاز دارم:\n\n"
        "1️⃣ شماره کارت بانکیت (۱۶ رقمی) که بقیه بتونن بهت واریز کنن.\n"
        "همینو بفرست (با خط تیره یا بدون خط تیره، فرقی نداره):"
    )
    return REG_CARD


async def reg_card_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    card = ju.clean_card_number(update.message.text)
    if card is None:
        await update.message.reply_text(
            "این شماره کارت معتبر به نظر نمی‌رسه 🤔 باید ۱۶ رقم باشه. دوباره بفرست:"
        )
        return REG_CARD
    context.user_data["reg_card"] = card
    await update.message.reply_text(
        "2️⃣ معمولاً با چند نفر میای؟ (خودت + همراهانی مثل همسر/فرزند که معمولاً باهاته)\n"
        "فقط عدد بفرست. اگه همیشه تنها میای، بنویس: 1"
    )
    return REG_WEIGHT


async def reg_weight_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    weight = ju.parse_positive_int(update.message.text)
    if weight is None or weight > 20:
        await update.message.reply_text("یه عدد معتبر بفرست (مثلاً 1 یا 2 یا 3):")
        return REG_WEIGHT

    db = get_db(context)
    user_id = context.user_data["reg_user_id"]
    card = context.user_data["reg_card"]
    db.set_user_registration_details(user_id, card, weight)

    tg = update.effective_user
    should_bootstrap = (not db.any_admin_exists()) or (tg.id in config.INITIAL_ADMIN_IDS)

    if should_bootstrap:
        db.activate_user(user_id, as_admin=True)
        user = db.get_user_by_id(user_id)
        await update.message.reply_text(
            "تمومه! 🎉 چون اولین نفر بودی (یا مدیر از قبل تنظیم‌شده هستی)، به عنوان "
            "<b>مدیر گروه</b> فعال شدی. حالا می‌تونی بقیه دوستات رو اضافه کنی (بهشون بگو /start بزنن، "
            "بعد از داخل «⏳ درخواست‌های در انتظار» تاییدشون کن).",
            parse_mode="HTML",
        )
        await send_main_menu(update, user)
    else:
        await update.message.reply_text(
            "مرسی! ✅ اطلاعاتت ثبت شد و به مدیر گروه اطلاع دادم. به محض تایید، خبرت می‌کنم."
        )
        db_admins = db.list_admins()
        user = db.get_user_by_id(user_id)
        notify_text = (
            f"👤 <b>درخواست عضویت جدید</b>\n\n"
            f"نام: {user['first_name']}"
            + (f" (@{user['username']})" if user["username"] else "")
            + f"\nتعداد نفرات: {weight}\n"
            f"شماره کارت: {ju.format_card_number_html(card)}\n\n"
            "تایید می‌کنی؟"
        )
        for admin in db_admins:
            try:
                await context.bot.send_message(
                    chat_id=admin["telegram_id"], text=notify_text,
                    parse_mode="HTML", reply_markup=kb.approval_keyboard(user_id),
                )
            except Exception:
                pass  # admin may have blocked the bot etc. -- not fatal

    context.user_data.clear()
    return ConversationHandler.END


async def approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = get_db(context)

    action, user_id_str = query.data.rsplit("_", 1)
    user_id = int(user_id_str)
    user = db.get_user_by_id(user_id)
    if user is None:
        await query.edit_message_text("این کاربر دیگه پیدا نشد.")
        return

    admin = db.get_user_by_telegram_id(update.effective_user.id)
    if admin is None or not admin["is_admin"]:
        await query.answer("این کار فقط برای مدیر گروهه.", show_alert=True)
        return

    if user["status"] != "pending":
        await query.edit_message_text(f"درخواست {user['first_name']} قبلاً بررسی شده بود.")
        return

    if action == "reg_approve":
        db.activate_user(user_id, as_admin=False)
        await query.edit_message_text(f"✅ {user['first_name']} به جمع اضافه شد.")
        try:
            await context.bot.send_message(
                chat_id=user["telegram_id"],
                text="🎉 عضویتت توسط مدیر گروه تایید شد! حالا می‌تونی هزینه ثبت کنی.",
            )
            fresh = db.get_user_by_id(user_id)
            await context.bot.send_message(
                chat_id=user["telegram_id"], text="از منوی زیر استفاده کن:",
                reply_markup=kb.main_menu(bool(fresh["is_admin"])),
            )
        except Exception:
            pass
    else:  # reg_reject
        db.reject_user(user_id)
        await query.edit_message_text(f"❌ درخواست {user['first_name']} رد شد.")
        try:
            await context.bot.send_message(
                chat_id=user["telegram_id"],
                text="متاسفانه درخواست عضویتت توسط مدیر گروه رد شد.",
            )
        except Exception:
            pass


registration_conv = ConversationHandler(
    entry_points=[CommandHandler("start", start_command)],
    states={
        REG_CARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_card_received)],
        REG_WEIGHT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_weight_received)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_conversation),
        MessageHandler(filters.COMMAND, cancel_conversation),
    ],
    name="registration_conv",
    persistent=False,
)

approval_query_handler = CallbackQueryHandler(approval_callback, pattern=r"^reg_(approve|reject)_\d+$")
