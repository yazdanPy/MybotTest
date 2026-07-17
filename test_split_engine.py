# -*- coding: utf-8 -*-
"""Manual sanity tests for split_engine.py -- run with: python3 test_split_engine.py"""
from split_engine import calculate_shares, NetLedger, simplify_debts

failures = []


def check(name, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


# ---- calculate_shares: weighted ----
# Ali(weight 3) + Reza(weight 1) share 400,000 -> exactly 300,000 / 100,000
shares = calculate_shares(400_000, [{"user_id": 1, "weight": 3}, {"user_id": 2, "weight": 1}], "weighted")
check("weighted exact split", shares == {1: 300_000, 2: 100_000})
check("weighted sums to total", sum(shares.values()) == 400_000)

# ---- calculate_shares: remainder handling (not evenly divisible) ----
shares2 = calculate_shares(100_000, [{"user_id": 1, "weight": 1}, {"user_id": 2, "weight": 1}, {"user_id": 3, "weight": 1}], "weighted")
check("remainder split sums exactly", sum(shares2.values()) == 100_000)
check("remainder split all positive", all(v > 0 for v in shares2.values()))

# ---- calculate_shares: equal mode ignores weight ----
shares3 = calculate_shares(300_000, [{"user_id": 1, "weight": 5}, {"user_id": 2, "weight": 1}], "equal")
check("equal split ignores weight", shares3 == {1: 150_000, 2: 150_000})

# ---- calculate_shares: single participant gets everything ----
shares4 = calculate_shares(50_000, [{"user_id": 9, "weight": 4}], "weighted")
check("single participant gets full amount", shares4 == {9: 50_000})

# ---- calculate_shares: invalid inputs raise ----
try:
    calculate_shares(0, [{"user_id": 1, "weight": 1}])
    check("zero amount raises", False)
except ValueError:
    check("zero amount raises", True)

try:
    calculate_shares(1000, [])
    check("empty participants raises", False)
except ValueError:
    check("empty participants raises", True)

# ---- NetLedger: simple two-person netting (the core "تهاتر" requirement) ----
ledger = NetLedger()
# Expense 1: Ali pays 400,000; Reza's share is 100,000 -> Reza owes Ali 100,000
ledger.add_expense_debt(debtor_id=2, creditor_id=1, amount=100_000)
check("after expense1, Reza owes Ali 100,000", ledger.net_between(2, 1) == 100_000)
check("symmetric: Ali owes Reza -100,000 (i.e. Reza owes Ali)", ledger.net_between(1, 2) == -100_000)

# Expense 2 (days later): Reza pays 300,000; Ali's share is 150,000 -> Ali owes Reza 150,000
ledger.add_expense_debt(debtor_id=1, creditor_id=2, amount=150_000)
# Net: Ali owes Reza 150,000, Reza owes Ali 100,000 => net Ali owes Reza 50,000
check("netted: Ali owes Reza 50,000", ledger.net_between(1, 2) == 50_000)
check("netted other direction: Reza owes Ali -50,000", ledger.net_between(2, 1) == -50_000)

pairs = ledger.all_pairs()
check("all_pairs reports exactly one entry", len(pairs) == 1)
check("all_pairs entry is (Ali=1 owes Reza=2, 50000)", pairs == [(1, 2, 50_000)])

# Now Ali pays Reza back 50,000 -> should be fully settled
ledger.add_payment(payer_id=1, payee_id=2, amount=50_000)
check("after settling payment, net is zero", ledger.net_between(1, 2) == 0)
check("all_pairs now empty (fully settled)", ledger.all_pairs() == [])

# ---- Group-wide debt simplification ----
# Classic scenario: A owes B 100,000; B owes C 100,000  => should simplify to A pays C 100,000 directly
ledger2 = NetLedger()
ledger2.add_expense_debt(debtor_id="A", creditor_id="B", amount=100_000)
ledger2.add_expense_debt(debtor_id="B", creditor_id="C", amount=100_000)
positions = ledger2.net_positions()
check("A's net position is -100,000", positions.get("A", 0) == -100_000)
check("B's net position is 0 (owes and is owed equally)", positions.get("B", 0) == 0)
check("C's net position is +100,000", positions.get("C", 0) == 100_000)
check("positions sum to zero", sum(positions.values()) == 0)

txns = simplify_debts(positions)
check("simplified to a single transaction", len(txns) == 1)
check("simplified transaction is A->C 100,000", txns == [("A", "C", 100_000)])

# ---- A more complex 4-person scenario ----
ledger3 = NetLedger()
ledger3.add_expense_debt("A", "B", 50_000)
ledger3.add_expense_debt("A", "C", 30_000)
ledger3.add_expense_debt("D", "B", 20_000)
ledger3.add_expense_debt("B", "D", 70_000)  # B separately owes D from another outing
positions3 = ledger3.net_positions()
check("complex scenario positions sum to zero", sum(positions3.values()) == 0)
txns3 = simplify_debts(positions3)
total_settled = sum(t[2] for t in txns3)
# verify the simplified transactions actually re-create the same net positions
recomputed = {}
for frm, to, amt in txns3:
    recomputed[frm] = recomputed.get(frm, 0) - amt
    recomputed[to] = recomputed.get(to, 0) + amt
for uid in positions3:
    recomputed.setdefault(uid, 0)
check("simplified transactions reproduce exact original net positions", recomputed == positions3)
check("simplify never uses more transactions than (n-1)", len(txns3) <= len(positions3) - 1)

# ---- Regression test: MIXED int/string ids must not crash (this is exactly
# ---- how a long-term program's shared pool is represented -- a string
# ---- pseudo-id like "program:5" alongside real integer user ids) ----
ledger4 = NetLedger()
ledger4.add_expense_debt(debtor_id=101, creditor_id="program:5", amount=200_000)  # int debtor, string creditor
ledger4.add_expense_debt(debtor_id=102, creditor_id="program:5", amount=100_000)
ledger4.add_payment(payer_id=102, payee_id="program:5", amount=150_000)  # 102 overpaid their share
try:
    pairs4 = ledger4.all_pairs()
    check("all_pairs() does not crash with mixed int/string ids", True)
except TypeError as e:
    check(f"all_pairs() does not crash with mixed int/string ids -- got {e}", False)
    pairs4 = []
check("mixed-id pairs computed correctly (101 owes program 200,000; program owes 102 50,000)",
      set(pairs4) == {(101, "program:5", 200_000), ("program:5", 102, 50_000)})

positions4 = ledger4.net_positions()
try:
    txns4 = simplify_debts(positions4)
    check("simplify_debts() does not crash with mixed int/string ids", True)
except TypeError as e:
    check(f"simplify_debts() does not crash with mixed int/string ids -- got {e}", False)
    txns4 = []
# 101 owes 200,000 total; "program:5" is owed 150,000 and 102 is owed 50,000 --
# the greedy algorithm settles this in 2 transactions (not necessarily mirroring
# the original pairwise relationships, since "simplify" optimizes for fewest
# transactions, not for preserving who-originally-owed-whom).
total_paid = sum(amt for _, _, amt in txns4)
check("mixed-id simplification pays out exactly the total owed (200,000)", total_paid == 200_000)
recomputed4 = {}
for frm, to, amt in txns4:
    recomputed4[frm] = recomputed4.get(frm, 0) - amt
    recomputed4[to] = recomputed4.get(to, 0) + amt
for k in positions4:
    recomputed4.setdefault(k, 0)
check("mixed-id simplification reproduces the exact original net positions", recomputed4 == positions4)

print()
if failures:
    print(f"*** {len(failures)} TEST(S) FAILED: {failures}")
    raise SystemExit(1)
else:
    print("ALL TESTS PASSED")
