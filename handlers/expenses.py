# -*- coding: utf-8 -*-
"""
New-expense conversation:
  description -> who was present -> split mode -> [mode-specific amount entry]
  -> preview (with option to change payer) -> confirm & save -> announce to the
  group with the fresh, netted balances -> optional receipt attachment.

Split modes:
  weighted -> ask total, then let the user fine-tune each person's headcount
              for just this expense (defaults from their profile).
  equal    -> ask total, split evenly among everyone selected.
  custom   -> ask each selected person's exact amount one by one (e.g. a
              restaurant bill where everyone ordered something different-priced).
  ocr      -> upload a photo of the receipt; Veryfi extracts each line item,
              then for every item you tag which participant(s) it belongs to.
              Internally stored as split_mode="custom" since the resulting data
              shape (a manual amount per person) is identical either way.
"""
import re

import config
import jalali_utils as ju
import keyboards as kb
import veryfi_client
from balances import group_report
from handlers.common import get_active_user_or_warn, get_db, cancel_conversation, send_main_menu
from split_engine import calculate_shares
from telegram import Update
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters, CallbackQueryHandler

(
    EXP_DESC, EXP_PROGRAM_CHOICE, EXP_PARTICIPANTS, EXP_SPLIT_MODE, EXP_AMOUNT, EXP_WEIGHTS,
    EXP_CUSTOM_AMOUNTS, EXP_OCR_PHOTO, EXP_OCR_ITEM, EXP_OCR_REMAINDER,
    EXP_CONFIRM, EXP_CHANGE_PAYER, EXP_RECEIPT,
) = range(13)

MODE_LABELS = {
    "weighted": "بر اساس تعداد نفرات",
    "equal": "مساوی بین همه",
    "custom": "مبلغ دلخواه برای هرکس",
}


async def expense_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = await get_active_user_or_warn(update, context)
    if user is None:
        return ConversationHandler.END
    db = get_db(context)
    if len(db.list_active_users()) < 1:
        await update.effective_message.reply_text("هنوز هیچ عضو فعالی نیست.")
        return ConversationHandler.END

    context.user_data.clear()
    context.user_data["exp_payer_id"] = user["id"]
    await update.effective_message.reply_text(
        "بابت چی بود؟ 📝 (مثلاً: شام رستوران، بلیط سینما، تاکسی)",
        reply_markup=kb.cancel_inline_keyboard(),
    )
    return EXP_DESC


async def description_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    desc = update.message.text.strip()
    if not desc:
        await update.message.reply_text("یه توضیح کوتاه بفرست:")
        return EXP_DESC
    context.user_data["exp_description"] = desc[:300]

    db = get_db(context)
    payer = db.get_user_by_id(context.user_data["exp_payer_id"])
    programs = db.list_programs_for_user(payer["id"], active_only=True)
    if programs:
        await update.message.reply_text(
            "این هزینه مربوط به یه برنامه‌ست؟ 📁", reply_markup=kb.program_choice_for_expense_keyboard(programs)
        )
        return EXP_PROGRAM_CHOICE

    return await _ask_participants(update, context, edit=False)


def _candidate_users(db, context) -> list[dict]:
    """Who can be picked as a participant: everyone active normally, or just
    this program's participants if the expense is tied to one."""
    program_id = context.user_data.get("exp_program_id")
    if program_id:
        program = db.get_program(program_id)
        return [db.get_user_by_id(pp["user_id"]) for pp in program["participants"]]
    return db.list_active_users()


async def _ask_participants(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool) -> int:
    db = get_db(context)
    candidates = _candidate_users(db, context)
    context.user_data["exp_selected"] = {u["id"] for u in candidates}
    text = "کیا بودن؟ 👥 (پیش‌فرض همه انتخاب شدن؛ کسی که نبود رو بزن که غیرفعال بشه)"
    markup = kb.participants_keyboard(candidates, context.user_data["exp_selected"])
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    return EXP_PARTICIPANTS


async def program_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data != "expprog_none":
        program_id = int(query.data.split("_", 1)[1])
        context.user_data["exp_program_id"] = program_id
    else:
        context.user_data["exp_program_id"] = None
    return await _ask_participants(update, context, edit=True)


async def participants_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    db = get_db(context)
    candidates = _candidate_users(db, context)
    selected: set = context.user_data.setdefault("exp_selected", {u["id"] for u in candidates})

    if query.data == "toggle_all":
        selected.clear()
        selected.update(u["id"] for u in candidates)
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
            await query.answer("حداقل یه نفر باید انتخاب بشه!", show_alert=True)
            return EXP_PARTICIPANTS
        await query.edit_message_text(
            "هزینه رو چطوری تقسیم کنم؟",
            reply_markup=kb.split_mode_keyboard(show_ocr=config.VERYFI_ENABLED),
        )
        return EXP_SPLIT_MODE

    await query.edit_message_reply_markup(reply_markup=kb.participants_keyboard(candidates, selected))
    return EXP_PARTICIPANTS


def _build_preview(db, context) -> str:
    payer = db.get_user_by_id(context.user_data["exp_payer_id"])
    amount = context.user_data["exp_amount"]
    description = context.user_data["exp_description"]
    shares: dict = context.user_data["exp_shares"]
    mode = context.user_data["exp_split_mode"]
    mode_label = MODE_LABELS.get(mode, mode)

    lines = [
        "🧾 <b>پیش‌نمایش هزینه</b>",
        f"📌 بابت: {description}",
        f"💰 مبلغ کل: {ju.format_money(amount)}",
        f"📅 تاریخ: {ju.jalali_pretty(ju.now_iran())}",
        f"👤 پرداخت‌کننده: {payer['first_name']}",
        f"🔀 نحوه تقسیم: {mode_label}",
        "",
        "سهم هرکس:",
    ]
    for uid, share in sorted(shares.items(), key=lambda kv: -kv[1]):
        u = db.get_user_by_id(uid)
        tag = "(خودش پرداخت کرد)" if uid == payer["id"] else f"→ بدهکار به {payer['first_name']}"
        headcount = ""
        if mode == "weighted":
            w = context.user_data.get("exp_weights", {}).get(uid)
            if w:
                headcount = f" ({w} نفر)"
        lines.append(f"  • {u['first_name']}{headcount}: {ju.format_money(share)} {tag}")

    return "\n".join(lines)


async def _show_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db = get_db(context)
    text = _build_preview(db, context)
    markup = kb.expense_confirm_keyboard()
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    return EXP_CONFIRM


# --------------------------------------------------------------- split mode
async def split_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "split_custom":
        context.user_data["exp_split_mode"] = "custom"
        return await _start_custom_amounts(update, context)

    if data == "split_ocr":
        if not config.VERYFI_ENABLED:
            await query.answer("این قابلیت فعال نیست.", show_alert=True)
            return EXP_SPLIT_MODE
        context.user_data["exp_split_mode"] = "custom"
        await query.edit_message_text("📷 عکس رسید یا فاکتور رو بفرست تا آیتم‌هاش رو خودم دربیارم.")
        return EXP_OCR_PHOTO

    mode = "weighted" if data == "split_weighted" else "equal"
    context.user_data["exp_split_mode"] = mode
    await query.edit_message_text("چقدر خرج کردی؟ 💰 (فقط عدد، به تومان)")
    return EXP_AMOUNT


async def amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount = ju.parse_amount(update.message.text)
    if amount is None:
        await update.message.reply_text("فقط یه عدد مثبت بفرست، مثلاً 500000 یا 500,000")
        return EXP_AMOUNT
    context.user_data["exp_amount"] = amount

    db = get_db(context)
    mode = context.user_data["exp_split_mode"]
    selected_ids = context.user_data["exp_selected"]

    if mode == "equal":
        participants = [{"user_id": uid, "weight": 1} for uid in selected_ids]
        shares = calculate_shares(amount, participants, "equal")
        context.user_data["exp_shares"] = shares
        context.user_data["exp_weights"] = {uid: 1 for uid in selected_ids}
        return await _show_preview(update, context)

    # weighted mode: let the user fine-tune each person's headcount for THIS
    # expense specifically (defaults to their profile setting, but someone's
    # family size at a given outing can vary -- e.g. today Mehrab counts as 2,
    # Saleh as 1, Amir as 4 -- without touching anyone's permanent profile).
    # If this expense belongs to a program, the default instead comes from
    # that program's own per-person headcount ("زیرمجموعه") set when it was created.
    program_id = context.user_data.get("exp_program_id")
    if program_id:
        weights = {uid: db.get_program_participant_weight(program_id, uid) for uid in selected_ids}
    else:
        weights = {uid: db.get_user_by_id(uid)["weight"] for uid in selected_ids}
    context.user_data["exp_weights"] = weights
    users = [db.get_user_by_id(uid) for uid in selected_ids]
    await update.message.reply_text(
        "تعداد نفرات هرکس رو برای همین هزینه مشخص کن 👨‍👩‍👧\n"
        "(پیش‌فرض از پروفایل خودشونه؛ با ➖ ➕ می‌تونی فقط برای همین یه بار تغییرش بدی)",
        reply_markup=kb.weights_keyboard(users, weights),
    )
    return EXP_WEIGHTS


async def weights_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    db = get_db(context)
    weights: dict = context.user_data["exp_weights"]
    selected_ids = context.user_data["exp_selected"]
    users = [db.get_user_by_id(uid) for uid in selected_ids]

    if query.data == "weights_done":
        await query.answer()
        participants = [{"user_id": uid, "weight": weights[uid]} for uid in selected_ids]
        shares = calculate_shares(context.user_data["exp_amount"], participants, "weighted")
        context.user_data["exp_shares"] = shares
        return await _show_preview(update, context)

    if query.data.startswith("wnoop_"):
        await query.answer()
        return EXP_WEIGHTS

    action, uid_str = query.data.split("_", 1)
    uid = int(uid_str)
    current = weights.get(uid, 1)
    if action == "winc":
        weights[uid] = min(20, current + 1)
    else:  # wdec
        weights[uid] = max(1, current - 1)
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=kb.weights_keyboard(users, weights))
    return EXP_WEIGHTS


# -------------------------------------------------------- custom amounts
async def _start_custom_amounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["exp_custom_queue"] = sorted(context.user_data["exp_selected"])
    context.user_data["exp_custom_index"] = 0
    context.user_data["exp_custom_amounts"] = {}
    return await _ask_next_custom_amount(update, context)


async def _ask_next_custom_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db = get_db(context)
    queue = context.user_data["exp_custom_queue"]
    idx = context.user_data["exp_custom_index"]
    user = db.get_user_by_id(queue[idx])
    text = f"سهم {user['first_name']} از این هزینه چقدر بود؟ 💰 ({idx + 1}/{len(queue)})"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb.cancel_inline_keyboard())
    else:
        await update.effective_message.reply_text(text, reply_markup=kb.cancel_inline_keyboard())
    return EXP_CUSTOM_AMOUNTS


async def custom_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount = ju.parse_amount(update.message.text)
    if amount is None:
        await update.message.reply_text("فقط یه عدد مثبت بفرست:")
        return EXP_CUSTOM_AMOUNTS
    queue = context.user_data["exp_custom_queue"]
    idx = context.user_data["exp_custom_index"]
    context.user_data["exp_custom_amounts"][queue[idx]] = amount
    context.user_data["exp_custom_index"] += 1

    if context.user_data["exp_custom_index"] >= len(queue):
        return await _finalize_custom_shares(update, context, context.user_data["exp_custom_amounts"])
    return await _ask_next_custom_amount(update, context)


async def _finalize_custom_shares(update: Update, context: ContextTypes.DEFAULT_TYPE, totals: dict) -> int:
    totals = {uid: amt for uid, amt in totals.items() if amt > 0}
    if not totals:
        msg = "هیچ مبلغی ثبت نشد. دوباره از اول امتحان کن یا /cancel بزن."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg)
        else:
            await update.effective_message.reply_text(msg)
        context.user_data.clear()
        return ConversationHandler.END

    context.user_data["exp_amount"] = sum(totals.values())
    context.user_data["exp_shares"] = totals
    context.user_data["exp_weights"] = {uid: 1 for uid in totals}
    context.user_data["exp_selected"] = set(totals.keys())
    return await _show_preview(update, context)


# --------------------------------------------------------------- OCR (Veryfi)
async def ocr_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text(
            "فقط عکس قبول می‌کنم 📷 اگه رسید نداری، /cancel بزن و «مبلغ دلخواه» رو انتخاب کن."
        )
        return EXP_OCR_PHOTO

    file_id = update.message.photo[-1].file_id
    context.user_data["exp_ocr_photo_file_id"] = file_id
    status_msg = await update.message.reply_text("⏳ در حال خوندن رسید با هوش مصنوعی... چند ثانیه طول می‌کشه.")

    try:
        tg_file = await context.bot.get_file(file_id)
        image_bytes = bytes(await tg_file.download_as_bytearray())
        result = await veryfi_client.extract_receipt(image_bytes, "receipt.jpg")
    except veryfi_client.VeryfiError as e:
        await status_msg.edit_text(
            f"❌ استخراج خودکار جواب نداد:\n{e}\n\nبه‌جاش دستی مبلغ هرکس رو می‌پرسم."
        )
        context.user_data["exp_split_mode"] = "custom"
        return await _start_custom_amounts(update, context)
    except Exception as e:
        await status_msg.edit_text(
            f"❌ یه خطای غیرمنتظره پیش اومد ({e}).\n\nبه‌جاش دستی مبلغ هرکس رو می‌پرسم."
        )
        context.user_data["exp_split_mode"] = "custom"
        return await _start_custom_amounts(update, context)

    context.user_data["exp_ocr_items"] = result.line_items
    context.user_data["exp_ocr_document_total"] = result.document_total
    context.user_data["exp_ocr_index"] = 0
    context.user_data["exp_ocr_selected"] = set()
    context.user_data["exp_ocr_totals"] = {}

    vendor_note = f" ({result.vendor_name})" if result.vendor_name else ""
    await status_msg.edit_text(f"✅ {len(result.line_items)} آیتم پیدا شد{vendor_note}. حالا مشخص کن مال کیه:")
    return await _prompt_current_item(update, context, edit=False)


def _current_item_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    items = context.user_data["exp_ocr_items"]
    idx = context.user_data["exp_ocr_index"]
    item = items[idx]
    return (
        f"🧾 آیتم {idx + 1} از {len(items)}:\n"
        f"<b>{item.description}</b> — {ju.format_money(item.amount)}\n\n"
        "مال کیه؟ (میشه چند نفر باشه، مثلاً یه پیش‌غذای مشترک)"
    )


async def _prompt_current_item(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool) -> int:
    db = get_db(context)
    users = [db.get_user_by_id(uid) for uid in sorted(context.user_data["exp_selected"])]
    selected = context.user_data["exp_ocr_selected"]
    idx = context.user_data["exp_ocr_index"]
    total = len(context.user_data["exp_ocr_items"])
    text = _current_item_text(context)
    markup = kb.item_assign_keyboard(users, selected, f"{idx + 1}/{total}")

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    return EXP_OCR_ITEM


async def ocr_item_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data
    selected: set = context.user_data["exp_ocr_selected"]

    if data.startswith("itemtag_"):
        uid = int(data.split("_", 1)[1])
        if uid in selected:
            selected.discard(uid)
        else:
            selected.add(uid)
        db = get_db(context)
        users = [db.get_user_by_id(u) for u in sorted(context.user_data["exp_selected"])]
        idx = context.user_data["exp_ocr_index"]
        total = len(context.user_data["exp_ocr_items"])
        await query.edit_message_reply_markup(reply_markup=kb.item_assign_keyboard(users, selected, f"{idx + 1}/{total}"))
        return EXP_OCR_ITEM

    items = context.user_data["exp_ocr_items"]
    idx = context.user_data["exp_ocr_index"]
    item = items[idx]

    if data == "item_confirm" and selected:
        totals: dict = context.user_data.setdefault("exp_ocr_totals", {})
        selected_sorted = sorted(selected)
        share = item.amount // len(selected_sorted)
        remainder = item.amount - share * len(selected_sorted)
        for i, uid in enumerate(selected_sorted):
            add = share + (remainder if i == len(selected_sorted) - 1 else 0)
            totals[uid] = totals.get(uid, 0) + add
    # "item_none", or "item_confirm" with nobody tagged -> this item just isn't
    # assigned to anyone; it still affects the tax/service remainder check below.

    context.user_data["exp_ocr_index"] += 1
    context.user_data["exp_ocr_selected"] = set()

    if context.user_data["exp_ocr_index"] >= len(items):
        return await _finish_ocr_items(update, context)

    return await _prompt_current_item(update, context, edit=True)


async def _finish_ocr_items(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    totals: dict = context.user_data.get("exp_ocr_totals", {})
    assigned_sum = sum(totals.values())
    doc_total = context.user_data.get("exp_ocr_document_total")

    if doc_total and doc_total > assigned_sum:
        remainder = doc_total - assigned_sum
        context.user_data["exp_ocr_remainder"] = remainder
        text = (
            f"جمع آیتم‌های تگ‌شده: {ju.format_money(assigned_sum)}\n"
            f"مبلغ کل رسید: {ju.format_money(doc_total)}\n"
            f"باقی‌مونده (احتمالاً مالیات/سرویس/انعام): {ju.format_money(remainder)}\n\n"
            "این مبلغ باقی‌مونده رو مساوی بین همه‌ی حاضرین تقسیم کنم؟"
        )
        await update.callback_query.edit_message_text(text, reply_markup=kb.remainder_keyboard())
        return EXP_OCR_REMAINDER

    return await _finalize_custom_shares(update, context, totals)


async def ocr_remainder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    totals: dict = context.user_data.get("exp_ocr_totals", {})

    if query.data == "remainder_split":
        remainder = context.user_data.get("exp_ocr_remainder", 0)
        participants = sorted(context.user_data["exp_selected"])
        share = remainder // len(participants)
        rem2 = remainder - share * len(participants)
        for i, uid in enumerate(participants):
            add = share + (rem2 if i == len(participants) - 1 else 0)
            totals[uid] = totals.get(uid, 0) + add

    return await _finalize_custom_shares(update, context, totals)


# ---------------------------------------------------------------- payer/confirm
async def change_payer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    db = get_db(context)
    active_users = db.list_active_users()
    await query.edit_message_text("چه کسی هزینه رو پرداخت کرد؟", reply_markup=kb.payer_choice_keyboard(active_users))
    return EXP_CHANGE_PAYER


async def payer_chosen_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    if query.data != "payer_cancel_change":
        payer_id = int(query.data.split("_", 1)[1])
        context.user_data["exp_payer_id"] = payer_id
    return await _show_preview(update, context)


async def _announce_expense(context, db, expense_id: int):
    expense = db.get_expense(expense_id)
    payer = db.get_user_by_id(expense["payer_id"])
    mode_label = MODE_LABELS.get(expense["split_mode"], expense["split_mode"])

    lines = [
        f"🔔 <b>هزینه جدید ثبت شد</b> (#{expense_id})",
        f"📌 بابت: {expense['description']}",
        f"📅 تاریخ: {ju.jalali_pretty(expense['jalali_date'])}",
        f"💰 مبلغ کل: {ju.format_money(expense['amount'])}",
        f"👤 پرداخت‌کننده: {payer['first_name']}",
        f"🔀 نحوه تقسیم: {mode_label}",
        "",
        "سهم هرکس:",
    ]
    for p in sorted(expense["participants"], key=lambda x: -x["share_amount"]):
        tag = "(خودش پرداخت کرد)" if p["user_id"] == payer["id"] else "→ بدهکار"
        headcount = f" ({p['weight_used']} نفر)" if expense["split_mode"] == "weighted" else ""
        lines.append(f"  • {p['first_name']}{headcount}: {ju.format_money(p['share_amount'])} {tag}")

    if payer["card_number"]:
        lines.append("")
        lines.append(f"💳 برای واریز به {payer['first_name']}: {ju.format_card_number_html(payer['card_number'])}")

    pairs, _ = group_report(db)
    lines.append("")
    lines.append("📊 <b>وضعیت به‌روز بدهی‌ها (بعد از تهاتر)</b>:")
    if not pairs:
        lines.append("  همه چی صافه، کسی به کسی بدهکار نیست ✅")
    else:
        for debtor, creditor, amt, program_name in pairs:
            tag = f" (بابت برنامه {program_name})" if program_name else ""
            lines.append(f"  • {debtor['first_name']} به {creditor['first_name']}: {ju.format_money(amt)}{tag}")

    text = "\n".join(lines)

    group_chat_id = db.get_setting(config.GROUP_CHAT_ID_SETTING_KEY)
    if group_chat_id:
        try:
            await context.bot.send_message(chat_id=int(group_chat_id), text=text, parse_mode="HTML")
        except Exception:
            pass
        return text, True
    return text, False


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    db = get_db(context)

    if query.data == "expense_cancel":
        await query.edit_message_text("لغو شد. ❌")
        context.user_data.clear()
        return ConversationHandler.END

    program_id = context.user_data.get("exp_program_id")
    approval_status = "pending" if program_id else "approved"

    expense_id = db.create_expense(
        payer_id=context.user_data["exp_payer_id"],
        creator_id=db.get_user_by_telegram_id(update.effective_user.id)["id"],
        amount=context.user_data["exp_amount"],
        description=context.user_data["exp_description"],
        split_mode=context.user_data["exp_split_mode"],
        shares=context.user_data["exp_shares"],
        weights_used=context.user_data["exp_weights"],
        program_id=program_id,
        approval_status=approval_status,
    )

    # If they got here via OCR, the receipt photo they already sent for
    # extraction IS the receipt -- attach it automatically, no need to ask again.
    ocr_photo_id = context.user_data.get("exp_ocr_photo_file_id")
    if ocr_photo_id:
        db.set_expense_receipt(expense_id, file_id=ocr_photo_id)

    if program_id:
        # program expenses don't affect balances or get announced until a
        # group admin approves them -- notify the admin(s) instead.
        program = db.get_program(program_id)
        await query.edit_message_text(
            f"✅ ثبت شد و برای تایید مدیر گروه فرستاده شد.\n(بابت برنامه‌ی «{program['name']}»)"
        )
        expense = db.get_expense(expense_id)
        submitter = db.get_user_by_id(expense["creator_id"])
        approval_text = (
            f"⏳ <b>هزینه‌ی جدید منتظر تایید</b> (برنامه: {program['name']})\n"
            f"📌 بابت: {expense['description']}\n"
            f"💰 مبلغ: {ju.format_money(expense['amount'])}\n"
            f"👤 ثبت‌کننده: {submitter['first_name']}"
        )
        for admin in db.list_admins():
            try:
                await context.bot.send_message(
                    admin["telegram_id"], approval_text, parse_mode="HTML",
                    reply_markup=kb.expense_approval_keyboard(expense_id),
                )
            except Exception:
                pass
    else:
        text, sent_to_group = await _announce_expense(context, db, expense_id)
        if sent_to_group:
            await query.edit_message_text("✅ ثبت شد و خلاصه‌اش به گروه فرستاده شد.")
        else:
            await query.edit_message_text(
                text + "\n\n<i>راهنمایی: اگه دستور /setgroup رو داخل گروه تلگرامی‌تون بزنید، این خلاصه خودکار برای همه فرستاده میشه.</i>",
                parse_mode="HTML",
            )

    user = db.get_user_by_telegram_id(update.effective_user.id)

    if ocr_photo_id:
        context.user_data.clear()
        await send_main_menu(update, user, "چیز دیگه‌ای هست؟")
        return ConversationHandler.END

    context.user_data["exp_id_for_receipt"] = expense_id
    await query.message.reply_text(
        "می‌خوای رسید یا فاکتور این هزینه رو هم ضمیمه کنی؟ 🧾 (عکس بفرست یا یه توضیح متنی بنویس)",
        reply_markup=kb.receipt_prompt_keyboard(),
    )
    return EXP_RECEIPT


# ------------------------------------------------------------------- receipt
async def receipt_skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("باشه، بدون رسید ثبت موند. ✅")
    db = get_db(context)
    user = db.get_user_by_telegram_id(update.effective_user.id)
    context.user_data.clear()
    await send_main_menu(update, user)
    return ConversationHandler.END


async def receipt_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db = get_db(context)
    expense_id = context.user_data["exp_id_for_receipt"]
    file_id = update.message.photo[-1].file_id
    db.set_expense_receipt(expense_id, file_id=file_id)
    await update.message.reply_text("رسید ضمیمه شد. ✅")
    user = db.get_user_by_telegram_id(update.effective_user.id)
    context.user_data.clear()
    await send_main_menu(update, user)
    return ConversationHandler.END


async def receipt_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db = get_db(context)
    expense_id = context.user_data["exp_id_for_receipt"]
    db.set_expense_receipt(expense_id, text=update.message.text.strip()[:1000])
    await update.message.reply_text("توضیح رسید ثبت شد. ✅")
    user = db.get_user_by_telegram_id(update.effective_user.id)
    context.user_data.clear()
    await send_main_menu(update, user)
    return ConversationHandler.END


expense_conv = ConversationHandler(
    entry_points=[
        CommandHandler("expense", expense_entry),
        MessageHandler(filters.Regex(f"^{re.escape(kb.BTN_NEW_EXPENSE)}$"), expense_entry),
    ],
    states={
        EXP_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, description_received)],
        EXP_PROGRAM_CHOICE: [CallbackQueryHandler(program_choice_callback, pattern=r"^expprog_")],
        EXP_PARTICIPANTS: [CallbackQueryHandler(participants_callback, pattern=r"^(toggle_|participants_done$)")],
        EXP_SPLIT_MODE: [CallbackQueryHandler(split_mode_callback, pattern=r"^split_(weighted|equal|custom|ocr)$")],
        EXP_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_received)],
        EXP_WEIGHTS: [CallbackQueryHandler(weights_callback, pattern=r"^(winc_|wdec_|wnoop_|weights_done$)")],
        EXP_CUSTOM_AMOUNTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_amount_received)],
        EXP_OCR_PHOTO: [MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), ocr_photo_received)],
        EXP_OCR_ITEM: [CallbackQueryHandler(ocr_item_callback, pattern=r"^(itemtag_|item_confirm$|item_none$)")],
        EXP_OCR_REMAINDER: [CallbackQueryHandler(ocr_remainder_callback, pattern=r"^remainder_(split|skip)$")],
        EXP_CONFIRM: [
            CallbackQueryHandler(change_payer_callback, pattern=r"^expense_change_payer$"),
            CallbackQueryHandler(confirm_callback, pattern=r"^expense_(confirm|cancel)$"),
        ],
        EXP_CHANGE_PAYER: [CallbackQueryHandler(payer_chosen_callback, pattern=r"^payer_")],
        EXP_RECEIPT: [
            CallbackQueryHandler(receipt_skip_callback, pattern=r"^receipt_skip$"),
            MessageHandler(filters.PHOTO, receipt_photo_received),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receipt_text_received),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_conversation),
        MessageHandler(filters.COMMAND, cancel_conversation),
    ],
    name="expense_conv",
    persistent=False,
)
