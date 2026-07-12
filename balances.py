# -*- coding: utf-8 -*-
"""
Glue layer between the database and split_engine: builds a NetLedger from
every non-deleted expense + confirmed payment, then answers balance questions.

Nothing here is cached -- it is rebuilt from scratch on every call. For a
small friend-group bot this is instant (a few hundred/thousand rows at most)
and guarantees the numbers are always 100% consistent with the underlying
history, including after edits or deletions.
"""
from dataclasses import dataclass

from split_engine import NetLedger, simplify_debts


def build_ledger(db) -> NetLedger:
    ledger = NetLedger()
    for expense in db.all_expenses_with_participants():
        payer_id = expense["payer_id"]
        for p in expense["participants"]:
            if p["user_id"] != payer_id:
                ledger.add_expense_debt(debtor_id=p["user_id"], creditor_id=payer_id, amount=p["share_amount"])
    for payment in db.all_confirmed_payments():
        ledger.add_payment(payer_id=payment["from_user_id"], payee_id=payment["to_user_id"], amount=payment["amount"])
    return ledger


@dataclass
class BalanceLine:
    other_user_id: int
    other_name: str
    other_card: str | None
    amount: int          # always positive
    i_owe_them: bool      # True: the requested user owes `other`; False: `other` owes the user


def personal_balance(db, user_id: int) -> list[BalanceLine]:
    """All non-zero balances that concern `user_id`, sorted biggest first."""
    ledger = build_ledger(db)
    lines = []
    for a, b, amount in ledger.all_pairs():
        if a == user_id:
            other = db.get_user_by_id(b)
            lines.append(BalanceLine(b, other["first_name"], other["card_number"], amount, True))
        elif b == user_id:
            other = db.get_user_by_id(a)
            lines.append(BalanceLine(a, other["first_name"], other["card_number"], amount, False))
    lines.sort(key=lambda l: -l.amount)
    return lines


def group_report(db):
    """
    Returns (pairs, simplified_transactions) where:
      pairs: list of (debtor_user_row, creditor_user_row, amount) for every
             outstanding pairwise balance in the group.
      simplified_transactions: list of (from_user_row, to_user_row, amount) -
             the minimal settle-up plan for the whole group.
    """
    ledger = build_ledger(db)
    pairs_raw = ledger.all_pairs()
    pairs = [(db.get_user_by_id(a), db.get_user_by_id(b), amt) for a, b, amt in pairs_raw]

    positions = ledger.net_positions()
    simplified_raw = simplify_debts(positions)
    simplified = [(db.get_user_by_id(a), db.get_user_by_id(b), amt) for a, b, amt in simplified_raw]
    return pairs, simplified
