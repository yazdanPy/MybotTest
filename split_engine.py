# -*- coding: utf-8 -*-
"""
Pure-logic engine for the group expense bot:
  1. calculate_shares    -> split one bill among the people who were present,
                             weighted by household size (or equally).
  2. NetLedger            -> keeps a running pairwise ledger from every expense
                             + payment, and can report the netted ("تهاتر شده")
                             balance between any two people.
  3. simplify_debts       -> classic greedy "minimum cash flow" algorithm that
                             turns a messy web of debts into the smallest possible
                             set of settle-up transactions for the whole group.

No Telegram / database imports here on purpose, so this file can be tested and
trusted in complete isolation from the bot plumbing.
"""
from collections import defaultdict
from dataclasses import dataclass, field


def calculate_shares(total_amount: int, participants: list[dict], mode: str = "weighted") -> dict[int, int]:
    """
    Split `total_amount` (an integer, Toman) among `participants`.

    participants: list of {"user_id": int, "weight": int}
    mode: "weighted" -> proportional to each person's household weight
          "equal"    -> split evenly regardless of weight

    Returns {user_id: share_amount} where the shares always sum EXACTLY to
    total_amount (any rounding remainder is given to the last participant,
    chosen deterministically, so re-running this never gives different results).
    """
    if total_amount <= 0:
        raise ValueError("total_amount must be positive")
    if not participants:
        raise ValueError("participants must not be empty")

    if mode == "equal":
        weights = {p["user_id"]: 1 for p in participants}
    elif mode == "weighted":
        weights = {p["user_id"]: max(1, int(p["weight"])) for p in participants}
    else:
        raise ValueError(f"unknown split mode: {mode}")

    total_weight = sum(weights.values())
    ids_sorted = sorted(weights.keys())  # deterministic order

    shares = {}
    allocated = 0
    for idx, uid in enumerate(ids_sorted):
        if idx == len(ids_sorted) - 1:
            shares[uid] = total_amount - allocated  # last participant absorbs the rounding remainder
        else:
            share = (total_amount * weights[uid]) // total_weight
            shares[uid] = share
            allocated += share
    return shares


@dataclass
class NetLedger:
    """
    Tracks gross "i owes j" amounts accumulated from expenses and payments,
    and can answer both pairwise-netted and whole-group questions.
    """
    # pairwise[i][j] = total gross amount i owes j (before netting against j->i)
    pairwise: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))

    def add_expense_debt(self, debtor_id: int, creditor_id: int, amount: int) -> None:
        if amount <= 0 or debtor_id == creditor_id:
            return
        self.pairwise[debtor_id][creditor_id] += amount

    def add_payment(self, payer_id: int, payee_id: int, amount: int) -> None:
        """A payment from payer_id to payee_id reduces what payer_id owes payee_id."""
        if amount <= 0 or payer_id == payee_id:
            return
        self.pairwise[payer_id][payee_id] -= amount

    def net_between(self, a: int, b: int) -> int:
        """
        Positive  -> `a` owes `b` this amount (net, i.e. already تهاتر شده).
        Negative  -> `b` owes `a` the absolute value.
        Zero      -> settled up.
        """
        return self.pairwise[a][b] - self.pairwise[b][a]

    def all_pairs(self) -> list[tuple[int, int, int]]:
        """
        All (debtor, creditor, amount) triples with amount > 0, netted,
        deduplicated so each pair appears only once (a,b) not also (b,a).

        Sorting uses `key=str` because ids aren't always the same type --
        e.g. real integer user ids can appear alongside string pseudo-ids
        like "program:5" (used to represent a long-term program's shared
        pool as a ledger participant). Plain `sorted()` would raise
        TypeError comparing int to str directly; sorting by string
        representation sidesteps that while keeping the order deterministic
        (the exact order doesn't matter for correctness, only that pairs
        aren't produced twice in both directions).
        """
        seen = set()
        result = []
        ids = set(self.pairwise.keys())
        for i in list(self.pairwise.keys()):
            ids.update(self.pairwise[i].keys())
        ids = sorted(ids, key=str)
        n = len(ids)
        for idx_i in range(n):
            for idx_j in range(idx_i + 1, n):
                i, j = ids[idx_i], ids[idx_j]
                net = self.net_between(i, j)
                if net > 0:
                    result.append((i, j, net))
                elif net < 0:
                    result.append((j, i, -net))
        return result

    def net_positions(self) -> dict[int, int]:
        """
        Overall net position per person across the whole group:
          positive -> this person is, on net, owed money by the group
          negative -> this person, on net, owes the group
        Summing all values always yields 0.
        """
        position = defaultdict(int)
        for i, row in self.pairwise.items():
            for j, amount in row.items():
                if amount == 0:
                    continue
                position[i] -= amount
                position[j] += amount
        return dict(position)


def simplify_debts(net_positions: dict[int, int]) -> list[tuple[int, int, int]]:
    """
    Greedy "minimum cash flow" settle-up: given each person's overall net
    position (from NetLedger.net_positions), return the smallest practical
    list of (from_user_id, to_user_id, amount) transactions that zeroes
    everyone out. This is the same well-known heuristic Splitwise-style
    apps use; it is not always the mathematically-perfect minimum in every
    edge case, but it is simple, fast, and always correct (fully settles
    the group).
    """
    creditors = [[uid, amt] for uid, amt in net_positions.items() if amt > 0]
    debtors = [[uid, -amt] for uid, amt in net_positions.items() if amt < 0]

    # deterministic ordering: biggest amounts first, tie-broken by id
    # (sorted as a string since ids can mix real integer user ids with
    # string pseudo-ids like "program:5")
    creditors.sort(key=lambda x: (-x[1], str(x[0])))
    debtors.sort(key=lambda x: (-x[1], str(x[0])))

    transactions = []
    i, j = 0, 0
    while i < len(debtors) and j < len(creditors):
        pay = min(debtors[i][1], creditors[j][1])
        if pay > 0:
            transactions.append((debtors[i][0], creditors[j][0], pay))
        debtors[i][1] -= pay
        creditors[j][1] -= pay
        if debtors[i][1] == 0:
            i += 1
        if creditors[j][1] == 0:
            j += 1
    return transactions
