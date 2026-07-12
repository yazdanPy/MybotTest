# -*- coding: utf-8 -*-
"""Integration test: simulate a realistic scenario end-to-end and hand-verify the numbers."""
import os
from database import Database
from split_engine import calculate_shares
import balances

DB_PATH = "/tmp/test_hesabkitab.db"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

db = Database(DB_PATH)
failures = []


def check(name, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


# --- create 3 friends: Ali (weight 3: himself+wife+kid), Reza (weight 1), Mohammad (weight 1)
ali_id = db.create_pending_user(telegram_id=111, username="ali_tg", first_name="علی")
db.set_user_registration_details(ali_id, "6037991111111111", 3)
db.activate_user(ali_id, as_admin=True)

reza_id = db.create_pending_user(telegram_id=222, username="reza_tg", first_name="رضا")
db.set_user_registration_details(reza_id, "6037992222222222", 1)
db.activate_user(reza_id)

mohammad_id = db.create_pending_user(telegram_id=333, username=None, first_name="محمد")
db.set_user_registration_details(mohammad_id, "6037993333333333", 1)
db.activate_user(mohammad_id)

active = db.list_active_users()
check("3 active users exist", len(active) == 3)
check("ali is admin", db.get_user_by_id(ali_id)["is_admin"] == 1)

# --- Expense 1: Ali pays 400,000 for dinner, everyone present, weighted split ---
# weights: ali=3, reza=1, mohammad=1 -> total weight 5
# expected shares: ali 240,000 reza 80,000 mohammad 80,000
participants = [
    {"user_id": ali_id, "weight": 3},
    {"user_id": reza_id, "weight": 1},
    {"user_id": mohammad_id, "weight": 1},
]
shares1 = calculate_shares(400_000, participants, "weighted")
check("expense1 shares sum to 400,000", sum(shares1.values()) == 400_000)
weights_used = {p["user_id"]: p["weight"] for p in participants}
exp1_id = db.create_expense(ali_id, ali_id, 400_000, "شام رستوران", "weighted", shares1, weights_used)

# --- Expense 2: Reza pays 150,000 for coffee, only Reza & Mohammad present, equal split ---
participants2 = [{"user_id": reza_id, "weight": 1}, {"user_id": mohammad_id, "weight": 1}]
shares2 = calculate_shares(150_000, participants2, "equal")
check("expense2 equal split is 75,000/75,000", shares2 == {reza_id: 75_000, mohammad_id: 75_000})
weights_used2 = {p["user_id"]: p["weight"] for p in participants2}
exp2_id = db.create_expense(reza_id, reza_id, 150_000, "قهوه", "equal", shares2, weights_used2)

# Expected raw debts so far:
#   Reza owes Ali:      shares1[reza] = 80,000
#   Mohammad owes Ali:  shares1[mohammad] = 80,000
#   Mohammad owes Reza: shares2[mohammad] = 75,000
# Net between Ali & Reza = Reza owes Ali 80,000 (no netting yet, different pair)
# Net between Ali & Mohammad = Mohammad owes Ali 80,000
# Net between Reza & Mohammad = Mohammad owes Reza 75,000

pairs, simplified = balances.group_report(db)
pair_map = {(p[0]["id"], p[1]["id"]): p[2] for p in pairs}
check("Reza owes Ali 80,000 (raw)", pair_map.get((reza_id, ali_id)) == 80_000)
check("Mohammad owes Ali 80,000 (raw)", pair_map.get((mohammad_id, ali_id)) == 80_000)
check("Mohammad owes Reza 75,000 (raw)", pair_map.get((mohammad_id, reza_id)) == 75_000)

# --- Now record a payment: Mohammad pays Ali back 80,000, and it gets CONFIRMED ---
pay1_id = db.create_payment(mohammad_id, ali_id, 80_000, "کارت به کارت")
db.set_payment_status(pay1_id, "confirmed")

pairs, simplified = balances.group_report(db)
pair_map = {(p[0]["id"], p[1]["id"]): p[2] for p in pairs}
check("after Mohammad pays Ali, that pair disappears (settled)", (mohammad_id, ali_id) not in pair_map)
check("Reza still owes Ali 80,000", pair_map.get((reza_id, ali_id)) == 80_000)
check("Mohammad still owes Reza 75,000", pair_map.get((mohammad_id, reza_id)) == 75_000)

# --- THE KEY "تهاتر" SCENARIO from the user's request ---
# Later, Ali goes out again and pays 100,000 for a small snack with just Reza (equal split: 50k/50k)
participants3 = [{"user_id": ali_id, "weight": 1}, {"user_id": reza_id, "weight": 1}]  # equal mode -> weight ignored anyway
shares3 = calculate_shares(100_000, participants3, "equal")
weights_used3 = {p["user_id"]: p["weight"] for p in participants3}
exp3_id = db.create_expense(ali_id, ali_id, 100_000, "اسنک", "equal", shares3, weights_used3)
# Now Reza owes Ali an additional 50,000 on top of the earlier 80,000 => 130,000 raw
# But suppose Reza had ALSO separately paid Ali back 100,000 in cash for the old debt (recorded as payment)
pay2_id = db.create_payment(reza_id, ali_id, 100_000, "نقدی پرداخت شد")
db.set_payment_status(pay2_id, "confirmed")
# Expected net: Reza owed Ali 130,000 total, paid back 100,000 => Reza still owes Ali net 30,000
pairs, simplified = balances.group_report(db)
pair_map = {(p[0]["id"], p[1]["id"]): p[2] for p in pairs}
check("تهاتر: Reza net owes Ali exactly 30,000 after mixed payments/expenses", pair_map.get((reza_id, ali_id)) == 30_000)

# --- personal_balance() view for Reza ---
reza_balances = balances.personal_balance(db, reza_id)
check("Reza has exactly 2 open balances (Ali + Mohammad)", len(reza_balances) == 2)
reza_to_ali = next(b for b in reza_balances if b.other_user_id == ali_id)
check("Reza's view: owes Ali 30,000", reza_to_ali.amount == 30_000 and reza_to_ali.i_owe_them is True)
reza_to_mohammad = next(b for b in reza_balances if b.other_user_id == mohammad_id)
check("Reza's view: Mohammad owes Reza 75,000", reza_to_mohammad.amount == 75_000 and reza_to_mohammad.i_owe_them is False)

# --- pending (unconfirmed) payment shouldn't affect balance ---
pay3_id = db.create_payment(mohammad_id, reza_id, 75_000, "test unconfirmed")
pairs_before_confirm, _ = balances.group_report(db)
pair_map_before = {(p[0]["id"], p[1]["id"]): p[2] for p in pairs_before_confirm}
check("unconfirmed payment does NOT change balance yet", pair_map_before.get((mohammad_id, reza_id)) == 75_000)
db.set_payment_status(pay3_id, "confirmed")
pairs_after_confirm, _ = balances.group_report(db)
pair_map_after = {(p[0]["id"], p[1]["id"]): p[2] for p in pairs_after_confirm}
check("once confirmed, Mohammad/Reza pair fully settles", (mohammad_id, reza_id) not in pair_map_after)

# --- soft-delete an expense and verify it's excluded from balances ---
pairs_before_delete, _ = balances.group_report(db)
db.delete_expense(exp3_id)  # remove the 100,000 ali/reza snack expense
pairs_after_delete, _ = balances.group_report(db)
pair_map_ad = {(p[0]["id"], p[1]["id"]): p[2] for p in pairs_after_delete}
# without exp3 (50,000 owed) but the 100,000 cash payment still recorded -> Reza would be OVERPAID by 20,000
# i.e. now Ali owes Reza 20,000 (reversed direction!)
check("after deleting exp3, direction flips: Ali owes Reza 20,000", pair_map_ad.get((ali_id, reza_id)) == 20_000)

# --- admin/member management ---
db.set_weight(reza_id, 2)
check("weight update persisted", db.get_user_by_id(reza_id)["weight"] == 2)
db.remove_user(mohammad_id)
check("removed user no longer active", len(db.list_active_users()) == 2)
db.restore_user(mohammad_id)
check("restored user active again", len(db.list_active_users()) == 3)

# --- expense history listing ---
history = db.list_expenses(limit=10)
check("history excludes soft-deleted expense", all(e["id"] != exp3_id for e in history))
check("history has correct count (2 remaining)", len(history) == 2)
check("history entries carry participants with names", all("first_name" in p for e in history for p in e["participants"]))

print()
if failures:
    print(f"*** {len(failures)} TEST(S) FAILED: {failures}")
    raise SystemExit(1)
else:
    print("ALL DATABASE/BALANCE TESTS PASSED")

os.remove(DB_PATH)
