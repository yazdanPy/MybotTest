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
import os
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
    receipt_text    TEXT,   -- free-text receipt/note, if a photo wasn't sent
    program_id      INTEGER REFERENCES programs(id),   -- set if this expense belongs to a long-term program
    approval_status TEXT NOT NULL DEFAULT 'approved'   -- approved | pending | rejected (program expenses start pending)
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

-- A "long-term program" (e.g. a multi-day trip) with its own dedicated "mother
-- card". The mother card belongs to no single person -- it's the program's own
-- pool of money -- so any leftover surplus/shortfall against it resolves
-- against the group admin (who approves everything in the program) rather than
-- a designated "treasurer" person.
CREATE TABLE IF NOT EXISTS programs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    mother_card  TEXT NOT NULL,
    creator_id   INTEGER NOT NULL REFERENCES users(id),
    status       TEXT NOT NULL DEFAULT 'active',   -- active | closed
    jalali_date  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    closed_at    TEXT
);

CREATE TABLE IF NOT EXISTS program_participants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    program_id  INTEGER NOT NULL REFERENCES programs(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    weight      INTEGER NOT NULL DEFAULT 1   -- default per-program headcount, same idea as users.weight
);

-- Money a participant puts INTO a program's mother card ("شارژ"). Separate
-- from `payments` (which is always person-to-person) because a charge has no
-- individual recipient to confirm receipt -- the group admin confirms it
-- instead, on behalf of the program.
CREATE TABLE IF NOT EXISTS program_charges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    program_id    INTEGER NOT NULL REFERENCES programs(id),
    user_id       INTEGER NOT NULL REFERENCES users(id),
    amount        INTEGER NOT NULL,
    note          TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed | rejected
    jalali_date   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    is_deleted    INTEGER NOT NULL DEFAULT 0,
    receipt_file_id TEXT,
    receipt_text    TEXT
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
    ("expenses", "program_id", "INTEGER"),
    ("expenses", "approval_status", "TEXT"),
]

# New TABLES added after the initial release also need to be (re)created on an
# existing database, since executescript's CREATE TABLE IF NOT EXISTS only runs
# against whatever schema string is currently in this file -- but since SCHEMA
# already lists them with IF NOT EXISTS, simply re-running it (done in
# __init__ on every startup) is sufficient; no separate table-creation step
# is needed here. Only column-level ALTERs need this explicit migration list.


class Database:
    def __init__(self, path: str):
        self.path = path
        # If DB_PATH points into a directory that doesn't exist yet (e.g. a
        # Railway Volume that failed to attach, or simply the very first boot
        # before anything has been written), create it instead of crashing.
        # This does NOT replace needing a real persistent Volume mounted at
        # that path -- without one, this directory is still wiped on every
        # redeploy -- it just stops a missing folder from crash-looping the bot.
        db_dir = os.path.dirname(os.path.abspath(path))
        if db_dir:
            try:
                os.makedirs(db_dir, exist_ok=True)
            except OSError as e:
                raise RuntimeError(
                    f"نتونستم پوشه‌ی دیتابیس رو بسازم: {db_dir} ({e}). "
                    "اگه روی Railway هستی، مطمئن شو یک Volume با Mount Path دقیقاً "
                    "همون مسیر ساخته و به این سرویس وصل شده."
                ) from e
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
                        split_mode: str, shares: dict[int, int], weights_used: dict[int, int],
                        program_id: int | None = None, approval_status: str = "approved") -> int:
        now = ju.now_iran()
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO expenses (payer_id, creator_id, amount, description, split_mode, "
                "jalali_date, created_at, program_id, approval_status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (payer_id, creator_id, amount, description, split_mode, ju.jalali_date_str(now),
                 now.isoformat(), program_id, approval_status),
            )
            expense_id = cur.lastrowid
            for uid, share in shares.items():
                con.execute(
                    "INSERT INTO expense_participants (expense_id, user_id, weight_used, share_amount) "
                    "VALUES (?, ?, ?, ?)",
                    (expense_id, uid, weights_used.get(uid, 1), share),
                )
            return expense_id

    def set_expense_approval(self, expense_id: int, status: str):
        with self._conn() as con:
            con.execute("UPDATE expenses SET approval_status = ? WHERE id = ?", (status, expense_id))

    def list_pending_program_expenses(self, program_id: int | None = None):
        with self._conn() as con:
            if program_id is not None:
                rows = con.execute(
                    "SELECT * FROM expenses WHERE approval_status = 'pending' AND is_deleted = 0 "
                    "AND program_id = ? ORDER BY id",
                    (program_id,),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM expenses WHERE approval_status = 'pending' AND is_deleted = 0 "
                    "AND program_id IS NOT NULL ORDER BY id"
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
        """Used by the balance/report engine -- only non-deleted, APPROVED expenses.
        Program expenses start as 'pending' and don't affect balances until a
        group admin approves them; regular (non-program) expenses are always
        created as 'approved' already, so this never affects them."""
        all_expenses = self.list_expenses(limit=1_000_000_000, offset=0, include_deleted=False)
        return [e for e in all_expenses if (e.get("approval_status") or "approved") == "approved"]

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

    # ------------------------------------------------------------- programs
    def create_program(self, name: str, mother_card: str, creator_id: int, weights: dict[int, int]) -> int:
        """weights: {user_id: per-program headcount} for every selected participant."""
        now = ju.now_iran()
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO programs (name, mother_card, creator_id, jalali_date, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, mother_card, creator_id, ju.jalali_date_str(now), now.isoformat()),
            )
            program_id = cur.lastrowid
            for uid, weight in weights.items():
                con.execute(
                    "INSERT INTO program_participants (program_id, user_id, weight) VALUES (?, ?, ?)",
                    (program_id, uid, weight),
                )
            return program_id

    def get_program(self, program_id: int):
        with self._conn() as con:
            row = con.execute("SELECT * FROM programs WHERE id = ?", (program_id,)).fetchone()
            if not row:
                return None
            program = dict(row)
            parts = con.execute(
                "SELECT pp.*, u.first_name, u.telegram_id FROM program_participants pp "
                "JOIN users u ON u.id = pp.user_id WHERE pp.program_id = ?",
                (program_id,),
            ).fetchall()
            program["participants"] = [dict(p) for p in parts]
            return program

    def list_active_programs(self):
        with self._conn() as con:
            rows = con.execute("SELECT * FROM programs WHERE status = 'active' ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]

    def list_programs_for_user(self, user_id: int, active_only: bool = True):
        clause = "AND p.status = 'active'" if active_only else ""
        with self._conn() as con:
            rows = con.execute(
                f"SELECT p.* FROM programs p JOIN program_participants pp ON pp.program_id = p.id "
                f"WHERE pp.user_id = ? {clause} ORDER BY p.id DESC",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def is_program_participant(self, program_id: int, user_id: int) -> bool:
        with self._conn() as con:
            row = con.execute(
                "SELECT 1 FROM program_participants WHERE program_id = ? AND user_id = ?",
                (program_id, user_id),
            ).fetchone()
            return row is not None

    def get_program_participant_weight(self, program_id: int, user_id: int) -> int:
        with self._conn() as con:
            row = con.execute(
                "SELECT weight FROM program_participants WHERE program_id = ? AND user_id = ?",
                (program_id, user_id),
            ).fetchone()
            return row["weight"] if row else 1

    def close_program(self, program_id: int):
        with self._conn() as con:
            con.execute(
                "UPDATE programs SET status = 'closed', closed_at = ? WHERE id = ?",
                (ju.now_iran().isoformat(), program_id),
            )

    # ------------------------------------------------------- program charges
    def create_program_charge(self, program_id: int, user_id: int, amount: int, note: str | None) -> int:
        now = ju.now_iran()
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO program_charges (program_id, user_id, amount, note, status, jalali_date, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
                (program_id, user_id, amount, note, ju.jalali_date_str(now), now.isoformat()),
            )
            return cur.lastrowid

    def get_program_charge(self, charge_id: int):
        with self._conn() as con:
            row = con.execute("SELECT * FROM program_charges WHERE id = ?", (charge_id,)).fetchone()
            return dict(row) if row else None

    def set_program_charge_status(self, charge_id: int, status: str):
        with self._conn() as con:
            con.execute("UPDATE program_charges SET status = ? WHERE id = ?", (status, charge_id))

    def set_program_charge_receipt(self, charge_id: int, file_id: str | None = None, text: str | None = None):
        with self._conn() as con:
            con.execute(
                "UPDATE program_charges SET receipt_file_id = ?, receipt_text = ? WHERE id = ?",
                (file_id, text, charge_id),
            )

    def list_program_charges(self, program_id: int, include_deleted: bool = False):
        clause = "" if include_deleted else "AND is_deleted = 0"
        with self._conn() as con:
            rows = con.execute(
                f"SELECT * FROM program_charges WHERE program_id = ? {clause} ORDER BY id", (program_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    def list_pending_program_charges(self, program_id: int | None = None):
        clause = "AND program_id = ?" if program_id is not None else ""
        params = (program_id,) if program_id is not None else ()
        with self._conn() as con:
            rows = con.execute(
                f"SELECT * FROM program_charges WHERE status = 'pending' AND is_deleted = 0 {clause} ORDER BY id",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def all_confirmed_program_charges(self):
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM program_charges WHERE status = 'confirmed' AND is_deleted = 0 ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]

    def all_program_expenses(self, program_id: int):
        """Approved, non-deleted expenses tied to one program -- used for the program's own report."""
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM expenses WHERE program_id = ? AND is_deleted = 0 AND approval_status = 'approved' "
                "ORDER BY id",
                (program_id,),
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
