# -*- coding: utf-8 -*-
"""
SQLite data-access layer for the group expense bot.

Design notes:
- Plain sqlite3 (no ORM) so the schema is easy to read, back up, and inspect
  by hand -- important for a small friend-group bot that someone non-technical
  might eventually need to peek into.
- Balances are always recomputed from the full history of expenses + confirmed
  payments (see reports.py / split_engine.py), never cached -- this guarantees
  the numbers are always consistent even if an old expense gets edited/deleted.
- Soft deletes everywhere (is_deleted / status='removed') so history and audit
  trail are preserved.
- weight_used and share_amount on expense_participants are a SNAPSHOT taken at
  the time the expense was recorded, so editing someone's household weight
  later never rewrites history.
"""
import sqlite3
from contextlib import contextmanager

import jalali_utils as ju

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id   INTEGER UNIQUE NOT NULL,
    username      TEXT,
    first_name    TEXT NOT NULL,
    card_number   TEXT,
    weight        INTEGER NOT NULL DEFAULT 1,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | active | removed
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS expenses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    payer_id     INTEGER NOT NULL REFERENCES users(id),
    creator_id   INTEGER NOT NULL REFERENCES users(id),  -- who actually entered it in the bot
    amount       INTEGER NOT NULL,
    description  TEXT NOT NULL,
    split_mode   TEXT NOT NULL DEFAULT 'weighted',       -- weighted | equal | custom
    jalali_date  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    is_deleted   INTEGER NOT NULL DEFAULT 0,
    receipt_file_id TEXT,   -- Telegram photo file_id of the uploaded receipt, if any
    receipt_text    TEXT    -- free-text receipt/note, if a photo wasn't sent
);

CREATE TABLE IF NOT EXISTS expense_participants (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_id    INTEGER NOT NULL REFERENCES expenses(id),
    user_id       INTEGER NOT NULL REFERENCES users(id),
    weight_used   INTEGER NOT NULL,
    share_amount  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id  INTEGER NOT NULL REFERENCES users(id),
    to_user_id    INTEGER NOT NULL REFERENCES users(id),
    amount        INTEGER NOT NULL,
    note          TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed | rejected
    jalali_date   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    is_deleted    INTEGER NOT NULL DEFAULT 0,
    receipt_file_id TEXT,   -- Telegram photo file_id of the uploaded receipt, if any
    receipt_text    TEXT    -- free-text receipt/note, if a photo wasn't sent
);
"""

# Columns added after the initial release. Each tuple is (table, column, sqlite_type).
# Applied with ALTER TABLE on every startup so existing deployments (e.g. already
# running on Railway with real data) pick up new columns without losing any data;
# "duplicate column" errors are simply ignored since that just means it already ran.
_MIGRATIONS = [
    ("expenses", "receipt_file_id", "TEXT"),
    ("expenses", "receipt_text", "TEXT"),
    ("payments", "receipt_file_id", "TEXT"),
    ("payments", "receipt_text", "TEXT"),
]


class Database:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as con:
            con.executescript(SCHEMA)
        self._run_migrations()

    def _run_migrations(self):
        with self._conn() as con:
            for table, column, col_type in _MIGRATIONS:
                try:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                except sqlite3.OperationalError as e:
                    if "duplicate column name" not in str(e).lower():
                        raise

    @contextmanager
    def _conn(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ---------------------------------------------------------------- users
    def get_user_by_telegram_id(self, telegram_id: int):
        with self._conn() as con:
            row = con.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
            return dict(row) if row else None

    def update_username(self, user_id: int, username: str | None):
        with self._conn() as con:
            con.execute("UPDATE users SET username = ? WHERE id = ?", (username, user_id))

    def get_user_by_id(self, user_id: int):
        with self._conn() as con:
            row = con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def any_admin_exists(self) -> bool:
        with self._conn() as con:
            row = con.execute("SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1").fetchone()
            return row is not None

    def create_pending_user(self, telegram_id: int, username: str | None, first_name: str) -> int:
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO users (telegram_id, username, first_name, weight, status, created_at) "
                "VALUES (?, ?, ?, 1, 'pending', ?)",
                (telegram_id, username, first_name, ju.jalali_date_str()),
            )
            return cur.lastrowid

    def set_user_registration_details(self, user_id: int, card_number: str, weight: int):
        with self._conn() as con:
            con.execute(
                "UPDATE users SET card_number = ?, weight = ? WHERE id = ?",
                (card_number, weight, user_id),
            )

    def activate_user(self, user_id: int, as_admin: bool = False):
        with self._conn() as con:
            con.execute(
                "UPDATE users SET status = 'active', is_admin = ? WHERE id = ?",
                (1 if as_admin else 0, user_id),
            )
            # if it was already admin, don't downgrade
            if not as_admin:
                con.execute("UPDATE users SET is_admin = is_admin WHERE id = ?", (user_id,))

    def reject_user(self, user_id: int):
        with self._conn() as con:
            con.execute("UPDATE users SET status = 'removed' WHERE id = ?", (user_id,))

    def remove_user(self, user_id: int):
        with self._conn() as con:
            con.execute("UPDATE users SET status = 'removed' WHERE id = ?", (user_id,))

    def restore_user(self, user_id: int):
        with self._conn() as con:
            con.execute("UPDATE users SET status = 'active' WHERE id = ?", (user_id,))

    def set_weight(self, user_id: int, weight: int):
        with self._conn() as con:
            con.execute("UPDATE users SET weight = ? WHERE id = ?", (weight, user_id))

    def set_card_number(self, user_id: int, card_number: str):
        with self._conn() as con:
            con.execute("UPDATE users SET card_number = ? WHERE id = ?", (card_number, user_id))

    def set_admin(self, user_id: int, is_admin: bool):
        with self._conn() as con:
            con.execute("UPDATE users SET is_admin = ? WHERE id = ?", (1 if is_admin else 0, user_id))

    def list_active_users(self):
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM users WHERE status = 'active' ORDER BY first_name COLLATE NOCASE"
            ).fetchall()
            return [dict(r) for r in rows]

    def list_pending_users(self):
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM users WHERE status = 'pending' ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def list_all_users(self):
        with self._conn() as con:
            rows = con.execute("SELECT * FROM users ORDER BY status, first_name COLLATE NOCASE").fetchall()
            return [dict(r) for r in rows]

    def list_admins(self):
        with self._conn() as con:
            rows = con.execute("SELECT * FROM users WHERE is_admin = 1 AND status = 'active'").fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------- settings
    def get_setting(self, key: str):
        with self._conn() as con:
            row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else None

    def set_setting(self, key: str, value: str):
        with self._conn() as con:
            con.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ------------------------------------------------------------- expenses
    def create_expense(self, payer_id: int, creator_id: int, amount: int, description: str,
                        split_mode: str, shares: dict[int, int], weights_used: dict[int, int]) -> int:
        now = ju.now_iran()
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO expenses (payer_id, creator_id, amount, description, split_mode, jalali_date, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (payer_id, creator_id, amount, description, split_mode, ju.jalali_date_str(now), now.isoformat()),
            )
            expense_id = cur.lastrowid
            for uid, share in shares.items():
                con.execute(
                    "INSERT INTO expense_participants (expense_id, user_id, weight_used, share_amount) "
                    "VALUES (?, ?, ?, ?)",
                    (expense_id, uid, weights_used.get(uid, 1), share),
                )
            return expense_id

    def delete_expense(self, expense_id: int):
        with self._conn() as con:
            con.execute("UPDATE expenses SET is_deleted = 1 WHERE id = ?", (expense_id,))

    def set_expense_receipt(self, expense_id: int, file_id: str | None = None, text: str | None = None):
        with self._conn() as con:
            con.execute(
                "UPDATE expenses SET receipt_file_id = ?, receipt_text = ? WHERE id = ?",
                (file_id, text, expense_id),
            )

    def get_expense(self, expense_id: int):
        with self._conn() as con:
            row = con.execute("SELECT * FROM expenses WHERE id = ?", (expense_id,)).fetchone()
            if not row:
                return None
            expense = dict(row)
            parts = con.execute(
                "SELECT ep.*, u.first_name FROM expense_participants ep "
                "JOIN users u ON u.id = ep.user_id WHERE ep.expense_id = ?",
                (expense_id,),
            ).fetchall()
            expense["participants"] = [dict(p) for p in parts]
            return expense

    def list_expenses(self, limit: int = 20, offset: int = 0, include_deleted: bool = False):
        clause = "" if include_deleted else "WHERE is_deleted = 0"
        with self._conn() as con:
            rows = con.execute(
                f"SELECT * FROM expenses {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            expenses = []
            for row in rows:
                expense = dict(row)
                parts = con.execute(
                    "SELECT ep.*, u.first_name FROM expense_participants ep "
                    "JOIN users u ON u.id = ep.user_id WHERE ep.expense_id = ?",
                    (expense["id"],),
                ).fetchall()
                expense["participants"] = [dict(p) for p in parts]
                expenses.append(expense)
            return expenses

    def count_expenses(self, include_deleted: bool = False) -> int:
        clause = "" if include_deleted else "WHERE is_deleted = 0"
        with self._conn() as con:
            row = con.execute(f"SELECT COUNT(*) AS c FROM expenses {clause}").fetchone()
            return row["c"]

    def all_expenses_with_participants(self):
        """Used by the balance/report engine -- only non-deleted expenses."""
        return self.list_expenses(limit=1_000_000_000, offset=0, include_deleted=False)

    # ------------------------------------------------------------- payments
    def create_payment(self, from_user_id: int, to_user_id: int, amount: int, note: str | None) -> int:
        now = ju.now_iran()
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO payments (from_user_id, to_user_id, amount, note, status, jalali_date, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (from_user_id, to_user_id, amount, note, ju.jalali_date_str(now), now.isoformat()),
            )
            return cur.lastrowid

    def get_payment(self, payment_id: int):
        with self._conn() as con:
            row = con.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            return dict(row) if row else None

    def set_payment_status(self, payment_id: int, status: str):
        with self._conn() as con:
            con.execute("UPDATE payments SET status = ? WHERE id = ?", (status, payment_id))

    def delete_payment(self, payment_id: int):
        with self._conn() as con:
            con.execute("UPDATE payments SET is_deleted = 1 WHERE id = ?", (payment_id,))

    def set_payment_receipt(self, payment_id: int, file_id: str | None = None, text: str | None = None):
        with self._conn() as con:
            con.execute(
                "UPDATE payments SET receipt_file_id = ?, receipt_text = ? WHERE id = ?",
                (file_id, text, payment_id),
            )

    def list_payments(self, limit: int = 20, offset: int = 0, include_deleted: bool = False):
        clause = "" if include_deleted else "WHERE is_deleted = 0"
        with self._conn() as con:
            rows = con.execute(
                f"SELECT * FROM payments {clause} ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def all_confirmed_payments(self):
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM payments WHERE status = 'confirmed' AND is_deleted = 0 ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def list_pending_payments_for_user(self, to_user_id: int):
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM payments WHERE to_user_id = ? AND status = 'pending' AND is_deleted = 0 ORDER BY id",
                (to_user_id,),
            ).fetchall()
            return [dict(r) for r in rows]
