"""
SQLite persistence layer.

Schema overview
---------------
products        – ASINs being monitored (name, URL)
sellers         – known sellers keyed by OID
offers          – time-series snapshots of every scrape (price, stock, fulfillment)
restock_events  – edge-triggered: fires when a seller flips to in_stock=1
                  notified=0 rows are picked up by the Discord notifier
"""

import sqlite3
from pathlib import Path
from typing import Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    asin        TEXT PRIMARY KEY,
    name        TEXT,
    url         TEXT,
    created_at  TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS sellers (
    seller_id   TEXT PRIMARY KEY,
    seller_name TEXT,
    first_seen  TEXT DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS offers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asin        TEXT    NOT NULL,
    seller_id   TEXT    NOT NULL,
    price_jpy   INTEGER,
    condition   TEXT,
    fulfillment TEXT,
    in_stock    INTEGER NOT NULL DEFAULT 1,
    scraped_at  TEXT    DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (asin)      REFERENCES products(asin),
    FOREIGN KEY (seller_id) REFERENCES sellers(seller_id)
);

CREATE TABLE IF NOT EXISTS restock_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asin        TEXT    NOT NULL,
    seller_id   TEXT    NOT NULL,
    price_jpy   INTEGER,
    condition   TEXT,
    fulfillment TEXT,
    detected_at TEXT    DEFAULT (datetime('now', 'localtime')),
    notified    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_offers_asin_seller  ON offers(asin, seller_id);
CREATE INDEX IF NOT EXISTS idx_offers_scraped_at   ON offers(scraped_at);
CREATE INDEX IF NOT EXISTS idx_restock_notified    ON restock_events(notified);
"""


class Database:
    def __init__(self, db_path: str = "amz_jp.db"):
        self.db_path = Path(db_path)
        self._init()

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    def upsert_product(self, asin: str, name: Optional[str] = None, url: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO products (asin, name, url) VALUES (?, ?, ?)",
                (asin, name, url),
            )
            if name:
                conn.execute("UPDATE products SET name=? WHERE asin=?", (name, asin))

    def get_product(self, asin: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM products WHERE asin=?", (asin,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Sellers
    # ------------------------------------------------------------------

    def upsert_seller(self, seller_id: str, seller_name: Optional[str] = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sellers (seller_id, seller_name) VALUES (?, ?)",
                (seller_id, seller_name or "Unknown"),
            )
            if seller_name and seller_name != "Unknown":
                conn.execute(
                    "UPDATE sellers SET seller_name=? WHERE seller_id=?",
                    (seller_name, seller_id),
                )

    def all_sellers(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM sellers ORDER BY first_seen").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Offers
    # ------------------------------------------------------------------

    def save_offer(self, asin: str, seller_id: str, price_jpy: Optional[int],
                   condition: str, fulfillment: str, in_stock: bool) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO offers (asin, seller_id, price_jpy, condition, fulfillment, in_stock)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (asin, seller_id, price_jpy, condition, fulfillment, 1 if in_stock else 0),
            )

    def get_last_offer(self, asin: str, seller_id: str) -> Optional[dict]:
        """Return the most recent offer snapshot for this asin+seller pair."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT in_stock, price_jpy, scraped_at FROM offers
                   WHERE asin=? AND seller_id=?
                   ORDER BY scraped_at DESC LIMIT 1""",
                (asin, seller_id),
            ).fetchone()
        return dict(row) if row else None

    def offers_for_asin(self, asin: str, limit: int = 500) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT o.*, s.seller_name FROM offers o
                   JOIN sellers s ON o.seller_id = s.seller_id
                   WHERE o.asin=? ORDER BY o.scraped_at DESC LIMIT ?""",
                (asin, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Restock events
    # ------------------------------------------------------------------

    def record_restock(self, asin: str, seller_id: str, price_jpy: Optional[int],
                       condition: str, fulfillment: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO restock_events (asin, seller_id, price_jpy, condition, fulfillment)
                   VALUES (?, ?, ?, ?, ?)""",
                (asin, seller_id, price_jpy, condition, fulfillment),
            )
            return cur.lastrowid

    def pending_notifications(self) -> list[dict]:
        """Return restock events that have not yet been sent to Discord."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT r.*, p.name AS product_name, s.seller_name
                   FROM restock_events r
                   JOIN products p ON r.asin = p.asin
                   JOIN sellers  s ON r.seller_id = s.seller_id
                   WHERE r.notified = 0
                   ORDER BY r.detected_at""",
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_notified(self, restock_id: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE restock_events SET notified=1 WHERE id=?", (restock_id,))

    # ------------------------------------------------------------------
    # Analytics helpers
    # ------------------------------------------------------------------

    def restock_history(self, asin: str, seller_id: Optional[str] = None) -> list[dict]:
        query = """SELECT r.*, s.seller_name FROM restock_events r
                   JOIN sellers s ON r.seller_id = s.seller_id
                   WHERE r.asin=?"""
        args: list = [asin]
        if seller_id:
            query += " AND r.seller_id=?"
            args.append(seller_id)
        query += " ORDER BY r.detected_at DESC"
        with self._conn() as conn:
            rows = conn.execute(query, args).fetchall()
        return [dict(r) for r in rows]
