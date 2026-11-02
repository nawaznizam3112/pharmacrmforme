"""SQLite layer. Swap for Postgres (e.g. Supabase) when deploying to production."""
import sqlite3
from pathlib import Path

DB_PATH = Path("data/pharmawh.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, gstin TEXT, state TEXT, phone TEXT, drug_license TEXT);
CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, name TEXT NOT NULL, gstin TEXT, state TEXT, phone TEXT, drug_license TEXT, credit_limit REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, generic TEXT, manufacturer TEXT, hsn TEXT, gst_rate REAL DEFAULT 12, schedule TEXT DEFAULT 'OTC', pack TEXT, reorder_level INTEGER DEFAULT 0, cold_chain INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id), batch_no TEXT NOT NULL, expiry TEXT NOT NULL, qty INTEGER NOT NULL DEFAULT 0 CHECK(qty>=0), cost REAL, ptr REAL, mrp REAL, supplier_id INTEGER, UNIQUE(product_id,batch_no));
CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY, supplier_id INTEGER, invoice_no TEXT, date TEXT, total REAL);
CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY, invoice_no TEXT UNIQUE, customer_id INTEGER, date TEXT, subtotal REAL, tax REAL, total REAL, paid REAL DEFAULT 0, interstate INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS sale_items(id INTEGER PRIMARY KEY, sale_id INTEGER, product_id INTEGER, batch_id INTEGER, qty INTEGER, rate REAL, disc_pct REAL, gst_rate REAL, amount REAL);
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, product_id INTEGER, batch_id INTEGER, change INTEGER, reason TEXT, ref TEXT);
CREATE INDEX IF NOT EXISTS ix_batches_prod ON batches(product_id, expiry);
"""


def conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)


def q(sql, params=()):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params)]
