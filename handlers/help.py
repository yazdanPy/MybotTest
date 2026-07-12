# -*- coding: utf-8 -*-
import re

import keyboards as kb
from handlers.common import HELP_TEXT, ADMIN_HELP_EXTRA, get_active_user_or_warn, get_db
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, MessageHandler, filters


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = get_db(context)
    user = db.get_user_by_telegram_id(update.effective_user.id)
    text = HELP_TEXT
    if user and user["is_admin"]:
        text += ADMIN_HELP_EXTRA
    await update.effective_message.reply_text(text, parse_mode="HTML")


help_command_handler = CommandHandler("help", help_command)
help_button_handler = MessageHandler(filters.Regex(f"^{re.escape(kb.BTN_HELP)}$"), help_command)
