# Manufacturing Process Dashboard — Deployment Guide

## Architecture

```
┌──────────────────────┐        ┌──────────────────────────────┐
│  Data Historian      │        │  Server (Linux / Windows)    │
│  (PI / OPC-UA /      │──────▶ │                              │
│   ODBC / REST / CSV) │        │  scheduler.py  (cron-like)   │
└──────────────────────┘        │       │ writes every N sec   │
                                │       ▼                       │
                                │  process_buffer.db  (SQLite)  │
                                │       │                       │
                                │       ▼                       │
                                │  app.py  (Streamlit :8501)   │
                                └──────────────────────────────┘
                                         │
                          ┌──────────────┼──────────────┐
                          ▼              ▼              ▼
                     Monitor 1      Monitor 2     Manager's PC
                  (browser kiosk) (browser kiosk)  (normal view)
```

Two separate processes share one SQLite file:
- **scheduler.py** – pulls historian data on a configurable interval, runs SPC checks, fires alerts
- **app.py** – Streamlit dashboard read-only; auto-refreshes every N seconds

---

## Quick Start (development / CSV)

```bash
git clone … && cd manufacturing_dashboard
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Terminal 1 — start data harvester
python scheduler.py

# Terminal 2 — start dashboard
streamlit run app.py
# Open http://localhost:8501
```

---

## Connecting to your historian

Edit **config.yaml** and set `historian.type` to one of:

| Type          | Requires               | Notes                                    |
|---------------|------------------------|------------------------------------------|
| `csv`         | nothing                | Dev / fallback                           |
| `odbc`        | `pip install pyodbc`   | SQL Server, Oracle, Aspen IP21, PHD, … |
| `osisoft_pi`  | `pip install requests` | PI Web API must be enabled on your server|
| `opcua`       | `pip install opcua`    | OPC-UA server with historical data       |
| `rest`        | `pip install requests` | Any JSON REST endpoint                   |

### Secrets — never put passwords in config.yaml

Use environment variables instead:

| Channel     | Env var            |
|-------------|-------------------|
| PI Web API  | `PI_PASSWORD`      |
| OPC-UA      | `OPC_PASSWORD`     |
| Email       | `EMAIL_PASSWORD`   |
| Teams       | `TEAMS_WEBHOOK_URL`|
| Slack       | `SLACK_WEBHOOK_URL`|

---

## Production deployment (Linux + systemd)

```bash
# 1. Copy files
sudo cp -r . /opt/manufacturing_dashboard
sudo useradd -r -s /bin/false dashboarduser
sudo chown -R dashboarduser:dashboarduser /opt/manufacturing_dashboard

# 2. Create virtualenv
cd /opt/manufacturing_dashboard
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# 3. Install systemd units (see systemd_services.conf for templates)
sudo cp systemd_services.conf /etc/systemd/system/mfg-scheduler.service
# Edit the [Unit] block for the dashboard and save as mfg-dashboard.service

sudo systemctl daemon-reload
sudo systemctl enable mfg-scheduler mfg-dashboard
sudo systemctl start  mfg-scheduler mfg-dashboard

# 4. Check logs
journalctl -u mfg-scheduler -f
journalctl -u mfg-dashboard  -f
```

---

## Multi-monitor / operations room setup

### Option A — Browser kiosk (recommended)

On each monitor PC, open a browser in full-screen kiosk mode pointing to the dashboard URL.  The `?kiosk=1` query parameter hides the sidebar:

**Chrome / Edge (Windows)**
```batch
chrome.exe --kiosk http://dashboard-server:8501/?kiosk=1
```

**Chromium (Linux / Raspberry Pi)**
```bash
chromium-browser --kiosk --noerrdialogs --disable-infobars \
  http://dashboard-server:8501/?kiosk=1
```

**Per-monitor variable lock** — add both `kiosk=1` and a default variable:
```
http://dashboard-server:8501/?kiosk=1
```
Then set the default_variable per display group in a separate config, or use URL params if you extend the app.

### Option B — Raspberry Pi (cheap dedicated displays)

Each monitor gets a Raspberry Pi 4 running Raspberry Pi OS Lite + Chromium in kiosk mode.  Boot script (`/etc/xdg/lxsession/LXDE-pi/autostart`):
```
@chromium-browser --kiosk http://192.168.1.100:8501/?kiosk=1
```

### Option C — Nginx reverse proxy + TLS (LAN access)

```nginx
server {
    listen 80;
    server_name dashboard.yourcompany.local;

    location / {
        proxy_pass         http://127.0.0.1:8501;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";   # WebSocket support
        proxy_set_header   Host       $host;
    }
}
```

---

## Alerting setup

### Email (SMTP)
```yaml
alerting:
  email:
    enabled: true
    smtp_host: smtp.company.com
    smtp_port: 587
    use_tls: true
    sender: dashboard@company.com
    recipients:
      - ops.manager@company.com
```
Set `EMAIL_PASSWORD` env var.

### Microsoft Teams
1. In Teams → channel → ⋯ → Connectors → **Incoming Webhook** → copy URL
2. Set `TEAMS_WEBHOOK_URL` env var (or paste in config.yaml)
3. Set `alerting.teams.enabled: true`

### Slack
1. Create an Incoming Webhook at https://api.slack.com/messaging/webhooks
2. Set `SLACK_WEBHOOK_URL` env var
3. Set `alerting.slack.enabled: true`

### Cooldown
`alerting.cooldown_minutes` prevents the same variable from firing alerts more often than that interval.  Default is 15 minutes.

---

## Tuning the refresh rate

| Setting                         | Where                            |
|---------------------------------|----------------------------------|
| Historian poll interval         | `scheduler.interval_seconds`     |
| Dashboard browser refresh       | `display.refresh_seconds`        |
| Alert cooldown                  | `alerting.cooldown_minutes`      |
| History retained in SQLite      | `scheduler.retention_hours`      |

Typical production values:
- PLC/SCADA data: 5–30 s poll, 10 s display refresh
- Lab / batch data: 60–300 s poll, 30 s display refresh

---

## File layout

```
manufacturing_dashboard/
├── config.yaml              ← all settings
├── app.py                   ← Streamlit dashboard
├── scheduler.py             ← background harvester (run separately)
├── historian_connector.py   ← historian adapters (CSV/ODBC/PI/OPC-UA/REST)
├── data_store.py            ← SQLite buffer
├── alerting.py              ← email / Teams / Slack alerts
├── spc.py                   ← pure SPC logic (shared)
├── requirements.txt
├── systemd_services.conf    ← Linux service templates
└── process_buffer.db        ← created at runtime by scheduler
```
