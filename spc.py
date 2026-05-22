"""
spc.py
──────
Pure SPC computation helpers — no Streamlit, no DB dependency.
Shared by both scheduler.py and app.py.
"""

from __future__ import annotations

from typing import Dict

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px


# ─────────────────────────────────────────────────────────────────────────────

def compute_spc_metrics(series: pd.Series) -> Dict[str, float]:
    mean = series.mean()
    std = series.std()
    return {
        "mean": mean,
        "std": std,
        "ucl": mean + 3 * std,
        "lcl": mean - 3 * std,
        "ucl_2sigma": mean + 2 * std,
        "lcl_2sigma": mean - 2 * std,
    }


def detect_violations(df: pd.DataFrame, variable: str, rules: list) -> pd.DataFrame:
    """
    Return a copy of *df* with violation flag columns.

    Supported rules:
        3-sigma                 – any point beyond ±3σ
        8-consecutive-one-side  – 8+ consecutive points on same side of mean
        2-of-3-2sigma           – 2 of 3 consecutive points beyond ±2σ (same side)
    """
    out = df.copy().reset_index(drop=True)
    series = out[variable]
    metrics = compute_spc_metrics(series)

    # ── 3-sigma ──────────────────────────────────────────────────────────────
    out["out_of_control_3sigma"] = (series > metrics["ucl"]) | (series < metrics["lcl"])

    # ── Side-of-mean helper ───────────────────────────────────────────────────
    out["side_of_mean"] = series.apply(
        lambda v: 1 if v > metrics["mean"] else (-1 if v < metrics["mean"] else 0)
    )

    # ── 8 consecutive points on same side ────────────────────────────────────
    consec_flag = [False] * len(out)
    if "8-consecutive-one-side" in rules:
        count = 0
        last_side = 0
        for i, side in enumerate(out["side_of_mean"]):
            if side == 0:
                count = 0
                last_side = 0
            elif side == last_side:
                count += 1
            else:
                count = 1
                last_side = side
            consec_flag[i] = count >= 8
    out["8_consec_one_side"] = consec_flag

    # ── 2 of 3 beyond ±2σ (same side) ───────────────────────────────────────
    above_2s = series > metrics["ucl_2sigma"]
    below_2s = series < metrics["lcl_2sigma"]
    two_of_three = [False] * len(out)
    if "2-of-3-2sigma" in rules:
        for i in range(len(series)):
            start = max(0, i - 2)
            if above_2s.iloc[start : i + 1].sum() >= 2 or below_2s.iloc[start : i + 1].sum() >= 2:
                two_of_three[i] = True
    out["2_of_3_2sigma"] = two_of_three

    # ── Combined ─────────────────────────────────────────────────────────────
    out["violation"] = False
    if "3-sigma" in rules:
        out["violation"] |= out["out_of_control_3sigma"]
    if "8-consecutive-one-side" in rules:
        out["violation"] |= out["8_consec_one_side"]
    if "2-of-3-2sigma" in rules:
        out["violation"] |= out["2_of_3_2sigma"]

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Chart builders
# ─────────────────────────────────────────────────────────────────────────────

_RULE_COLORS = {
    "out_of_control_3sigma": ("red", "x"),
    "2_of_3_2sigma": ("orange", "triangle-up"),
    "8_consec_one_side": ("purple", "diamond"),
}


def plot_spc_chart(
    df: pd.DataFrame,
    variable: str,
    metrics: Dict[str, float],
    violations_df: pd.DataFrame,
) -> go.Figure:
    fig = px.line(df, x="Time", y=variable, title=f"{variable} — SPC Control Chart")
    fig.update_traces(line_color="#1f77b4", line_width=1.5)

    # ── Control limit lines ───────────────────────────────────────────────────
    for y_val, dash, color, label, pos in [
        (metrics["mean"],       "dash",  "black", "Mean",  "top left"),
        (metrics["ucl"],        "dash",  "red",   "UCL",   "top right"),
        (metrics["lcl"],        "dash",  "red",   "LCL",   "bottom right"),
        (metrics["ucl_2sigma"], "dot",   "orange","2σ+",   "top right"),
        (metrics["lcl_2sigma"], "dot",   "orange","2σ−",   "bottom right"),
    ]:
        fig.add_hline(
            y=y_val,
            line_dash=dash,
            line_color=color,
            annotation_text=label,
            annotation_position=pos,
        )

    # ── Scatter layer with per-point color/symbol ─────────────────────────────
    colors, symbols = [], []
    for i in range(len(violations_df)):
        assigned = False
        for col, (c, s) in _RULE_COLORS.items():
            if col in violations_df.columns and violations_df.loc[i, col]:
                colors.append(c)
                symbols.append(s)
                assigned = True
                break
        if not assigned:
            colors.append("#1f77b4")
            symbols.append("circle")

    fig.add_trace(
        go.Scatter(
            x=df["Time"],
            y=df[variable],
            mode="markers",
            marker=dict(color=colors, size=7, symbol=symbols),
            name="Points",
            showlegend=False,
        )
    )

    # ── Legend annotation ─────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                             marker=dict(color="red", symbol="x", size=9),
                             name="3σ violation"))
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                             marker=dict(color="orange", symbol="triangle-up", size=9),
                             name="2-of-3 violation"))
    fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                             marker=dict(color="purple", symbol="diamond", size=9),
                             name="8-consec violation"))

    fig.update_layout(
        xaxis_title="Time",
        yaxis_title=variable,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=40, r=40, t=60, b=40),
    )
    return fig


def plot_multi_var_chart(df: pd.DataFrame, variables: list) -> go.Figure:
    fig = go.Figure()
    for v in variables:
        fig.add_trace(go.Scatter(x=df["Time"], y=df[v], mode="lines+markers", name=v))
    fig.update_layout(
        title="Multi-variable Comparison",
        xaxis_title="Time",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(l=40, r=40, t=60, b=40),
    )
    return fig
