# -*- coding: utf-8 -*-
"""
Tests the long-term "program" (mother-card trip fund) financial logic:
  - program expenses don't affect balances until approved
  - per-program net position (contributed vs consumed) is live at any time
  - a person's balance relative to a program resolves to the group admin,
    labeled with the program's name (not silently merged into a raw number)
  - the group-wide minimum-transaction settlement DOES merge program
    positions into the admin's real position (since that's the whole point
    of that specific view)
"""
import os

from database import Database
from split_engine import calculate_shares
import balances

DB_PATH = "/tmp/test_programs.db"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
db = Database(DB_PATH)

failures = []


def check(name, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


# --- 3 friends, Ali is admin ---
ali_id = db.create_pending_user(1, "a", "علی")
db.set_user_registration_details(ali_id, "6037991111111111", 2)
db.activate_user(ali_id, as_admin=True)

reza_id = db.create_pending_user(2, "b", "رضا")
db.set_user_registration_details(reza_id, "6037992222222222", 1)
db.activate_user(reza_id)

mohammad_id = db.create_pending_user(3, "c", "محمد")
db.set_user_registration_details(mohammad_id, "6037993333333333", 1)
db.activate_user(mohammad_id)

# --- Ali creates "سفر شمال" with all 3, weights 2/1/1 ---
program_id = db.create_program("سفر شمال", "6037990000000000", ali_id, {ali_id: 2, reza_id: 1, mohammad_id: 1})
program = db.get_program(program_id)
check("program created with correct name", program["name"] == "سفر شمال")
check("program starts active", program["status"] == "active")
check("3 participants stored with correct weights", {p["user_id"]: p["weight"] for p in program["participants"]} == {ali_id: 2, reza_id: 1, mohammad_id: 1})

# --- Reza and Mohammad charge the mother card ---
charge1 = db.create_program_charge(program_id, reza_id, 500_000, None)
charge2 = db.create_program_charge(program_id, mohammad_id, 300_000, None)
report = balances.program_report(db, program_id)
check("before confirmation, charges don't count yet", report["total_charges"] == 0)

db.set_program_charge_status(charge1, "confirmed")
db.set_program_charge_status(charge2, "confirmed")
report = balances.program_report(db, program_id)
check("after confirmation, total_charges = 800,000", report["total_charges"] == 800_000)
check("no expenses yet, remaining = 800,000", report["remaining"] == 800_000)

# --- an expense against the program, equal split, starts PENDING ---
participants = [{"user_id": ali_id, "weight": 1}, {"user_id": reza_id, "weight": 1}, {"user_id": mohammad_id, "weight": 1}]
shares = calculate_shares(400_000, participants, "equal")  # 133,333 / 133,333 / 133,334
exp_id = db.create_expense(ali_id, ali_id, 400_000, "بنزین و اقامتگاه", "equal", shares, {ali_id: 1, reza_id: 1, mohammad_id: 1}, program_id=program_id, approval_status="pending")

check("expense saved as pending", db.get_expense(exp_id)["approval_status"] == "pending")
report = balances.program_report(db, program_id)
check("PENDING program expense does not count toward total_expenses yet", report["total_expenses"] == 0)
# NOTE: charges are already confirmed at this point, so Reza legitimately already
# shows a balance (from his 500,000 charge alone) -- the thing we're actually
# verifying is that the PENDING expense's share hasn't been subtracted from it yet.
pending_check_lines = balances.personal_balance(db, reza_id)
check("before approval, Reza's balance reflects only his charge (500,000), not yet reduced by his pending expense share",
      len(pending_check_lines) == 1 and pending_check_lines[0].amount == 500_000)

# --- Ali (admin) approves it ---
db.set_expense_approval(exp_id, "approved")
report = balances.program_report(db, program_id)
check("after approval, total_expenses = 400,000", report["total_expenses"] == 400_000)
check("remaining = 800,000 - 400,000 = 400,000", report["remaining"] == 400_000)

net_by_user = {p["user_id"]: p["net"] for p in report["per_participant"]}
check("Reza's net = contributed 500,000 - consumed share (overpaid, positive)", net_by_user[reza_id] == 500_000 - shares[reza_id])
check("Mohammad's net = contributed 300,000 - consumed share", net_by_user[mohammad_id] == 300_000 - shares[mohammad_id])
check("Ali's net = contributed 0 - consumed share (negative, he owes)", net_by_user[ali_id] == 0 - shares[ali_id])
# The ledger's true zero-sum property holds across ALL entities, including the
# "program" pseudo-id itself -- so the 3 human participants' nets sum to
# whatever's left UNCLAIMED in the mother card (the `remaining` balance), not
# to zero (zero would only hold if the pseudo-id's own position were included too).
check("participants' nets sum to the program's remaining balance (400,000)", sum(net_by_user.values()) == report["remaining"])

# --- personal_balance: Reza's overpayment should resolve to the ADMIN (Ali), labeled with the program name ---
reza_lines = balances.personal_balance(db, reza_id)
check("Reza now has exactly 1 balance line (from the program)", len(reza_lines) == 1)
line = reza_lines[0]
check("it resolves to Ali (the group admin who created the program), not a fake 'program' entity", line.other_user_id == ali_id)
check("it is clearly labeled with the program's name", line.program_name == "سفر شمال")
check("direction is correct: Ali/program owes Reza (Reza overpaid)", line.i_owe_them is False)
check("amount matches Reza's net overpayment", line.amount == 500_000 - shares[reza_id])

# --- group_report: pairs should carry the program label; simplify should MERGE into Ali's real position ---
pairs, simplified = balances.group_report(db)
check("pairs is a list of 4-tuples now (debtor, creditor, amount, program_name)", all(len(p) == 4 for p in pairs))
program_pairs = [p for p in pairs if p[3] == "سفر شمال"]
check("at least one pair is labeled with the program name", len(program_pairs) >= 1)

# Now ALSO give Ali a completely unrelated, ordinary personal debt: Ali owes Reza 50,000 directly (not via any program)
# via a separate everyday expense, to verify group-wide simplification merges both sources against Ali correctly.
shares2 = calculate_shares(50_000, [{"user_id": reza_id, "weight": 1}, {"user_id": ali_id, "weight": 1}], "equal")
# make Reza the payer so Ali ends up owing Reza his 25,000 share directly (ordinary, non-program expense)
exp2 = db.create_expense(reza_id, reza_id, 50_000, "قهوه", "equal", shares2, {reza_id: 1, ali_id: 1})
check("ordinary (non-program) expense is auto-approved immediately", db.get_expense(exp2)["approval_status"] == "approved")

_, simplified_after = balances.group_report(db)
ali_to_reza_or_reverse = [t for t in simplified_after if ali_id in (t[0]["id"], t[1]["id"]) and reza_id in (t[0]["id"], t[1]["id"])]
check("exactly one merged transaction between Ali and Reza in the simplified settlement (program debt + direct debt combined)", len(ali_to_reza_or_reverse) == 1)
merged_amount = ali_to_reza_or_reverse[0][2]
# Both debts point the SAME direction (Ali/program owes Reza from the trip,
# AND Ali personally owes Reza from the coffee) so they ADD UP into one
# combined payment, they don't offset.
expected_merged = (500_000 - shares[reza_id]) + 25_000
check(f"merged amount is correct ({expected_merged:,})", merged_amount == expected_merged)

# --- closing the program ---
db.close_program(program_id)
check("program is now closed", db.get_program(program_id)["status"] == "closed")
# closed programs should be excluded from list_active_programs / list_programs_for_user(active_only=True)
check("closed program excluded from active list", program_id not in [p["id"] for p in db.list_active_programs()])
check("closed program excluded from user's active programs", program_id not in [p["id"] for p in db.list_programs_for_user(reza_id)])
# but the underlying debt/credit does NOT disappear just because the program closed
reza_lines_after_close = balances.personal_balance(db, reza_id)
check("Reza's balance from the (now closed) program still shows until actually settled", len(reza_lines_after_close) >= 1)

os.remove(DB_PATH)

print()
if failures:
    print(f"*** {len(failures)} TEST(S) FAILED: {failures}")
    raise SystemExit(1)
else:
    print("ALL PROGRAM FINANCIAL LOGIC TESTS PASSED")
