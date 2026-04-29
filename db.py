"""SQLite persistence for BillBot — tracks processed bills and Splitwise postings."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BILLBOT_DIR = Path.home() / ".billbot"
DEFAULT_DB_PATH = BILLBOT_DIR / "history.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_message_id TEXT UNIQUE NOT NULL,
    provider TEXT,
    amount_due REAL,
    bill_period_start TEXT,
    bill_period_end TEXT,
    pdf_path TEXT,
    tenant_shares_json TEXT,
    parse_result_json TEXT,
    processed_at TEXT NOT NULL,
    splitwise_expense_id TEXT,
    splitwise_posted_at TEXT
);
"""


def init_db(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)
    conn.commit()
    return conn


def is_processed(conn: sqlite3.Connection, email_message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM bills WHERE email_message_id = ?", (email_message_id,)
    ).fetchone()
    return row is not None


def save_parsed(
    conn: sqlite3.Connection,
    email_message_id: str,
    provider: Optional[str],
    amount_due: float,
    bill_period_start: Optional[str],
    bill_period_end: Optional[str],
    pdf_path: Optional[str],
    tenant_shares: object,
    parse_result: object,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """INSERT INTO bills
           (email_message_id, provider, amount_due, bill_period_start, bill_period_end,
            pdf_path, tenant_shares_json, parse_result_json, processed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            email_message_id,
            provider,
            amount_due,
            bill_period_start,
            bill_period_end,
            pdf_path,
            json.dumps(tenant_shares, default=str),
            json.dumps(parse_result, default=str),
            now,
        ),
    )
    conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


def mark_posted(
    conn: sqlite3.Connection,
    email_message_id: str,
    splitwise_expense_id: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE bills SET splitwise_expense_id = ?, splitwise_posted_at = ?
           WHERE email_message_id = ?""",
        (splitwise_expense_id, now, email_message_id),
    )
    conn.commit()


def get_latest_date(conn: sqlite3.Connection) -> Optional[str]:
    """Return the most recent processed_at date, or None if DB is empty."""
    row = conn.execute(
        "SELECT MAX(processed_at) as latest FROM bills"
    ).fetchone()
    if row and row["latest"]:
        return row["latest"]
    return None


def get_unposted(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM bills WHERE splitwise_expense_id IS NULL ORDER BY processed_at"
    ).fetchall()


def get_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM bills ORDER BY processed_at DESC").fetchall()
