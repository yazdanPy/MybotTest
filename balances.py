# -*- coding: utf-8 -*-
"""
Glue layer between the database and split_engine: builds a NetLedger from
every non-deleted, approved expense + confirmed payment + confirmed program
charge, then answers balance questions.

Long-term programs (e.g. a multi-day trip with its own "mother card") are
represented in the ledger as a pseudo participant "program:<id>" rather than
a real user -- nobody personally owns the mother card. Whenever a program
pseudo-id would need to become a REAL payment between REAL people (personal
balance view, the group-wide report, or the minimum-transaction settlement
suggestion), it resolves to the group admin who created that program, since
the admin is the one who approves everything that touches the program and is
therefore the practical counterparty for any leftover surplus/shortfall.

Nothing here is cached -- it is rebuilt from scratch on every call. For a
small friend-group bot this is instant and guarantees the numbers are always
100% consistent with the underlying history, including after edits.
"""
from dataclasses import dataclass

from split_engine import NetLedger, simplify_debts

PROGRAM_PSEUDO_PREFIX = "program:"


def program_pseudo_id(program_id: int) -> str:
    return f"{PROGRAM_PSEUDO_PREFIX}{program_id}"


def is_program_pseudo(x) -> bool:
    return isinstance(x, str) and x.startswith(PROGRAM_PSEUDO_PREFIX)


def program_id_from_pseudo(x: str) -> int:
    return int(x[len(PROGRAM_PSEUDO_PREFIX):])


def build_ledger(db) -> NetLedger:
    ledger = NetLedger()
    for expense in db.all_expenses_with_participants():
        program_id = expense.get("program_id")
        if program_id:
            creditor = program_pseudo_id(program_id)
            for p in expense["participants"]:
                ledger.add_expense_debt(debtor_id=p["user_id"], creditor_id=creditor, amount=p["share_amount"])
        else:
            payer_id = expense["payer_id"]
            for p in expense["participants"]:
                if p["user_id"] != payer_id:
                    ledger.add_expense_debt(debtor_id=p["user_id"], creditor_id=payer_id, amount=p["share_amount"])
    for payment in db.all_confirmed_payments():
        ledger.add_payment(payer_id=payment["from_user_id"], payee_id=payment["to_user_id"], amount=payment["amount"])
    for charge in db.all_confirmed_program_charges():
        ledger.add_payment(
            payer_id=charge["user_id"], payee_id=program_pseudo_id(charge["program_id"]), amount=charge["amount"]
        )
    return ledger


def _resolve_other(db, other_id):
    """
    Returns (id, name, card, program_name) for either a real user id or a
    program pseudo-id. For a program pseudo-id, resolves to the group admin
    who created that program (the practical settlement counterparty), with
    program_name set so callers can label it clearly rather than silently
    merging it into that admin's other balances.
    """
    if is_program_pseudo(other_id):
        program = db.get_program(program_id_from_pseudo(other_id))
        admin = db.get_user_by_id(program["creator_id"])
        return admin["id"], admin["first_name"], admin["card_number"], program["name"]
    u = db.get_user_by_id(other_id)
    return u["id"], u["first_name"], u["card_number"], None


@dataclass
class BalanceLine:
    other_user_id: int
    other_name: str
    other_card: str | None
    amount: int          # always positive
    i_owe_them: bool      # True: the requested user owes `other`; False: `other` owes the user
    program_name: str | None = None  # set if this line came from a long-term program, not a direct debt


def personal_balance(db, user_id: int) -> list[BalanceLine]:
    """All non-zero balances that concern `user_id`, sorted biggest first."""
    ledger = build_ledger(db)
    lines = []
    for a, b, amount in ledger.all_pairs():
        if a == user_id:
            other_id, name, card, program_name = _resolve_other(db, b)
            lines.append(BalanceLine(other_id, name, card, amount, True, program_name))
        elif b == user_id:
            other_id, name, card, program_name = _resolve_other(db, a)
            lines.append(BalanceLine(other_id, name, card, amount, False, program_name))
    lines.sort(key=lambda l: -l.amount)
    return lines


def group_report(db):
    """
    Returns (pairs, simplified_transactions) where:
      pairs: list of (debtor_user_row, creditor_user_row, amount, program_name_or_None)
             for every outstanding pairwise balance in the group. Program-derived
             pairs resolve to the program's admin but keep their program_name
             label so the detailed listing stays clear about the source.
      simplified_transactions: list of (from_user_row, to_user_row, amount) --
             the minimal settle-up plan for the whole group. Here, program
             pseudo-positions ARE merged into the admin's real position, since
             the whole point of this list is minimizing how many real
             transactions people actually need to make.
    """
    ledger = build_ledger(db)

    pairs_raw = ledger.all_pairs()
    pairs = []
    for a, b, amt in pairs_raw:
        debtor_id, debtor_name, debtor_card, debtor_prog = _resolve_other(db, a)
        creditor_id, creditor_name, creditor_card, creditor_prog = _resolve_other(db, b)
        program_name = debtor_prog or creditor_prog
        pairs.append((db.get_user_by_id(debtor_id), db.get_user_by_id(creditor_id), amt, program_name))

    raw_positions = ledger.net_positions()
    merged_positions: dict[int, int] = {}
    for uid, amt in raw_positions.items():
        if is_program_pseudo(uid):
            program = db.get_program(program_id_from_pseudo(uid))
            real_id = program["creator_id"]
        else:
            real_id = uid
        merged_positions[real_id] = merged_positions.get(real_id, 0) + amt

    simplified_raw = simplify_debts(merged_positions)
    simplified = [(db.get_user_by_id(a), db.get_user_by_id(b), amt) for a, b, amt in simplified_raw]
    return pairs, simplified


def program_report(db, program_id: int):
    """
    Self-contained status for one program: total confirmed charges, total
    approved expenses, remaining balance in the mother card, and each
    participant's net position (positive = they've contributed more than
    they've consumed so far -- the program/admin owes them; negative = they
    owe the program). This is live at any time, not just when the program
    closes, since every expense and charge already carries its own approved
    amount the moment it's confirmed.
    """
    program = db.get_program(program_id)
    expenses = db.all_program_expenses(program_id)
    charges = [c for c in db.all_confirmed_program_charges() if c["program_id"] == program_id]

    total_expenses = sum(e["amount"] for e in expenses)
    total_charges = sum(c["amount"] for c in charges)
    remaining = total_charges - total_expenses

    ledger = NetLedger()
    pseudo = "program"  # local-only pseudo id, fine since this ledger is scoped to one program
    for e in expenses:
        for p in e["participants"]:
            ledger.add_expense_debt(debtor_id=p["user_id"], creditor_id=pseudo, amount=p["share_amount"])
    for c in charges:
        ledger.add_payment(payer_id=c["user_id"], payee_id=pseudo, amount=c["amount"])

    positions = ledger.net_positions()
    per_participant = []
    for pp in program["participants"]:
        uid = pp["user_id"]
        net = positions.get(uid, 0)  # positive = they're owed (overpaid), negative = they owe
        per_participant.append({"user_id": uid, "name": pp["first_name"], "net": net})
    per_participant.sort(key=lambda x: -x["net"])

    return {
        "program": program,
        "total_expenses": total_expenses,
        "total_charges": total_charges,
        "remaining": remaining,
        "per_participant": per_participant,
    }
