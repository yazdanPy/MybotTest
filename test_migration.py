# -*- coding: utf-8 -*-
"""
Verifies that upgrading an EXISTING database (e.g. the one already running in
production on Railway with real friends' data) to a newer version of this bot
never loses data, even when new columns are added to the schema over time.
"""
import os
import sqlite3

DB_PATH = "/tmp/test_migration.db"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

failures = []


def check(name, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {name}")
    if not condition:
        failures.append(name)


# Step 1: simulate an OLD deployment's database (schema WITHOUT the receipt columns
# that were added later for the receipt-attachment feature).
OLD_SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER UNIQUE NOT NULL, username TEXT, first_name TEXT NOT NULL, card_number TEXT, weight INTEGER NOT NULL DEFAULT 1, is_admin INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE expenses (id INTEGER PRIMARY KEY AUTOINCREMENT, payer_id INTEGER NOT NULL, creator_id INTEGER NOT NULL, amount INTEGER NOT NULL, description TEXT NOT NULL, split_mode TEXT NOT NULL DEFAULT 'weighted', jalali_date TEXT NOT NULL, created_at TEXT NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE expense_participants (id INTEGER PRIMARY KEY AUTOINCREMENT, expense_id INTEGER NOT NULL, user_id INTEGER NOT NULL, weight_used INTEGER NOT NULL, share_amount INTEGER NOT NULL);
CREATE TABLE payments (id INTEGER PRIMARY KEY AUTOINCREMENT, from_user_id INTEGER NOT NULL, to_user_id INTEGER NOT NULL, amount INTEGER NOT NULL, note TEXT, status TEXT NOT NULL DEFAULT 'pending', jalali_date TEXT NOT NULL, created_at TEXT NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0);
"""
con = sqlite3.connect(DB_PATH)
con.executescript(OLD_SCHEMA)
con.execute(
    "INSERT INTO users (telegram_id, username, first_name, card_number, weight, is_admin, status, created_at) "
    "VALUES (999, 'olduser', 'کاربر قدیمی', '6037000000000000', 1, 1, 'active', '1405/01/01')"
)
con.commit()
con.close()

# Step 2: open it with the REAL Database class (simulating redeploying updated
# bot code against the old db file, exactly like a Railway redeploy would).
from database import Database

db = Database(DB_PATH)

# Step 3: verify old data survived.
existing = db.get_user_by_telegram_id(999)
check("pre-existing user data survived migration", existing is not None and existing["first_name"] == "کاربر قدیمی")

# Step 4: verify new columns actually exist and work.
uid = existing["id"]
exp_id = db.create_expense(uid, uid, 100_000, "تست", "equal", {uid: 100_000}, {uid: 1})
db.set_expense_receipt(exp_id, file_id="FAKE_FILE_ID_123", text=None)
exp = db.get_expense(exp_id)
check("new receipt_file_id column works on a migrated table", exp["receipt_file_id"] == "FAKE_FILE_ID_123")

pay_id = db.create_payment(uid, uid, 5_000, None)
db.set_payment_receipt(pay_id, file_id=None, text="یادداشت تستی رسید")
pay = db.get_payment(pay_id)
check("new receipt_text column works on a migrated payments table", pay["receipt_text"] == "یادداشت تستی رسید")

# Step 5: run migration AGAIN (simulating a second redeploy) -- must be idempotent.
try:
    db2 = Database(DB_PATH)
    check("running migrations a second time did not raise (idempotent)", True)
except Exception as e:
    check(f"running migrations a second time did not raise (idempotent) -- got {e}", False)

os.remove(DB_PATH)

print()
if failures:
    print(f"*** {len(failures)} TEST(S) FAILED: {failures}")
    raise SystemExit(1)
else:
    print("ALL MIGRATION TESTS PASSED")
