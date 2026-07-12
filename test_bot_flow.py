# -*- coding: utf-8 -*-
"""
Drives the ACTUAL handler coroutines (registration, expenses, payments, reports,
members) through realistic multi-user scenarios, using lightweight fake
Update/Context objects instead of a real Telegram connection (which this
sandbox can't reach anyway). This exercises real business logic, real DB
writes, and real string formatting code -- not just "does it compile".
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from database import Database
from handlers import registration, expenses, payments, reports, members
from split_engine import calculate_shares
import veryfi_client

DB_PATH = "/tmp/test_bot_flow.db"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
db = Database(DB_PATH)

failures = []


def check(name, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


def _make_sent_message():
    """What awaiting message.reply_text(...) 'returns' -- needs its own
    awaitable edit_text so code like `msg = await m.reply_text(...); await
    msg.edit_text(...)` (used in the OCR status-message flow) works."""
    sent = SimpleNamespace()
    sent.edit_text = AsyncMock()
    return sent


class FakeMessage:
    def __init__(self, text=None, photo=None):
        self.text = text
        self.photo = photo  # list of fake PhotoSize-like objects, or None
        self.chat_id = 555
        self.reply_text = AsyncMock(return_value=_make_sent_message())
        self.reply_html = AsyncMock()
        self.reply_document = AsyncMock()
        self.reply_photo = AsyncMock()


def make_photo_message(file_id="FAKE_RECEIPT_PHOTO_ID"):
    return FakeMessage(photo=[SimpleNamespace(file_id=file_id)])


class FakeCallbackQuery:
    def __init__(self, data, from_user):
        self.data = data
        self.from_user = from_user
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()
        self.edit_message_reply_markup = AsyncMock()
        self.message = FakeMessage()


class FakeUpdate:
    def __init__(self, user, message=None, callback_query=None, chat=None):
        self.effective_user = user
        self.message = message
        self.callback_query = callback_query
        self.effective_message = message if message else (callback_query.message if callback_query else None)
        self.effective_chat = chat or SimpleNamespace(id=user.id * -1, type="private")


def make_user(telegram_id, username, first_name):
    return SimpleNamespace(id=telegram_id, username=username, first_name=first_name)


def make_context():
    fake_bot = AsyncMock()
    fake_bot.get_file = AsyncMock(return_value=SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"fake-image-bytes"))))
    return SimpleNamespace(bot_data={"db": db}, user_data={}, bot=fake_bot)


async def main():
    ctx = make_context()

    # ================= USER A: Ali -- first ever user, should bootstrap as admin
    ali_tg = make_user(1001, "ali_tg", "علی")
    upd = FakeUpdate(ali_tg, message=FakeMessage("/start"))
    state = await registration.start_command(upd, ctx)
    check("Ali /start -> asks for card (REG_CARD state)", state == registration.REG_CARD)

    upd = FakeUpdate(ali_tg, message=FakeMessage("6037-9911-1111-1111"))
    state = await registration.reg_card_received(upd, ctx)
    check("Ali card accepted -> REG_WEIGHT state", state == registration.REG_WEIGHT)

    upd = FakeUpdate(ali_tg, message=FakeMessage("3"))
    state = await registration.reg_weight_received(upd, ctx)
    ali_row = db.get_user_by_telegram_id(1001)
    check("Ali weight accepted, conversation ends", state == -1)  # ConversationHandler.END == -1
    check("Ali bootstrapped as admin+active", ali_row["is_admin"] == 1 and ali_row["status"] == "active")
    ali_id = ali_row["id"]

    # ================= USER B: Reza -- second user, should be pending + admin notified
    reza_tg = make_user(1002, "reza_tg", "رضا")
    ctx_b = make_context()
    upd = FakeUpdate(reza_tg, message=FakeMessage("/start"))
    await registration.start_command(upd, ctx_b)
    upd = FakeUpdate(reza_tg, message=FakeMessage("6037-9922-2222-2222"))
    await registration.reg_card_received(upd, ctx_b)
    upd = FakeUpdate(reza_tg, message=FakeMessage("1"))
    await registration.reg_weight_received(upd, ctx_b)
    reza_row = db.get_user_by_telegram_id(1002)
    check("Reza stays pending (not bootstrap)", reza_row["status"] == "pending")
    check("Admin (Ali) was notified via bot.send_message", ctx_b.bot.send_message.await_count >= 1)
    reza_id = reza_row["id"]

    approve_cb = FakeCallbackQuery(f"reg_approve_{reza_id}", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=approve_cb)
    await registration.approval_callback(upd, ctx)
    check("Reza approved -> active", db.get_user_by_telegram_id(1002)["status"] == "active")

    # ================= USER C: Mohammad -- third user, approved same way
    mohammad_tg = make_user(1003, "mohammad_tg", "محمد")
    ctx_c = make_context()
    upd = FakeUpdate(mohammad_tg, message=FakeMessage("/start"))
    await registration.start_command(upd, ctx_c)
    upd = FakeUpdate(mohammad_tg, message=FakeMessage("6037993333333333"))
    await registration.reg_card_received(upd, ctx_c)
    upd = FakeUpdate(mohammad_tg, message=FakeMessage("1"))
    await registration.reg_weight_received(upd, ctx_c)
    mohammad_id = db.get_user_by_telegram_id(1003)["id"]
    approve_cb2 = FakeCallbackQuery(f"reg_approve_{mohammad_id}", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=approve_cb2)
    await registration.approval_callback(upd, ctx)
    check("3 active users now", len(db.list_active_users()) == 3)

    # ============================================================
    # EXPENSE FLOW 1: weighted mode, new order (desc -> participants
    # -> mode -> amount -> per-expense headcount bump), then SKIP receipt.
    # ============================================================
    ctx_exp = make_context()
    upd = FakeUpdate(ali_tg, message=FakeMessage("/expense"))
    state = await expenses.expense_entry(upd, ctx_exp)
    check("expense_entry -> EXP_DESC (description asked first now)", state == expenses.EXP_DESC)

    upd = FakeUpdate(ali_tg, message=FakeMessage("شام رستوران"))
    state = await expenses.description_received(upd, ctx_exp)
    check("description accepted -> EXP_PARTICIPANTS (all pre-selected)", state == expenses.EXP_PARTICIPANTS)
    check("all 3 active users pre-selected by default", ctx_exp.user_data["exp_selected"] == {ali_id, reza_id, mohammad_id})

    done_cb = FakeCallbackQuery("participants_done", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=done_cb)
    state = await expenses.participants_callback(upd, ctx_exp)
    check("participants confirmed -> EXP_SPLIT_MODE", state == expenses.EXP_SPLIT_MODE)

    split_cb = FakeCallbackQuery("split_weighted", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=split_cb)
    state = await expenses.split_mode_callback(upd, ctx_exp)
    check("weighted mode chosen -> EXP_AMOUNT (amount now asked AFTER mode)", state == expenses.EXP_AMOUNT)

    upd = FakeUpdate(ali_tg, message=FakeMessage("400000"))
    state = await expenses.amount_received(upd, ctx_exp)
    check("amount accepted -> EXP_WEIGHTS (headcount adjustment screen)", state == expenses.EXP_WEIGHTS)
    check("default headcounts pre-filled from profiles (ali=3, reza=1, mohammad=1)",
          ctx_exp.user_data["exp_weights"] == {ali_id: 3, reza_id: 1, mohammad_id: 1})

    # bump Mohammad's headcount up by 1 for this specific expense (1 -> 2), like "Amir counts as 4 today"
    inc_cb = FakeCallbackQuery(f"winc_{mohammad_id}", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=inc_cb)
    state = await expenses.weights_callback(upd, ctx_exp)
    check("winc bumps Mohammad to 2, stays in EXP_WEIGHTS", state == expenses.EXP_WEIGHTS and ctx_exp.user_data["exp_weights"][mohammad_id] == 2)
    check("Mohammad's PROFILE weight is untouched by the per-expense bump", db.get_user_by_id(mohammad_id)["weight"] == 1)

    done_weights_cb = FakeCallbackQuery("weights_done", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=done_weights_cb)
    state = await expenses.weights_callback(upd, ctx_exp)
    check("weights confirmed -> EXP_CONFIRM", state == expenses.EXP_CONFIRM)
    expected_shares = calculate_shares(400_000, [{"user_id": ali_id, "weight": 3}, {"user_id": reza_id, "weight": 1}, {"user_id": mohammad_id, "weight": 2}], "weighted")
    check("computed shares reflect the per-expense headcount bump, not the profile default",
          ctx_exp.user_data["exp_shares"] == expected_shares)

    confirm_cb = FakeCallbackQuery("expense_confirm", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=confirm_cb)
    state = await expenses.confirm_callback(upd, ctx_exp)
    check("expense confirmed -> EXP_RECEIPT (optional receipt prompt)", state == expenses.EXP_RECEIPT)
    check("exactly 1 expense saved in db", db.count_expenses() == 1)
    saved = db.list_expenses(limit=1)[0]
    check("saved expense has correct amount & description", saved["amount"] == 400_000 and saved["description"] == "شام رستوران")
    mohammad_participant = next(p for p in saved["participants"] if p["user_id"] == mohammad_id)
    check("saved expense_participants row stores the bumped weight_used (2), not profile weight (1)",
          mohammad_participant["weight_used"] == 2)

    # skip the receipt this time
    skip_receipt_cb = FakeCallbackQuery("receipt_skip", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=skip_receipt_cb)
    state = await expenses.receipt_skip_callback(upd, ctx_exp)
    check("receipt skipped, conversation ends", state == -1)
    check("no receipt was stored", db.get_expense(saved["id"])["receipt_file_id"] is None and db.get_expense(saved["id"])["receipt_text"] is None)

    # ============================================================
    # EXPENSE FLOW 2: CUSTOM per-person amounts (the user's own example:
    # a hiking trip where Mehrab/Saleh/etc. each cost something different)
    # -- and this time attach a receipt as TEXT.
    # ============================================================
    ctx_exp2 = make_context()
    upd = FakeUpdate(mohammad_tg, message=FakeMessage("/expense"))
    await expenses.expense_entry(upd, ctx_exp2)
    upd = FakeUpdate(mohammad_tg, message=FakeMessage("برنامه کوه"))
    state = await expenses.description_received(upd, ctx_exp2)
    check("expense 2 description -> EXP_PARTICIPANTS", state == expenses.EXP_PARTICIPANTS)

    done_cb2 = FakeCallbackQuery("participants_done", mohammad_tg)
    upd = FakeUpdate(mohammad_tg, callback_query=done_cb2)
    state = await expenses.participants_callback(upd, ctx_exp2)
    check("expense 2 participants confirmed -> EXP_SPLIT_MODE", state == expenses.EXP_SPLIT_MODE)

    # trying OCR while Veryfi isn't configured should be rejected and stay put
    ocr_attempt_cb = FakeCallbackQuery("split_ocr", mohammad_tg)
    upd = FakeUpdate(mohammad_tg, callback_query=ocr_attempt_cb)
    state = await expenses.split_mode_callback(upd, ctx_exp2)
    check("OCR option blocked when Veryfi isn't configured, stays in EXP_SPLIT_MODE", state == expenses.EXP_SPLIT_MODE)

    custom_cb = FakeCallbackQuery("split_custom", mohammad_tg)
    upd = FakeUpdate(mohammad_tg, callback_query=custom_cb)
    state = await expenses.split_mode_callback(upd, ctx_exp2)
    check("custom mode -> EXP_CUSTOM_AMOUNTS directly (no upfront total asked)", state == expenses.EXP_CUSTOM_AMOUNTS)
    queue = ctx_exp2.user_data["exp_custom_queue"]
    check("custom queue contains exactly the 3 selected participants", set(queue) == {ali_id, reza_id, mohammad_id})

    custom_values = {queue[0]: 150_000, queue[1]: 100_000, queue[2]: 200_000}
    for uid in queue:
        upd = FakeUpdate(mohammad_tg, message=FakeMessage(str(custom_values[uid])))
        state = await expenses.custom_amount_received(upd, ctx_exp2)

    check("after all custom amounts entered -> EXP_CONFIRM", state == expenses.EXP_CONFIRM)
    check("custom total = sum of individual entries (450,000)", ctx_exp2.user_data["exp_amount"] == 450_000)
    check("custom shares match exactly what was typed per person", ctx_exp2.user_data["exp_shares"] == custom_values)

    confirm_cb2 = FakeCallbackQuery("expense_confirm", mohammad_tg)
    upd = FakeUpdate(mohammad_tg, callback_query=confirm_cb2)
    state = await expenses.confirm_callback(upd, ctx_exp2)
    check("custom expense confirmed -> EXP_RECEIPT", state == expenses.EXP_RECEIPT)
    check("2 expenses now saved total", db.count_expenses() == 2)
    custom_expense_id = ctx_exp2.user_data["exp_id_for_receipt"]

    upd = FakeUpdate(mohammad_tg, message=FakeMessage("فیش واریز، شماره پیگیری 998877"))
    state = await expenses.receipt_text_received(upd, ctx_exp2)
    check("text receipt attached, conversation ends", state == -1)
    check("receipt_text actually saved on the expense", db.get_expense(custom_expense_id)["receipt_text"] == "فیش واریز، شماره پیگیری 998877")

    # ============================================================
    # EXPENSE FLOW 3: Veryfi OCR item-tagging (mocked -- no real API access),
    # including a tax/service remainder that gets split equally, and the
    # receipt photo auto-attaching itself (no separate prompt needed).
    # ============================================================
    import config as cfg
    cfg.VERYFI_ENABLED = True  # simulate credentials having been configured
    try:
        ctx_exp3 = make_context()
        upd = FakeUpdate(ali_tg, message=FakeMessage("/expense"))
        await expenses.expense_entry(upd, ctx_exp3)
        upd = FakeUpdate(ali_tg, message=FakeMessage("ناهار دورهمی"))
        await expenses.description_received(upd, ctx_exp3)
        done_cb3 = FakeCallbackQuery("participants_done", ali_tg)
        upd = FakeUpdate(ali_tg, callback_query=done_cb3)
        state = await expenses.participants_callback(upd, ctx_exp3)

        split_mode_kb_state_ok = state == expenses.EXP_SPLIT_MODE
        ocr_cb = FakeCallbackQuery("split_ocr", ali_tg)
        upd = FakeUpdate(ali_tg, callback_query=ocr_cb)
        state = await expenses.split_mode_callback(upd, ctx_exp3)
        check("OCR mode accepted once Veryfi is 'configured' -> EXP_OCR_PHOTO", split_mode_kb_state_ok and state == expenses.EXP_OCR_PHOTO)

        fake_extraction = veryfi_client.ReceiptExtraction(
            line_items=[
                veryfi_client.ReceiptLineItem(description="کباب کوبیده", amount=200_000),
                veryfi_client.ReceiptLineItem(description="نوشیدنی مشترک", amount=60_000),
            ],
            document_total=290_000,  # 30,000 more than the two items -> tax/service remainder
            vendor_name="رستوران تست",
        )
        with patch("veryfi_client.extract_receipt", new=AsyncMock(return_value=fake_extraction)):
            upd = FakeUpdate(ali_tg, message=make_photo_message("RECEIPT_FILE_XYZ"))
            state = await expenses.ocr_photo_received(upd, ctx_exp3)
        check("photo processed via (mocked) Veryfi -> EXP_OCR_ITEM, first item shown", state == expenses.EXP_OCR_ITEM)
        check("2 line items stored", len(ctx_exp3.user_data["exp_ocr_items"]) == 2)

        # Item 1 (کباب کوبیده, 200,000) -> tag to Ali only
        tag_cb = FakeCallbackQuery(f"itemtag_{ali_id}", ali_tg)
        upd = FakeUpdate(ali_tg, callback_query=tag_cb)
        await expenses.ocr_item_callback(upd, ctx_exp3)
        confirm_item_cb = FakeCallbackQuery("item_confirm", ali_tg)
        upd = FakeUpdate(ali_tg, callback_query=confirm_item_cb)
        state = await expenses.ocr_item_callback(upd, ctx_exp3)
        check("item 1 confirmed -> moves to item 2 (EXP_OCR_ITEM)", state == expenses.EXP_OCR_ITEM)
        check("item 1 fully assigned to Ali (200,000)", ctx_exp3.user_data["exp_ocr_totals"].get(ali_id) == 200_000)

        # Item 2 (shared drink, 60,000) -> tag to Reza AND Mohammad (split between them)
        tag_cb2a = FakeCallbackQuery(f"itemtag_{reza_id}", ali_tg)
        upd = FakeUpdate(ali_tg, callback_query=tag_cb2a)
        await expenses.ocr_item_callback(upd, ctx_exp3)
        tag_cb2b = FakeCallbackQuery(f"itemtag_{mohammad_id}", ali_tg)
        upd = FakeUpdate(ali_tg, callback_query=tag_cb2b)
        await expenses.ocr_item_callback(upd, ctx_exp3)
        confirm_item_cb2 = FakeCallbackQuery("item_confirm", ali_tg)
        upd = FakeUpdate(ali_tg, callback_query=confirm_item_cb2)
        state = await expenses.ocr_item_callback(upd, ctx_exp3)
        check("all items done -> tax/service remainder detected (EXP_OCR_REMAINDER)", state == expenses.EXP_OCR_REMAINDER)
        check("shared drink split 30,000/30,000 between Reza and Mohammad",
              ctx_exp3.user_data["exp_ocr_totals"].get(reza_id) == 30_000 and ctx_exp3.user_data["exp_ocr_totals"].get(mohammad_id) == 30_000)
        check("remainder computed correctly (290,000 - 260,000 = 30,000)", ctx_exp3.user_data["exp_ocr_remainder"] == 30_000)

        remainder_cb = FakeCallbackQuery("remainder_split", ali_tg)
        upd = FakeUpdate(ali_tg, callback_query=remainder_cb)
        state = await expenses.ocr_remainder_callback(upd, ctx_exp3)
        check("remainder split -> EXP_CONFIRM", state == expenses.EXP_CONFIRM)
        # 30,000 remainder split 3 ways = 10,000 each -> final: ali=210,000 reza=40,000 mohammad=40,000
        check("final OCR-derived shares include the split remainder",
              ctx_exp3.user_data["exp_shares"] == {ali_id: 210_000, reza_id: 40_000, mohammad_id: 40_000})
        check("final OCR total equals the receipt's document total (290,000)", ctx_exp3.user_data["exp_amount"] == 290_000)

        confirm_cb3 = FakeCallbackQuery("expense_confirm", ali_tg)
        upd = FakeUpdate(ali_tg, callback_query=confirm_cb3)
        state = await expenses.confirm_callback(upd, ctx_exp3)
        check("OCR expense confirmed and auto-ends (receipt already attached, no extra prompt)", state == -1)
        check("3 expenses now saved total", db.count_expenses() == 3)
        ocr_saved = db.list_expenses(limit=1)[0]
        check("OCR expense's receipt photo was auto-attached from the upload", ocr_saved["receipt_file_id"] == "RECEIPT_FILE_XYZ")
        check("OCR expense stored with split_mode='custom'", ocr_saved["split_mode"] == "custom")
    finally:
        cfg.VERYFI_ENABLED = False

    # ================= PAYMENT FLOW: Reza pays Ali back his exact share (66,666), Ali confirms receipt
    # (Ali's total across expense 1 + expense 3 owed-to-him from Reza: 66,666 from expense 1 only,
    #  since Reza wasn't charged anything in expense 3 beyond the shared drink which nets against Ali separately;
    #  we just pay back expense 1's share here to keep this scenario focused and legible.)
    ctx_pay = make_context()
    upd = FakeUpdate(reza_tg, message=FakeMessage("/payment"))
    state = await payments.payment_entry(upd, ctx_pay)
    check("payment_entry -> PAY_TARGET", state == payments.PAY_TARGET)

    target_cb = FakeCallbackQuery(f"paytarget_{ali_id}", reza_tg)
    upd = FakeUpdate(reza_tg, callback_query=target_cb)
    state = await payments.target_chosen_callback(upd, ctx_pay)
    check("target chosen -> PAY_AMOUNT", state == payments.PAY_AMOUNT)

    upd = FakeUpdate(reza_tg, message=FakeMessage("66666"))
    state = await payments.pay_amount_received(upd, ctx_pay)
    check("amount accepted -> PAY_NOTE", state == payments.PAY_NOTE)

    skip_cb = FakeCallbackQuery("note_skip", reza_tg)
    upd = FakeUpdate(reza_tg, callback_query=skip_cb)
    state = await payments.note_skip_callback(upd, ctx_pay)
    check("note skipped -> PAY_CONFIRM", state == payments.PAY_CONFIRM)

    pay_confirm_cb = FakeCallbackQuery("payment_confirm", reza_tg)
    upd = FakeUpdate(reza_tg, callback_query=pay_confirm_cb)
    state = await payments.payment_confirm_callback(upd, ctx_pay)
    check("payment recorded -> PAY_RECEIPT (optional receipt prompt)", state == payments.PAY_RECEIPT)
    pending_payments = db.list_pending_payments_for_user(ali_id)
    check("payment is pending confirmation from Ali", len(pending_payments) == 1 and pending_payments[0]["amount"] == 66_666)

    # attach a receipt photo for the payment this time
    payment_id = pending_payments[0]["id"]
    upd = FakeUpdate(reza_tg, message=make_photo_message("PAYMENT_RECEIPT_PHOTO_ID"))
    state = await payments.receipt_attach_photo_received(upd, ctx_pay)
    check("payment receipt photo attached, conversation ends", state == -1)
    check("payment's receipt_file_id saved", db.get_payment(payment_id)["receipt_file_id"] == "PAYMENT_RECEIPT_PHOTO_ID")

    # Ali confirms receipt of the money
    recv_cb = FakeCallbackQuery(f"payrecv_confirm_{payment_id}", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=recv_cb)
    await payments.receipt_callback(upd, ctx)
    check("payment status is now confirmed", db.get_payment(payment_id)["status"] == "confirmed")

    # ================= REPORTS: exercise balance/history/group-report/export code paths =================
    upd = FakeUpdate(mohammad_tg, message=FakeMessage("/balance"))
    ctx_rep = make_context()
    await reports.balance_command(upd, ctx_rep)
    check("balance_command replied without raising", upd.effective_message.reply_text.await_count == 1)

    upd = FakeUpdate(ali_tg, message=FakeMessage("/history"))
    await reports.history_command(upd, ctx_rep)
    check("history_command replied without raising", upd.effective_message.reply_text.await_count == 1)

    upd = FakeUpdate(ali_tg, message=FakeMessage("/report"))
    await reports.group_report_command(upd, ctx_rep)
    check("group_report_command replied without raising", upd.effective_message.reply_text.await_count == 1)

    upd = FakeUpdate(ali_tg, message=FakeMessage("/export"))
    await reports.export_command(upd, ctx_rep)
    check("export_command replied with a document without raising", upd.effective_message.reply_document.await_count == 1)

    # view the OCR expense's auto-attached receipt via history
    view_cb = FakeCallbackQuery(f"viewreceipt_{ocr_saved['id']}", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=view_cb)
    await reports.view_receipt_callback(upd, ctx_rep)
    check("view_receipt sends the photo back via send_photo", ctx_rep.bot.send_photo.await_count == 1)

    # ================= CARD NUMBER BIDI/COPY FIX =================
    import jalali_utils as ju
    html = ju.format_card_number_html("6037991111111111")
    check("card number HTML wrapped in <code> for tap-to-copy", html.startswith("<code>") and html.endswith("</code>"))
    check("card number wrapped in bidi isolate marks (U+2066/U+2069) to prevent group reversal",
          "\u2066" in html and "\u2069" in html)

    # ================= MEMBER MANAGEMENT: change Reza's weight via admin flow =================
    ctx_mem = make_context()
    upd = FakeUpdate(ali_tg, message=FakeMessage("/members"))
    state = await members.members_entry(upd, ctx_mem)
    check("members_entry -> MEM_MENU", state == members.MEM_MENU)

    view_member_cb = FakeCallbackQuery(f"member_{reza_id}", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=view_member_cb)
    state = await members.members_menu_callback(upd, ctx_mem)
    check("member detail view -> stays MEM_MENU", state == members.MEM_MENU)

    edit_cb = FakeCallbackQuery(f"memedit_weight_{reza_id}", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=edit_cb)
    state = await members.members_menu_callback(upd, ctx_mem)
    check("edit weight prompt -> MEM_AWAIT_WEIGHT", state == members.MEM_AWAIT_WEIGHT)

    upd = FakeUpdate(ali_tg, message=FakeMessage("2"))
    state = await members.weight_received(upd, ctx_mem)
    check("weight updated -> back to MEM_MENU", state == members.MEM_MENU)
    check("Reza's weight actually changed to 2 in DB", db.get_user_by_id(reza_id)["weight"] == 2)

    print()
    if failures:
        print(f"*** {len(failures)} TEST(S) FAILED: {failures}")
        raise SystemExit(1)
    else:
        print("ALL BOT-FLOW TESTS PASSED")


asyncio.run(main())
os.remove(DB_PATH)
