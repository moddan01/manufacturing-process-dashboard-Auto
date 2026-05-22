"""
historian_connector.py
──────────────────────
Pluggable adapters for different data historian backends.

Supported:
  csv        – local CSV file (dev / fallback)
  odbc       – any ODBC source (SQL Server, Oracle, Aspen IP21, …)
  osisoft_pi – OSIsoft PI via PI Web API REST
  opcua      – OPC Unified Architecture server
  rest       – generic JSON REST API
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ["Time", "Temperature", "Pressure", "FlowRate"]


# ─────────────────────────────────────────────────────────────────────────────
# Base class
# ─────────────────────────────────────────────────────────────────────────────

class HistorianConnector(ABC):
    """Fetch process data and return a validated DataFrame."""

    @abstractmethod
    def fetch(self, since: datetime | None = None) -> pd.DataFrame:
        """Return rows newer than *since* (UTC).  Full dataset if None."""

    def _validate(self, df: pd.DataFrame) -> pd.DataFrame:
        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"Historian data missing columns: {missing}")
        df["Time"] = pd.to_datetime(df["Time"], utc=True)
        return df


# ─────────────────────────────────────────────────────────────────────────────
# CSV adapter (dev / fallback)
# ─────────────────────────────────────────────────────────────────────────────

class CsvConnector(HistorianConnector):
    def __init__(self, csv_path: str) -> None:
        self.csv_path = csv_path

    def fetch(self, since: datetime | None = None) -> pd.DataFrame:
        df = pd.read_csv(self.csv_path)
        df = self._validate(df)
        if since is not None:
            since = since.replace(tzinfo=timezone.utc) if since.tzinfo is None else since
            df = df[df["Time"] > since]
        return df.reset_index(drop=True)


# ─────────────────────────────────────────────────────────────────────────────
# ODBC adapter  (pip install pyodbc)
# ─────────────────────────────────────────────────────────────────────────────

class OdbcConnector(HistorianConnector):
    """
    Works with any ODBC-accessible historian: SQL Server, Oracle,
    Aspen InfoPlus.21, Honeywell PHD, etc.

    The *query* must accept exactly one parameter: the lower-bound timestamp.
    Example query (SQL Server):
        SELECT timestamp AS Time, Temperature, Pressure, FlowRate
        FROM dbo.ProcessTag
        WHERE timestamp >= ?
        ORDER BY timestamp
    """

    def __init__(self, connection_string: str, query: str) -> None:
        self.connection_string = connection_string
        self.query = query

    def fetch(self, since: datetime | None = None) -> pd.DataFrame:
        try:
            import pyodbc  # type: ignore
        except ImportError:
            raise ImportError("pyodbc not installed. Run: pip install pyodbc")

        since = since or (datetime.now(timezone.utc) - timedelta(hours=1))
        conn = pyodbc.connect(self.connection_string, timeout=10)
        df = pd.read_sql(self.query, conn, params=[since])
        conn.close()
        return self._validate(df)


# ─────────────────────────────────────────────────────────────────────────────
# OSIsoft PI Web API adapter  (no extra package – uses requests)
# ─────────────────────────────────────────────────────────────────────────────

class OsisoftPiConnector(HistorianConnector):
    """
    Reads recorded values from PI via PI Web API.

    Config keys used:
        base_url    – https://pi-server/piwebapi
        username    – PI Web API user
        password    – PI Web API password (or env: PI_PASSWORD)
        verify_ssl  – True/False
        tags        – mapping of column name → PI tag path
                      e.g. Temperature: PI:TAG.TEMPERATURE
    """

    def __init__(self, base_url: str, username: str, password: str,
                 tags: dict[str, str], verify_ssl: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = (username, os.environ.get("PI_PASSWORD", password))
        self.tags = tags  # {column_name: pi_tag_name}
        self.verify_ssl = verify_ssl

    def _get_web_id(self, session: Any, tag: str) -> str:
        url = f"{self.base_url}/points?nameFilter={tag}&selectedFields=WebId"
        r = session.get(url, verify=self.verify_ssl, timeout=15)
        r.raise_for_status()
        items = r.json().get("Items", [])
        if not items:
            raise ValueError(f"PI tag not found: {tag}")
        return items[0]["WebId"]

    def fetch(self, since: datetime | None = None) -> pd.DataFrame:
        try:
            import requests
        except ImportError:
            raise ImportError("requests not installed. Run: pip install requests")

        since = since or (datetime.now(timezone.utc) - timedelta(hours=1))
        end = datetime.now(timezone.utc)
        start_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end.strftime("%Y-%m-%dT%H:%M:%SZ")

        session = requests.Session()
        session.auth = self.auth

        frames: list[pd.Series] = []
        time_index: pd.DatetimeIndex | None = None

        for col, tag in self.tags.items():
            web_id = self._get_web_id(session, tag)
            url = (
                f"{self.base_url}/streams/{web_id}/recorded"
                f"?startTime={start_str}&endTime={end_str}&maxCount=10000"
                f"&selectedFields=Items.Timestamp;Items.Value"
            )
            r = session.get(url, verify=self.verify_ssl, timeout=30)
            r.raise_for_status()
            items = r.json().get("Items", [])
            timestamps = [i["Timestamp"] for i in items]
            values = [i["Value"] if isinstance(i["Value"], (int, float)) else None for i in items]
            s = pd.Series(values, index=pd.to_datetime(timestamps, utc=True), name=col)
            frames.append(s)
            if time_index is None:
                time_index = s.index

        df = pd.concat(frames, axis=1).reset_index()
        df.rename(columns={"index": "Time"}, inplace=True)
        return self._validate(df)


# ─────────────────────────────────────────────────────────────────────────────
# OPC-UA adapter  (pip install opcua  or  asyncua)
# ─────────────────────────────────────────────────────────────────────────────

class OpcUaConnector(HistorianConnector):
    """
    Reads the *current* value of each configured node from an OPC-UA server
    and appends a timestamped row.  For historical reads your server must
    support OPC-UA HistoricalData; swap read_data_value → read_raw_history_data.
    """

    def __init__(self, endpoint: str, username: str, password: str,
                 node_ids: dict[str, str]) -> None:
        self.endpoint = endpoint
        self.username = username
        self.password = os.environ.get("OPC_PASSWORD", password)
        self.node_ids = node_ids  # {column_name: node_id_string}

    def fetch(self, since: datetime | None = None) -> pd.DataFrame:
        try:
            from opcua import Client  # type: ignore
        except ImportError:
            raise ImportError("opcua not installed. Run: pip install opcua")

        client = Client(self.endpoint)
        client.set_user(self.username)
        client.set_password(self.password)

        row: dict[str, Any] = {"Time": datetime.now(timezone.utc)}
        try:
            client.connect()
            for col, node_id in self.node_ids.items():
                node = client.get_node(node_id)
                row[col] = node.get_value()
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

        return self._validate(pd.DataFrame([row]))


# ─────────────────────────────────────────────────────────────────────────────
# Generic REST adapter
# ─────────────────────────────────────────────────────────────────────────────

class RestConnector(HistorianConnector):
    """
    Hits a JSON REST endpoint that returns an array of records with at least
    the REQUIRED_COLUMNS.  Customize *params* or auth headers in config.
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None,
                 params: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = headers or {}
        # Resolve env-var tokens in Authorization header
        for k, v in self.headers.items():
            if v.startswith("Bearer "):
                token_env = os.environ.get("REST_TOKEN", "")
                if token_env:
                    self.headers[k] = f"Bearer {token_env}"
        self.params = params or {}

    def fetch(self, since: datetime | None = None) -> pd.DataFrame:
        try:
            import requests
        except ImportError:
            raise ImportError("requests not installed. Run: pip install requests")

        params = dict(self.params)
        if since:
            params["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        r = requests.get(self.url, headers=self.headers, params=params, timeout=30)
        r.raise_for_status()
        df = pd.DataFrame(r.json())
        return self._validate(df)


# ─────────────────────────────────────────────────────────────────────────────
# Factory
# ─────────────────────────────────────────────────────────────────────────────

def build_connector(cfg: dict) -> HistorianConnector:
    """Instantiate the right connector from the *historian* config section."""
    kind = cfg.get("type", "csv").lower()

    if kind == "csv":
        return CsvConnector(csv_path=cfg["csv_path"])

    if kind == "odbc":
        return OdbcConnector(
            connection_string=cfg["connection_string"],
            query=cfg["query"],
        )

    if kind == "osisoft_pi":
        return OsisoftPiConnector(
            base_url=cfg["base_url"],
            username=cfg["username"],
            password=cfg.get("password", ""),
            tags=cfg["tags"],
            verify_ssl=cfg.get("verify_ssl", True),
        )

    if kind == "opcua":
        return OpcUaConnector(
            endpoint=cfg["endpoint"],
            username=cfg.get("username", ""),
            password=cfg.get("password", ""),
            node_ids=cfg["node_ids"],
        )

    if kind == "rest":
        return RestConnector(
            url=cfg["url"],
            headers=cfg.get("headers", {}),
            params=cfg.get("params", {}),
        )

    raise ValueError(f"Unknown historian type: {kind!r}")
