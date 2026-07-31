#!/usr/bin/env bash
# =====================================================================
#  Deploy the Hero SMS COLLECTOR on a Linux VPS (Ubuntu/Debian).
#  Safe to run on the SAME VPS as your Telegram bot (different port).
#
#  The collector only receives each client's recovered-accounts copy and
#  answers "is this client still enabled?" (the remote disable switch).
#  It runs NO browser and NO bot, so a datacenter VPS is fine here.
#
#  Usage:  copy this project folder to the VPS, then:
#            cd <folder> && bash deploy_collector.sh
# =====================================================================
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE="hero-collector"
PORT="${HERO_PORT:-5000}"
RUN_USER="$(whoami)"

echo "== 1/4  System packages =="
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip

echo "== 2/4  Python venv + deps (flask + waitress only) =="
cd "$APP_DIR"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip -q
./.venv/bin/pip install flask waitress -q

echo "== 3/4  systemd service (auto-start + auto-restart) =="
# Bind to 0.0.0.0 so the public IP serves it (IP-direct, no reverse proxy).
sudo tee /etc/systemd/system/${SERVICE}.service >/dev/null <<UNIT
[Unit]
Description=Hero SMS Collector
After=network.target

[Service]
WorkingDirectory=${APP_DIR}
Environment=HERO_HOST=0.0.0.0
Environment=HERO_PORT=${PORT}
ExecStart=${APP_DIR}/.venv/bin/python app.py ${PORT}
Restart=always
RestartSec=3
User=${RUN_USER}

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE} >/dev/null 2>&1 || true
sudo systemctl restart ${SERVICE}
sleep 2

echo "== 4/4  Firewall + status =="
# Open the collector port if ufw is active
if command -v ufw >/dev/null 2>&1; then
    sudo ufw allow ${PORT}/tcp >/dev/null 2>&1 || true
fi
sudo systemctl --no-pager status ${SERVICE} | head -8 || true

PUBIP="$(curl -s https://api.ipify.org 2>/dev/null || echo '<your-vps-ip>')"
echo ""
echo "-------------------------------------------------------------"
echo " Collector is LIVE on port ${PORT} (all interfaces)."
echo ""
echo " Collector URL for make_client / client packages:"
echo "     http://${PUBIP}:${PORT}/api/collect"
echo ""
echo " Owner dashboard (manage/disable clients):"
echo "     http://${PUBIP}:${PORT}    (login admin / admin - CHANGE IT)"
echo ""
echo " If your provider has its own firewall (e.g. DigitalOcean Cloud"
echo " Firewall), also allow inbound TCP ${PORT} there."
echo ""
echo " Manage the service:"
echo "     sudo systemctl restart ${SERVICE}     # restart"
echo "     journalctl -u ${SERVICE} -f           # live logs"
echo "-------------------------------------------------------------"
