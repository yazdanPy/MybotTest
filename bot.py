# -*- coding: utf-8 -*-
"""Entry point: wires up the database and every handler, then starts polling Telegram."""
import logging

from telegram import Update, BotCommand
from telegram.ext import Application, MessageHandler, CallbackQueryHandler, filters

import config
from database import Database
from handlers.common import error_handler
from handlers import registration, members, expenses, payments, reports, groupsetup, programs, help as help_module

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("hesabkitab_bot")


async def unknown_text(update: Update, context):
    await update.effective_message.reply_text(
        "متوجه نشدم 🤔 از دکمه‌های پایین صفحه استفاده کن یا /help رو بزن."
    )


async def unknown_callback(update: Update, context):
    await update.callback_query.answer("این دکمه دیگه معتبر نیست.", show_alert=False)


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("start", "شروع / عضویت در ربات"),
        BotCommand("expense", "ثبت هزینه جدید"),
        BotCommand("payment", "ثبت پرداخت"),
        BotCommand("balance", "وضعیت حساب من"),
        BotCommand("history", "تاریخچه هزینه‌ها"),
        BotCommand("programs", "برنامه‌های بلندمدت (مثل سفر)"),
        BotCommand("report", "گزارش کامل گروه (مدیر)"),
        BotCommand("export", "خروجی اکسل (مدیر)"),
        BotCommand("members", "مدیریت اعضا (مدیر)"),
        BotCommand("pending", "درخواست‌های در انتظار (مدیر)"),
        BotCommand("setgroup", "ثبت گروه برای اطلاع‌رسانی خودکار"),
        BotCommand("help", "راهنما"),
        BotCommand("cancel", "لغو عملیات جاری"),
    ])
    logger.info("Bot commands registered.")


def build_application() -> Application:
    if not config.BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN تنظیم نشده. یه فایل .env بساز (از روی .env.example) و توکن ربات رو توش بذار."
        )

    builder = Application.builder().token(config.BOT_TOKEN).post_init(post_init)
    if config.PROXY_URL:
        builder = builder.proxy(config.PROXY_URL).get_updates_proxy(config.PROXY_URL)
        logger.info("Using proxy for Telegram connection: %s", config.PROXY_URL)
    application = builder.build()
    application.bot_data["db"] = Database(config.DB_PATH)

    # --- conversations (order matters only in that these must come before the catch-alls)
    application.add_handler(registration.registration_conv)
    application.add_handler(members.members_conv)
    application.add_handler(expenses.expense_conv)
    application.add_handler(payments.payment_conv)
    application.add_handler(programs.program_conv)

    # --- standalone command handlers
    application.add_handler(members.pending_command_handler)
    application.add_handler(members.pending_button_handler)
    application.add_handler(reports.balance_command_handler)
    application.add_handler(reports.balance_button_handler)
    application.add_handler(reports.history_command_handler)
    application.add_handler(reports.history_button_handler)
    application.add_handler(reports.report_command_handler)
    application.add_handler(reports.report_button_handler)
    application.add_handler(reports.export_command_handler)
    application.add_handler(groupsetup.setgroup_command_handler)
    application.add_handler(help_module.help_command_handler)
    application.add_handler(help_module.help_button_handler)

    # --- standalone callback query handlers (outside any conversation)
    application.add_handler(registration.approval_query_handler)
    application.add_handler(payments.receipt_query_handler)
    application.add_handler(reports.history_page_query_handler)
    application.add_handler(reports.delete_expense_query_handler)
    application.add_handler(reports.view_receipt_query_handler)
    application.add_handler(programs.expense_approval_query_handler)
    application.add_handler(programs.charge_confirmation_query_handler)

    # --- catch-alls (must be added last so specific handlers above get first refusal)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_text))
    application.add_handler(CallbackQueryHandler(unknown_callback))

    application.add_error_handler(error_handler)
    return application


def main():
    application = build_application()
    logger.info("Starting bot polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
