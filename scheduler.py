"""
scheduler.py
────────────
Stand-alone background process that periodically pulls data from the historian
and writes it to the local SQLite buffer.  Run this separately from Streamlit:

    python scheduler.py

It is intentionally decoupled from the Streamlit process so that:
  • A Streamlit crash/restart does not interrupt data collection.
  • Multiple dashboard instances can share the same buffer.
  • The collection interval is independent of the display refresh rate.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from alerting import AlertEngine
from data_store import DataStore
from historian_connector import build_connector

# ── Re-use SPC logic from the main app ───────────────────────────────────────
# Import the pure-logic helpers (no Streamlit dependency)
from spc import compute_spc_metrics, detect_violations

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scheduler.log"),
    ],
)
logger = logging.getLogger("scheduler")

_CONFIG_FILE = Path(__file__).parent / "config.yaml"

_running = True


def _handle_signal(signum: int, _frame: object) -> None:
    global _running
    logger.info("Signal %d received — shutting down gracefully …", signum)
    _running = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(_CONFIG_FILE, "r") as f:
        return yaml.safe_load(f)


def harvest_once(
    connector: object,
    store: DataStore,
    alert_engine: AlertEngine,
    rules: list[str],
    retention_hours: int,
) -> None:
    """One harvest cycle: fetch → store → purge → check violations → alert."""
    try:
        latest = store.latest_timestamp()
        logger.info("Fetching data since %s …", latest)
        df = connector.fetch(since=latest)

        if df.empty:
            logger.info("No new data returned.")
            return

        inserted = store.upsert(df)
        logger.info("Inserted %d new rows (buffer total: %d)", inserted, store.row_count())

        store.purge_old(retention_hours)

        # ── Evaluate SPC violations on the fresh slice ───────────────────────
        for variable in ("Temperature", "Pressure", "FlowRate"):
            if variable not in df.columns:
                continue
            metrics = compute_spc_metrics(df[variable])
            violations_df = detect_violations(df.reset_index(drop=True), variable, rules)
            alert_engine.evaluate_and_alert(variable, violations_df, metrics)

    except Exception as exc:
        logger.error("Harvest error: %s", exc, exc_info=True)


def main() -> None:
    cfg = load_config()
    historian_cfg = cfg.get("historian", {})
    sched_cfg = cfg.get("scheduler", {})
    alert_cfg = cfg.get("alerting", {})

    interval = int(sched_cfg.get("interval_seconds", 60))
    retention = int(sched_cfg.get("retention_hours", 72))
    db_path = sched_cfg.get("db_path", "process_buffer.db")
    default_rules: list[str] = cfg.get("display", {}).get("default_rules", ["3-sigma"])

    connector = build_connector(historian_cfg)
    store = DataStore(db_path)
    alert_engine = AlertEngine(alert_cfg)

    logger.info(
        "Scheduler started — historian=%s  interval=%ds  db=%s",
        historian_cfg.get("type"),
        interval,
        db_path,
    )

    while _running:
        cycle_start = time.monotonic()
        harvest_once(connector, store, alert_engine, default_rules, retention)
        elapsed = time.monotonic() - cycle_start
        sleep_for = max(0.0, interval - elapsed)
        logger.info("Next harvest in %.0fs", sleep_for)

        # Sleep in short chunks so signal handling stays responsive
        slept = 0.0
        while slept < sleep_for and _running:
            time.sleep(min(1.0, sleep_for - slept))
            slept += 1.0

    logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
