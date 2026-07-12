# -*- coding: utf-8 -*-
"""/setgroup -- run inside your friends' Telegram group so the bot knows where
to post the automatic expense/balance announcements."""
import config
from handlers.common import get_admin_or_warn, get_db
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler


async def setgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(
            "این دستور رو باید داخل گروه تلگرامی دوستانتون بزنید، نه توی چت خصوصی."
        )
        return

    admin = await get_admin_or_warn(update, context)
    if admin is None:
        return

    db = get_db(context)
    db.set_setting(config.GROUP_CHAT_ID_SETTING_KEY, str(update.effective_chat.id))
    await update.effective_message.reply_text(
        "✅ از این به بعد خلاصه هر هزینه جدید و وضعیت بدهی‌ها خودکار همینجا فرستاده میشه."
    )


setgroup_command_handler = CommandHandler("setgroup", setgroup_command)
