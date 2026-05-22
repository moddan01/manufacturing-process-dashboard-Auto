"""
app.py
──────
Manufacturing Process Dashboard — Streamlit front-end.

Features added vs. original:
  • Reads from SQLite buffer populated by scheduler.py
  • Auto-refresh (configurable interval via config.yaml / sidebar)
  • Kiosk / monitor mode — hides sidebar for clean room-display view
  • Full SPC with 3 rule sets + violation table
  • Data + metrics + chart export
  • Status bar showing last harvest time
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from data_store import DataStore
from spc import compute_spc_metrics, detect_violations, plot_multi_var_chart, plot_spc_chart

# ─────────────────────────────────────────────────────────────────────────────
# Page config — must be the very first Streamlit call
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Manufacturing Process Dashboard",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── URL query parameter: ?kiosk=1 hides the sidebar ──────────────────────────
params = st.query_params
kiosk_mode: bool = params.get("kiosk", "0") == "1"

if kiosk_mode:
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"] {display: none}
        #MainMenu {visibility: hidden}
        footer {visibility: hidden}
        header {visibility: hidden}
        .block-container {padding-top: 0.5rem}
        </style>
        """,
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Load config
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_resource
def _load_config() -> dict:
    cfg_path = Path(__file__).parent / "config.yaml"
    with open(cfg_path, "r") as f:
        return yaml.safe_load(f)


cfg = _load_config()
display_cfg = cfg.get("display", {})
sched_cfg = cfg.get("scheduler", {})
DB_PATH = sched_cfg.get("db_path", "process_buffer.db")


# ─────────────────────────────────────────────────────────────────────────────
# Data access
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_resource
def get_store() -> DataStore:
    return DataStore(DB_PATH)


def load_data(hours: int) -> pd.DataFrame:
    store = get_store()
    return store.query(since_hours=hours)


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────

def render_sidebar() -> dict:
    """Render controls and return a settings dict."""
    with st.sidebar:
        st.title("⚙️ Controls")

        st.subheader("Data window")
        hours = st.slider("Hours of history", min_value=1, max_value=72, value=24, step=1)

        st.subheader("Variables")
        variables = st.multiselect(
            "Select variable(s)",
            ["Temperature", "Pressure", "FlowRate"],
            default=[display_cfg.get("default_variable", "Temperature")],
        )

        st.subheader("SPC rules")
        rules = st.multiselect(
            "Active rules",
            options=["3-sigma", "8-consecutive-one-side", "2-of-3-2sigma"],
            default=display_cfg.get("default_rules", ["3-sigma"]),
        )

        st.subheader("Filters")
        # Time filter applied after load
        time_filter = st.checkbox("Custom time range", value=False)
        time_range = None

        st.subheader("Auto-refresh")
        default_refresh = int(display_cfg.get("refresh_seconds", 30))
        refresh = st.number_input(
            "Refresh every (seconds) — 0 to disable",
            min_value=0,
            max_value=3600,
            value=default_refresh,
            step=10,
        )

        st.subheader("Display mode")
        st.markdown(
            "Add `?kiosk=1` to the URL to hide this sidebar for room displays.",
            help="Kiosk mode removes all chrome for a clean monitor view.",
        )

        st.subheader("Status")
        store = get_store()
        latest = store.latest_timestamp()
        row_count = store.row_count()
        if latest:
            st.metric("Last harvest", latest.strftime("%H:%M:%S UTC"))
        st.metric("Buffered rows", row_count)

    return {
        "hours": hours,
        "variables": variables,
        "rules": rules,
        "refresh": refresh,
        "time_filter": time_filter,
        "time_range": time_range,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main dashboard
# ─────────────────────────────────────────────────────────────────────────────

def render_dashboard(settings: dict) -> None:
    st.title("🏭 Manufacturing Process Dashboard")

    # ── Load data ─────────────────────────────────────────────────────────────
    df = load_data(settings["hours"])

    if df.empty:
        st.warning(
            "No data in buffer yet.  Make sure **scheduler.py** is running "
            "and has completed at least one harvest cycle."
        )
        return

    # ── Optional time range filter ─────────────────────────────────────────────
    if settings["time_filter"]:
        time_min = df["Time"].min().to_pydatetime()
        time_max = df["Time"].max().to_pydatetime()
        t0, t1 = st.slider(
            "Time window",
            min_value=time_min,
            max_value=time_max,
            value=(time_min, time_max),
            format="MM/DD HH:mm",
        )
        df = df[(df["Time"] >= t0) & (df["Time"] <= t1)]

    # ── Optional metadata filters ──────────────────────────────────────────────
    if "Batch" in df.columns:
        batches = sorted(df["Batch"].dropna().unique())
        sel = st.multiselect("Batch", batches, default=batches)
        df = df[df["Batch"].isin(sel)]

    if "Shift" in df.columns:
        shifts = sorted(df["Shift"].dropna().unique())
        sel = st.multiselect("Shift", shifts, default=shifts)
        df = df[df["Shift"].isin(sel)]

    if df.empty:
        st.info("No data matches the current filters.")
        return

    variables: list[str] = settings["variables"]
    rules: list[str] = settings["rules"]

    if not variables:
        st.info("Select at least one variable in the sidebar.")
        return

    # ── Single-variable SPC view ───────────────────────────────────────────────
    if len(variables) == 1:
        variable = variables[0]

        metrics = compute_spc_metrics(df[variable])
        violations_df = detect_violations(df.reset_index(drop=True), variable, rules)

        # Alert banner
        viol_count = int(violations_df["violation"].sum())
        if viol_count:
            st.error(
                f"⚠️  **{viol_count} violation(s) detected** for **{variable}** "
                f"under the selected rules."
            )
        else:
            st.success(f"✅  **{variable}** is in control under selected rules.")

        # Chart
        fig = plot_spc_chart(df.reset_index(drop=True), variable, metrics, violations_df)
        st.plotly_chart(fig, use_container_width=True)

        # Metrics row
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Mean",  f"{metrics['mean']:.3f}")
        c2.metric("Std",   f"{metrics['std']:.3f}")
        c3.metric("UCL",   f"{metrics['ucl']:.3f}")
        c4.metric("LCL",   f"{metrics['lcl']:.3f}")

        # Violations table
        if viol_count:
            with st.expander("Violation details", expanded=True):
                show_cols = ["Time", variable, "out_of_control_3sigma",
                             "2_of_3_2sigma", "8_consec_one_side"]
                show_cols = [c for c in show_cols if c in violations_df.columns]
                st.dataframe(
                    violations_df[violations_df["violation"]][show_cols],
                    use_container_width=True,
                )

        metrics_df = pd.DataFrame([{"variable": variable, **metrics}])

    # ── Multi-variable comparison ──────────────────────────────────────────────
    else:
        fig = plot_multi_var_chart(df, variables)
        st.plotly_chart(fig, use_container_width=True)

        metrics_list = []
        cols = st.columns(len(variables))
        for idx, v in enumerate(variables):
            m = compute_spc_metrics(df[v])
            violations_df = detect_violations(df.reset_index(drop=True), v, rules)
            viol_count = int(violations_df["violation"].sum())
            with cols[idx]:
                st.metric(f"{v} mean", f"{m['mean']:.3f}")
                if viol_count:
                    st.error(f"{viol_count} violation(s)")
                else:
                    st.success("In control")
            metrics_list.append({"variable": v, **m})

        metrics_df = pd.DataFrame(metrics_list)
        st.subheader("Metrics summary")
        st.dataframe(metrics_df, use_container_width=True)

    # ── Export buttons ─────────────────────────────────────────────────────────
    st.divider()
    e1, e2, e3 = st.columns(3)

    with e1:
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button("⬇ Filtered data (CSV)", csv, "filtered_data.csv", "text/csv")

    with e2:
        mcsv = metrics_df.to_csv(index=False).encode("utf-8")
        st.download_button("⬇ Metrics (CSV)", mcsv, "metrics.csv", "text/csv")

    with e3:
        try:
            img = fig.to_image(format="png")
            st.download_button("⬇ Chart (PNG)", img, "chart.png", "image/png")
        except Exception:
            st.info("Install **kaleido** to enable chart PNG export.")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    settings = render_sidebar()
    render_dashboard(settings)

    # ── Auto-refresh ──────────────────────────────────────────────────────────
    refresh_s = settings["refresh"]
    if refresh_s > 0:
        # st.empty placeholder at bottom to show countdown
        placeholder = st.empty()
        for remaining in range(refresh_s, 0, -1):
            placeholder.caption(f"🔄 Refreshing in {remaining}s …")
            time.sleep(1)
        placeholder.empty()
        st.rerun()


if __name__ == "__main__":
    main()
