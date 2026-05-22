"""
data_store.py
─────────────
SQLite-backed local buffer for historian data.

The scheduler writes here; the Streamlit app reads from here.
Using SQLite means zero extra infrastructure – the file lives
on the same server as the dashboard.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator

import pandas as pd

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS process_data (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    time        TEXT    NOT NULL,
    temperature REAL,
    pressure    REAL,
    flow_rate   REAL,
    batch       TEXT,
    shift       TEXT
);
CREATE INDEX IF NOT EXISTS idx_time ON process_data(time);
"""

_INSERT = """
INSERT INTO process_data (time, temperature, pressure, flow_rate, batch, shift)
VALUES (?, ?, ?, ?, ?, ?)
"""

_SELECT_SINCE = """
SELECT time AS Time, temperature AS Temperature, pressure AS Pressure,
       flow_rate AS FlowRate, batch AS Batch, shift AS Shift
FROM process_data
WHERE time >= ?
ORDER BY time
"""

_DELETE_OLD = """
DELETE FROM process_data WHERE time < ?
"""

_LATEST_TIME = """
SELECT MAX(time) FROM process_data
"""


class DataStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")  # allow concurrent reads
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_CREATE_TABLE)
        logger.info("DataStore initialised at %s", self.db_path)

    # ── Writes ────────────────────────────────────────────────────────────────

    def upsert(self, df: pd.DataFrame) -> int:
        """
        Insert rows from *df* that are newer than the latest stored timestamp.
        Returns the number of rows inserted.
        """
        if df.empty:
            return 0

        latest = self.latest_timestamp()

        # Normalise column names to lowercase for internal mapping
        col_map = {c: c.lower().replace("flowrate", "flow_rate") for c in df.columns}
        df = df.rename(columns=col_map)

        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], utc=True).dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        if latest is not None:
            df = df[df["time"] > latest.strftime("%Y-%m-%dT%H:%M:%S.%fZ")]

        if df.empty:
            return 0

        rows = [
            (
                row.get("time"),
                row.get("temperature"),
                row.get("pressure"),
                row.get("flow_rate"),
                row.get("batch"),
                row.get("shift"),
            )
            for row in df.to_dict(orient="records")
        ]

        with self._conn() as conn:
            conn.executemany(_INSERT, rows)

        logger.debug("Inserted %d rows into DataStore", len(rows))
        return len(rows)

    def purge_old(self, retention_hours: int) -> None:
        """Delete rows older than *retention_hours*."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=retention_hours)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with self._conn() as conn:
            conn.execute(_DELETE_OLD, (cutoff_str,))
        logger.debug("Purged rows older than %s", cutoff_str)

    # ── Reads ─────────────────────────────────────────────────────────────────

    def query(self, since_hours: int = 24) -> pd.DataFrame:
        """Return data from the last *since_hours* hours."""
        since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        since_str = since.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with self._conn() as conn:
            df = pd.read_sql_query(_SELECT_SINCE, conn, params=(since_str,))
        df["Time"] = pd.to_datetime(df["Time"], utc=True)
        # Drop optional columns if all null
        for col in ("Batch", "Shift"):
            if col in df.columns and df[col].isna().all():
                df.drop(columns=[col], inplace=True)
        return df

    def latest_timestamp(self) -> datetime | None:
        """Return the most recent timestamp stored, or None if empty."""
        with self._conn() as conn:
            cur = conn.execute(_LATEST_TIME)
            row = cur.fetchone()
        if row and row[0]:
            return datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        return None

    def row_count(self) -> int:
        with self._conn() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM process_data")
            return cur.fetchone()[0]
