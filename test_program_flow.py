# -*- coding: utf-8 -*-
"""
Drives the ACTUAL program-related handler coroutines (creation, program-linked
expense entry + admin approval, charging + admin confirmation, status view,
closing) through a realistic scenario, using the same lightweight fake
Update/Context objects as test_bot_flow.py.
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from database import Database
from handlers import registration, expenses, payments, programs
import balances

DB_PATH = "/tmp/test_program_flow.db"
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
    sent = SimpleNamespace()
    sent.edit_text = AsyncMock()
    return sent


class FakeMessage:
    def __init__(self, text=None, photo=None):
        self.text = text
        self.photo = photo
        self.chat_id = 555
        self.reply_text = AsyncMock(return_value=_make_sent_message())
        self.reply_html = AsyncMock()
        self.reply_document = AsyncMock()


class FakeCallbackQuery:
    def __init__(self, data, from_user):
        self.data = data
        self.from_user = from_user
        self.answer = AsyncMock()
        self.edit_message_text = AsyncMock()
        self.edit_message_reply_markup = AsyncMock()
        self.message = FakeMessage()


class FakeUpdate:
    def __init__(self, user, message=None, callback_query=None):
        self.effective_user = user
        self.message = message
        self.callback_query = callback_query
        self.effective_message = message if message else (callback_query.message if callback_query else None)
        self.effective_chat = SimpleNamespace(id=user.id * -1, type="private")


def make_user(telegram_id, username, first_name):
    return SimpleNamespace(id=telegram_id, username=username, first_name=first_name)


def make_context():
    return SimpleNamespace(bot_data={"db": db}, user_data={}, bot=AsyncMock())


async def main():
    ctx = make_context()

    # ---- set up 3 users: Ali (admin), Reza, Mohammad ----
    ali_tg = make_user(2001, "ali", "علی")
    upd = FakeUpdate(ali_tg, message=FakeMessage("/start"))
    await registration.start_command(upd, ctx)
    upd = FakeUpdate(ali_tg, message=FakeMessage("6037991111111111"))
    await registration.reg_card_received(upd, ctx)
    upd = FakeUpdate(ali_tg, message=FakeMessage("2"))
    await registration.reg_weight_received(upd, ctx)
    ali_id = db.get_user_by_telegram_id(2001)["id"]
    check("Ali bootstrapped as admin", db.get_user_by_id(ali_id)["is_admin"] == 1)

    reza_tg = make_user(2002, "reza", "رضا")
    ctx_r = make_context()
    upd = FakeUpdate(reza_tg, message=FakeMessage("/start"))
    await registration.start_command(upd, ctx_r)
    upd = FakeUpdate(reza_tg, message=FakeMessage("6037992222222222"))
    await registration.reg_card_received(upd, ctx_r)
    upd = FakeUpdate(reza_tg, message=FakeMessage("1"))
    await registration.reg_weight_received(upd, ctx_r)
    reza_id = db.get_user_by_telegram_id(2002)["id"]
    approve_cb = FakeCallbackQuery(f"reg_approve_{reza_id}", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=approve_cb)
    await registration.approval_callback(upd, ctx)

    mohammad_tg = make_user(2003, "mohammad", "محمد")
    ctx_m = make_context()
    upd = FakeUpdate(mohammad_tg, message=FakeMessage("/start"))
    await registration.start_command(upd, ctx_m)
    upd = FakeUpdate(mohammad_tg, message=FakeMessage("6037993333333333"))
    await registration.reg_card_received(upd, ctx_m)
    upd = FakeUpdate(mohammad_tg, message=FakeMessage("1"))
    await registration.reg_weight_received(upd, ctx_m)
    mohammad_id = db.get_user_by_telegram_id(2003)["id"]
    approve_cb2 = FakeCallbackQuery(f"reg_approve_{mohammad_id}", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=approve_cb2)
    await registration.approval_callback(upd, ctx)
    check("3 active users", len(db.list_active_users()) == 3)

    # ============================================================
    # PROGRAM CREATION: Ali creates "سفر شمال" with all 3 participants
    # ============================================================
    ctx_prog = make_context()
    upd = FakeUpdate(ali_tg, message=FakeMessage("/programs"))
    state = await programs.programs_entry(upd, ctx_prog)
    check("programs_entry -> PROG_MENU", state == programs.PROG_MENU)

    new_cb = FakeCallbackQuery("prog_new", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=new_cb)
    state = await programs.program_menu_callback(upd, ctx_prog)
    check("'prog_new' -> PROG_NAME (admin only)", state == programs.PROG_NAME)

    upd = FakeUpdate(ali_tg, message=FakeMessage("سفر شمال"))
    state = await programs.prog_name_received(upd, ctx_prog)
    check("name accepted -> PROG_PARTICIPANTS", state == programs.PROG_PARTICIPANTS)

    for uid in (ali_id, reza_id, mohammad_id):
        toggle_cb = FakeCallbackQuery(f"progtoggle_{uid}", ali_tg)
        upd = FakeUpdate(ali_tg, callback_query=toggle_cb)
        await programs.prog_participants_callback(upd, ctx_prog)
    check("all 3 selected as program participants", ctx_prog.user_data["prog_selected"] == {ali_id, reza_id, mohammad_id})

    done_cb = FakeCallbackQuery("progparticipants_done", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=done_cb)
    state = await programs.prog_participants_callback(upd, ctx_prog)
    check("participants confirmed -> PROG_WEIGHTS", state == programs.PROG_WEIGHTS)
    check("default weights pulled from profiles (ali=2, reza=1, mohammad=1)",
          ctx_prog.user_data["prog_weights"] == {ali_id: 2, reza_id: 1, mohammad_id: 1})

    weights_done_cb = FakeCallbackQuery("pweights_done", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=weights_done_cb)
    state = await programs.prog_weights_callback(upd, ctx_prog)
    check("weights confirmed -> PROG_CARD", state == programs.PROG_CARD)

    upd = FakeUpdate(ali_tg, message=FakeMessage("6037990000000000"))
    state = await programs.prog_card_received(upd, ctx_prog)
    check("card accepted -> PROG_CONFIRM", state == programs.PROG_CONFIRM)

    confirm_cb = FakeCallbackQuery("progcreate_confirm", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=confirm_cb)
    state = await programs.prog_confirm_callback(upd, ctx_prog)
    check("program created, conversation ends", state == -1)

    active_programs = db.list_active_programs()
    check("exactly 1 active program exists", len(active_programs) == 1)
    program_id = active_programs[0]["id"]
    check("program name correct", active_programs[0]["name"] == "سفر شمال")
    check("Reza can see it as one of his programs", program_id in [p["id"] for p in db.list_programs_for_user(reza_id)])

    # ============================================================
    # PROGRAM-LINKED EXPENSE: Reza logs a program expense (equal split),
    # which must NOT affect balances until Ali (admin) approves it.
    # ============================================================
    ctx_exp = make_context()
    upd = FakeUpdate(reza_tg, message=FakeMessage("/expense"))
    await expenses.expense_entry(upd, ctx_exp)
    upd = FakeUpdate(reza_tg, message=FakeMessage("بنزین"))
    state = await expenses.description_received(upd, ctx_exp)
    check("Reza (who's in a program) is asked which program -> EXP_PROGRAM_CHOICE", state == expenses.EXP_PROGRAM_CHOICE)

    prog_choice_cb = FakeCallbackQuery(f"expprog_{program_id}", reza_tg)
    upd = FakeUpdate(reza_tg, callback_query=prog_choice_cb)
    state = await expenses.program_choice_callback(upd, ctx_exp)
    check("program chosen -> EXP_PARTICIPANTS, pulled from program's participant list", state == expenses.EXP_PARTICIPANTS)
    check("participant pool = program's 3 participants", ctx_exp.user_data["exp_selected"] == {ali_id, reza_id, mohammad_id})

    part_done_cb = FakeCallbackQuery("participants_done", reza_tg)
    upd = FakeUpdate(reza_tg, callback_query=part_done_cb)
    state = await expenses.participants_callback(upd, ctx_exp)
    check("-> EXP_SPLIT_MODE", state == expenses.EXP_SPLIT_MODE)

    split_cb = FakeCallbackQuery("split_equal", reza_tg)
    upd = FakeUpdate(reza_tg, callback_query=split_cb)
    state = await expenses.split_mode_callback(upd, ctx_exp)
    check("equal mode -> EXP_AMOUNT", state == expenses.EXP_AMOUNT)

    upd = FakeUpdate(reza_tg, message=FakeMessage("300000"))
    state = await expenses.amount_received(upd, ctx_exp)
    check("amount accepted -> EXP_CONFIRM (equal mode skips weights)", state == expenses.EXP_CONFIRM)

    confirm_exp_cb = FakeCallbackQuery("expense_confirm", reza_tg)
    upd = FakeUpdate(reza_tg, callback_query=confirm_exp_cb)
    state = await expenses.confirm_callback(upd, ctx_exp)
    check("program expense saved -> still offers optional receipt (EXP_RECEIPT)", state == expenses.EXP_RECEIPT)

    saved_expenses = db.list_expenses(limit=5)
    program_expense = next(e for e in saved_expenses if e["program_id"] == program_id)
    check("expense saved with approval_status='pending'", program_expense["approval_status"] == "pending")
    check("Ali (admin) was notified for approval", ctx_exp.bot.send_message.await_count >= 1)
    check("PENDING program expense does not affect Reza's overall balance yet", balances.personal_balance(db, reza_id) == [])

    skip_receipt_cb = FakeCallbackQuery("receipt_skip", reza_tg)
    upd = FakeUpdate(reza_tg, callback_query=skip_receipt_cb)
    await expenses.receipt_skip_callback(upd, ctx_exp)

    approve_exp_cb = FakeCallbackQuery(f"expapprove_confirm_{program_expense['id']}", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=approve_exp_cb)
    await programs.expense_approval_callback(upd, ctx)
    check("expense now approved", db.get_expense(program_expense["id"])["approval_status"] == "approved")
    check("after approval, it now shows up in balances", len(balances.personal_balance(db, reza_id)) >= 1)

    # ============================================================
    # PROGRAM CHARGE: Mohammad charges the mother card via the unified
    # payment flow, admin confirms it.
    # ============================================================
    ctx_pay = make_context()
    upd = FakeUpdate(mohammad_tg, message=FakeMessage("/payment"))
    state = await payments.payment_entry(upd, ctx_pay)
    check("payment_entry -> PAY_TARGET", state == payments.PAY_TARGET)

    target_prog_cb = FakeCallbackQuery(f"paytargetprog_{program_id}", mohammad_tg)
    upd = FakeUpdate(mohammad_tg, callback_query=target_prog_cb)
    state = await payments.target_chosen_callback(upd, ctx_pay)
    check("program chosen as payment target -> PAY_AMOUNT", state == payments.PAY_AMOUNT)
    check("pay_target_program_id stored, not pay_target_id",
          ctx_pay.user_data.get("pay_target_program_id") == program_id and "pay_target_id" not in ctx_pay.user_data)

    upd = FakeUpdate(mohammad_tg, message=FakeMessage("600000"))
    state = await payments.pay_amount_received(upd, ctx_pay)
    check("amount accepted -> PAY_NOTE", state == payments.PAY_NOTE)

    skip_note_cb = FakeCallbackQuery("note_skip", mohammad_tg)
    upd = FakeUpdate(mohammad_tg, callback_query=skip_note_cb)
    state = await payments.note_skip_callback(upd, ctx_pay)
    check("note skipped -> PAY_CONFIRM", state == payments.PAY_CONFIRM)

    confirm_pay_cb = FakeCallbackQuery("payment_confirm", mohammad_tg)
    upd = FakeUpdate(mohammad_tg, callback_query=confirm_pay_cb)
    state = await payments.payment_confirm_callback(upd, ctx_pay)
    check("charge recorded -> PAY_RECEIPT", state == payments.PAY_RECEIPT)

    pending_charges = db.list_pending_program_charges(program_id)
    check("charge saved as pending, awaiting admin", len(pending_charges) == 1 and pending_charges[0]["amount"] == 600_000)

    skip_receipt_cb2 = FakeCallbackQuery("receipt_skip", mohammad_tg)
    upd = FakeUpdate(mohammad_tg, callback_query=skip_receipt_cb2)
    await payments.receipt_attach_skip_callback(upd, ctx_pay)

    charge_id = pending_charges[0]["id"]
    confirm_charge_cb = FakeCallbackQuery(f"chargeconf_confirm_{charge_id}", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=confirm_charge_cb)
    await programs.charge_confirmation_callback(upd, ctx)
    check("charge now confirmed", db.get_program_charge(charge_id)["status"] == "confirmed")

    # ============================================================
    # STATUS VIEW + CLOSE
    # ============================================================
    ctx_view = make_context()
    view_cb = FakeCallbackQuery(f"progview_{program_id}", reza_tg)
    upd = FakeUpdate(reza_tg, callback_query=view_cb)
    state = await programs.program_menu_callback(upd, ctx_view)
    check("non-admin can view program status -> PROG_MENU", state == programs.PROG_MENU)

    report = balances.program_report(db, program_id)
    check("total_charges reflects confirmed charge (600,000)", report["total_charges"] == 600_000)
    check("total_expenses reflects approved expense (300,000)", report["total_expenses"] == 300_000)
    check("remaining = 300,000", report["remaining"] == 300_000)

    ctx_close = make_context()
    close_cb = FakeCallbackQuery(f"progclose_{program_id}", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=close_cb)
    state = await programs.program_menu_callback(upd, ctx_close)
    check("close asks for confirmation first -> PROG_MENU", state == programs.PROG_MENU)

    close_confirm_cb = FakeCallbackQuery(f"progclose_confirm_{program_id}", ali_tg)
    upd = FakeUpdate(ali_tg, callback_query=close_confirm_cb)
    state = await programs.program_menu_callback(upd, ctx_close)
    check("program closed -> PROG_MENU", state == programs.PROG_MENU)
    check("program status is now 'closed'", db.get_program(program_id)["status"] == "closed")

    print()
    if failures:
        print(f"*** {len(failures)} TEST(S) FAILED: {failures}")
        raise SystemExit(1)
    else:
        print("ALL PROGRAM CONVERSATION-FLOW TESTS PASSED")


asyncio.run(main())
os.remove(DB_PATH)
