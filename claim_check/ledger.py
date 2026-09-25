"""SQLite ledger: quotes, checks, receipts, vouchers, revenue, used nonces."""
import json
import sqlite3
import time

from . import config


def _conn():
    c = sqlite3.connect(str(config.DB_PATH))
    c.row_factory = sqlite3.Row
    return c


def init():
    c = _conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS quotes(
            id TEXT PRIMARY KEY, kind TEXT, claim TEXT, params TEXT,
            price_usd REAL, price_units INTEGER, created INTEGER,
            expires INTEGER, used INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS checks(
            id TEXT PRIMARY KEY, quote_id TEXT, kind TEXT, payer TEXT,
            tx_hash TEXT, settlement TEXT, verdict TEXT,
            claimed_bps REAL, measured_bps REAL, delta_bps REAL,
            created INTEGER);
        CREATE TABLE IF NOT EXISTS receipts(
            hash TEXT PRIMARY KEY, check_id TEXT,
            receipt_json TEXT, fulfillment_json TEXT, created INTEGER);
        CREATE TABLE IF NOT EXISTS vouchers(
            id TEXT PRIMARY KEY, credits INTEGER, created INTEGER);
        CREATE TABLE IF NOT EXISTS revenue(
            check_id TEXT PRIMARY KEY, amount_usd REAL, created INTEGER);
        CREATE TABLE IF NOT EXISTS nonces(nonce TEXT PRIMARY KEY);
        """
    )
    c.commit()
    c.close()


def now() -> int:
    return int(time.time())


def save_quote(qid, kind, claim, params, price_usd, price_units, expires):
    c = _conn()
    c.execute(
        "INSERT INTO quotes VALUES (?,?,?,?,?,?,?,?,0)",
        (qid, kind, str(claim), json.dumps(params), price_usd, price_units,
         now(), expires),
    )
    c.commit()
    c.close()


def get_quote(qid):
    c = _conn()
    r = c.execute("SELECT * FROM quotes WHERE id=?", (qid,)).fetchone()
    c.close()
    return r


def mark_quote_used(qid):
    c = _conn()
    c.execute("UPDATE quotes SET used=1 WHERE id=?", (qid,))
    c.commit()
    c.close()


def save_check(cid, quote_id, kind, payer, tx_hash, settlement, verdict,
               claimed_bps, measured_bps, delta_bps, amount_usd):
    c = _conn()
    c.execute(
        "INSERT INTO checks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (cid, quote_id, kind, payer, tx_hash, settlement, verdict,
         claimed_bps, measured_bps, delta_bps, now()),
    )
    c.execute(
        "INSERT OR IGNORE INTO revenue VALUES (?,?,?)",
        (cid, amount_usd, now()),
    )
    c.commit()
    c.close()


def save_receipt(rhash, check_id, receipt_json, fulfillment_json):
    c = _conn()
    c.execute(
        "INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?)",
        (rhash, check_id, receipt_json, fulfillment_json, now()),
    )
    c.commit()
    c.close()


def get_receipt(rhash):
    c = _conn()
    r = c.execute("SELECT * FROM receipts WHERE hash=?", (rhash,)).fetchone()
    c.close()
    return r


def nonce_used(nonce) -> bool:
    c = _conn()
    r = c.execute("SELECT 1 FROM nonces WHERE nonce=?", (nonce,)).fetchone()
    c.close()
    return r is not None


def mark_nonce(nonce):
    c = _conn()
    c.execute("INSERT OR IGNORE INTO nonces VALUES (?)", (nonce,))
    c.commit()
    c.close()


def create_voucher(vid, credits):
    c = _conn()
    c.execute("INSERT INTO vouchers VALUES (?,?,?)", (vid, credits, now()))
    c.commit()
    c.close()


def get_voucher(vid):
    c = _conn()
    r = c.execute("SELECT * FROM vouchers WHERE id=?", (vid,)).fetchone()
    c.close()
    return r


def spend_voucher(vid) -> bool:
    c = _conn()
    r = c.execute("SELECT credits FROM vouchers WHERE id=?", (vid,)).fetchone()
    if not r or r["credits"] <= 0:
        c.close()
        return False
    c.execute("UPDATE vouchers SET credits=credits-1 WHERE id=?", (vid,))
    c.commit()
    c.close()
    return True


def revenue_total() -> float:
    c = _conn()
    r = c.execute("SELECT COALESCE(SUM(amount_usd),0) AS t FROM revenue").fetchone()
    c.close()
    return float(r["t"])


init()
