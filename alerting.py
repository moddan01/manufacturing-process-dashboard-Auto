"""
alerting.py
───────────
Send violation alerts through one or more channels:
  • E-mail (SMTP / TLS)
  • Microsoft Teams (Incoming Webhook)
  • Slack (Incoming Webhook)

A per-variable cooldown prevents alert spam.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import urllib.request
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Cooldown tracker (in-process; survives for the lifetime of the scheduler)
# ─────────────────────────────────────────────────────────────────────────────

class _Cooldown:
    def __init__(self, minutes: int) -> None:
        self.minutes = minutes
        self._last_sent: dict[str, datetime] = {}

    def should_send(self, key: str) -> bool:
        now = datetime.now(timezone.utc)
        last = self._last_sent.get(key)
        if last is None or (now - last) >= timedelta(minutes=self.minutes):
            self._last_sent[key] = now
            return True
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Individual senders
# ─────────────────────────────────────────────────────────────────────────────

def _send_email(cfg: dict, subject: str, body_html: str) -> None:
    host = cfg["smtp_host"]
    port = int(cfg.get("smtp_port", 587))
    sender = cfg["sender"]
    password = os.environ.get("EMAIL_PASSWORD", cfg.get("password", ""))
    recipients: list[str] = cfg["recipients"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body_html, "html"))

    try:
        if cfg.get("use_tls", True):
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())
        server.quit()
        logger.info("Alert e-mail sent to %s", recipients)
    except Exception as exc:
        logger.error("Failed to send e-mail alert: %s", exc)


def _post_webhook(url: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            logger.info("Webhook responded %d", resp.status)
    except Exception as exc:
        logger.error("Webhook POST failed: %s", exc)


def _send_teams(cfg: dict, summary: str, variable: str,
                violation_count: int, metrics: dict) -> None:
    url = os.environ.get("TEAMS_WEBHOOK_URL", cfg.get("webhook_url", ""))
    if not url:
        logger.warning("Teams webhook URL not configured")
        return

    payload = {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": "FF0000",
        "summary": summary,
        "sections": [
            {
                "activityTitle": f"⚠️ SPC Violation — {variable}",
                "activitySubtitle": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "facts": [
                    {"name": "Variable", "value": variable},
                    {"name": "Violations detected", "value": str(violation_count)},
                    {"name": "Mean", "value": f"{metrics.get('mean', 'N/A'):.3f}"},
                    {"name": "UCL", "value": f"{metrics.get('ucl', 'N/A'):.3f}"},
                    {"name": "LCL", "value": f"{metrics.get('lcl', 'N/A'):.3f}"},
                ],
                "markdown": True,
            }
        ],
    }
    _post_webhook(url, payload)
    logger.info("Teams alert sent for %s", variable)


def _send_slack(cfg: dict, summary: str, variable: str,
                violation_count: int, metrics: dict) -> None:
    url = os.environ.get("SLACK_WEBHOOK_URL", cfg.get("webhook_url", ""))
    if not url:
        logger.warning("Slack webhook URL not configured")
        return

    text = (
        f":warning: *SPC Violation — {variable}*\n"
        f"> Violations: *{violation_count}*\n"
        f"> Mean: `{metrics.get('mean', 'N/A'):.3f}` | "
        f"UCL: `{metrics.get('ucl', 'N/A'):.3f}` | "
        f"LCL: `{metrics.get('lcl', 'N/A'):.3f}`\n"
        f"> {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    payload = {"text": text}
    _post_webhook(url, payload)
    logger.info("Slack alert sent for %s", variable)


# ─────────────────────────────────────────────────────────────────────────────
# Public AlertEngine
# ─────────────────────────────────────────────────────────────────────────────

class AlertEngine:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        cooldown = int(cfg.get("cooldown_minutes", 15))
        self._cooldown = _Cooldown(cooldown)

    def evaluate_and_alert(
        self,
        variable: str,
        violations_df: Any,          # pd.DataFrame with a 'violation' column
        metrics: dict[str, float],
    ) -> None:
        """
        Call this after running detect_violations().  If violations exist and
        the cooldown has expired, fires alerts on all enabled channels.
        """
        if violations_df is None or violations_df.empty:
            return

        violation_count: int = int(violations_df["violation"].sum())
        if violation_count == 0:
            return

        key = variable
        if not self._cooldown.should_send(key):
            logger.debug("Alert suppressed by cooldown for %s", variable)
            return

        summary = f"SPC violation: {violation_count} point(s) out of control for {variable}"
        logger.warning(summary)

        email_cfg = self.cfg.get("email", {})
        if email_cfg.get("enabled"):
            body = self._build_email_body(variable, violation_count, metrics, violations_df)
            self._send_email_safe(email_cfg, f"⚠️ SPC Alert – {variable}", body)

        teams_cfg = self.cfg.get("teams", {})
        if teams_cfg.get("enabled"):
            _send_teams(teams_cfg, summary, variable, violation_count, metrics)

        slack_cfg = self.cfg.get("slack", {})
        if slack_cfg.get("enabled"):
            _send_slack(slack_cfg, summary, variable, violation_count, metrics)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_email_body(
        self,
        variable: str,
        violation_count: int,
        metrics: dict,
        violations_df: Any,
    ) -> str:
        rows_html = ""
        viol_rows = violations_df[violations_df["violation"]].head(20)
        for _, row in viol_rows.iterrows():
            val = row.get(variable, "")
            t = row.get("Time", "")
            types = []
            if row.get("out_of_control_3sigma"):
                types.append("3-sigma")
            if row.get("8_consec_one_side"):
                types.append("8-consec")
            if row.get("2_of_3_2sigma"):
                types.append("2-of-3")
            rows_html += (
                f"<tr><td>{t}</td><td>{val:.4f}</td>"
                f"<td>{'  |  '.join(types)}</td></tr>"
            )

        return f"""
<html><body style="font-family:Arial,sans-serif;color:#333">
<h2 style="color:#c0392b">⚠️ SPC Control Violation</h2>
<table cellpadding="6" cellspacing="0" border="1" style="border-collapse:collapse">
  <tr><th>Variable</th><td><b>{variable}</b></td></tr>
  <tr><th>Violations</th><td>{violation_count} point(s)</td></tr>
  <tr><th>Mean</th><td>{metrics.get('mean','N/A'):.4f}</td></tr>
  <tr><th>UCL</th><td>{metrics.get('ucl','N/A'):.4f}</td></tr>
  <tr><th>LCL</th><td>{metrics.get('lcl','N/A'):.4f}</td></tr>
  <tr><th>Timestamp</th><td>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</td></tr>
</table>
<h3>Violation details (first 20)</h3>
<table cellpadding="5" cellspacing="0" border="1" style="border-collapse:collapse">
  <tr style="background:#f2f2f2">
    <th>Time</th><th>Value</th><th>Rule(s)</th>
  </tr>
  {rows_html}
</table>
<p style="color:#777;font-size:12px">
  Sent by Manufacturing Process Dashboard &mdash;
  adjust cooldown settings to reduce alert frequency.
</p>
</body></html>
"""

    def _send_email_safe(self, cfg: dict, subject: str, body: str) -> None:
        try:
            _send_email(cfg, subject, body)
        except Exception as exc:
            logger.error("Email alert failed: %s", exc)
