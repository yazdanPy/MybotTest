# -*- coding: utf-8 -*-
"""Shared helpers used across every handler module."""
import logging

from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler

import keyboards as kb

logger = logging.getLogger("hesabkitab_bot")

HELP_TEXT = (
    "🤖 <b>راهنمای ربات حساب‌کتاب دوستانه</b>\n\n"
    "این ربات حساب‌کتاب دورهمی‌های شما رو نگه می‌داره: هرکی هرچی حساب کرد ثبت می‌کنه، "
    "ربات با توجه به تعداد نفرات هر کسی هزینه رو تقسیم می‌کنه و بدهی‌ها رو با هم "
    "تهاتر می‌کنه تا فقط یه عدد نهایی بین هرکسی باقی بمونه.\n\n"
    f"{kb.BTN_NEW_EXPENSE}\nوقتی جایی رو حساب کردی، اینجا ثبتش کن. مبلغ، بابت چی بوده و "
    "کیا بودن رو می‌پرسم و خودم حساب می‌کنم.\n\n"
    f"{kb.BTN_NEW_PAYMENT}\nوقتی بابت بدهیت کارت‌به‌کارت کردی، اینجا ثبتش کن تا طرف مقابل تایید کنه.\n\n"
    f"{kb.BTN_MY_BALANCE}\nمی‌بینی به کی بدهکاری و کی به تو بدهکاره (با شماره کارت).\n\n"
    f"{kb.BTN_HISTORY}\nریز هزینه‌های ثبت‌شده رو می‌بینی.\n\n"
    "در هر مرحله می‌تونی با /cancel لغو کنی.\n\n"
    "اعضای جدید فقط کافیه /start بزنن؛ عضویتشون باید توسط مدیر گروه تایید بشه."
)

ADMIN_HELP_EXTRA = (
    "\n\n👑 <b>دستورات مخصوص مدیر</b>\n"
    f"{kb.BTN_ADMIN_PENDING}\nدرخواست عضویت افراد جدید رو تایید یا رد می‌کنی.\n\n"
    f"{kb.BTN_ADMIN_MEMBERS}\nتعداد نفرات، شماره کارت، مدیر بودن یا حذف عضو رو تغییر می‌دی.\n\n"
    f"{kb.BTN_ADMIN_REPORT}\nگزارش کامل بدهی‌های گروه + پیشنهاد تسویه با کمترین تعداد واریزی + خروجی اکسل.\n\n"
    "برای اینکه ربات خلاصه هر هزینه رو خودکار توی گروهتون بفرسته، دستور /setgroup رو "
    "داخل گروه تلگرامی‌تون بزن."
)


async def send_main_menu(update: Update, user: dict, text: str = "چیکار می‌تونم برات بکنم؟"):
    await update.effective_message.reply_text(text, reply_markup=kb.main_menu(bool(user["is_admin"])))


def get_db(context: ContextTypes.DEFAULT_TYPE):
    return context.bot_data["db"]


async def get_active_user_or_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Returns the active user's DB row, or None (after sending an explanatory
    message) if they are not registered / not yet approved / removed.
    """
    db = get_db(context)
    tg_user = update.effective_user
    user = db.get_user_by_telegram_id(tg_user.id)
    if user is None:
        await update.effective_message.reply_text(
            "هنوز عضو این جمع نیستی 🙂 برای عضویت دستور /start رو بزن."
        )
        return None
    if user["status"] == "pending":
        await update.effective_message.reply_text(
            "درخواست عضویتت هنوز توسط مدیر گروه تایید نشده. یکم صبر کن ⏳"
        )
        return None
    if user["status"] == "removed":
        await update.effective_message.reply_text(
            "دسترسیت به این جمع حذف شده. اگه فکر می‌کنی اشتباهه با مدیر گروه صحبت کن."
        )
        return None
    return user


async def get_admin_or_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = await get_active_user_or_warn(update, context)
    if user is None:
        return None
    if not user["is_admin"]:
        await update.effective_message.reply_text("این قابلیت فقط برای مدیر گروه در دسترسه 🔒")
        return None
    return user


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    db = get_db(context)
    user = db.get_user_by_telegram_id(update.effective_user.id)
    await update.effective_message.reply_text(
        "لغو شد. ❌", reply_markup=kb.main_menu(bool(user["is_admin"])) if user else None
    )
    return ConversationHandler.END


async def error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception while processing update", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "یه خطای غیرمنتظره پیش اومد 😔 لطفاً دوباره امتحان کن. اگه ادامه داشت با /cancel شروع کن."
            )
    except Exception:
        pass
