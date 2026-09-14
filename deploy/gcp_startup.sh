#!/usr/bin/env bash
# ==============================================================================
# AURA QUANT AI • Automated Deployment & Setup Script for GCP e2-micro
# ==============================================================================
set -euo pipefail

echo "======================================================================"
echo "  Starting AURA QUANT AI Deployment on Google Cloud Platform (GCP)    "
echo "======================================================================"

# 1. Configure 2GB Swap Memory (Crucial for e2-micro 1GB RAM stability)
if [ ! -f /swapfile ]; then
    echo "[1/6] Allocating 2GB Swapfile for memory safety..."
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    sysctl vm.swappiness=15
    echo 'vm.swappiness=15' >> /etc/sysctl.conf
    echo "Swap allocated successfully:"
    free -h
else
    echo "[1/6] Swapfile already present."
fi

# 2. Update packages and install core dependencies
echo "[2/6] Installing Python3, Pip, Venv, and Git..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git curl ufw

# 3. Create target directory
APP_DIR="/opt/aura-quant"
echo "[3/6] Setting up application directory at ${APP_DIR}..."
mkdir -p "${APP_DIR}"

# If code is not already present, clone or prepare placeholder
if [ ! -f "${APP_DIR}/run_agent.py" ]; then
    echo "No existing installation found in ${APP_DIR}."
fi

# 4. Create and configure Python Virtual Environment
echo "[4/6] Initializing Python Virtual Environment..."
if [ ! -d "${APP_DIR}/venv" ]; then
    python3 -m venv "${APP_DIR}/venv"
fi

"${APP_DIR}/venv/bin/pip" install --upgrade pip setuptools wheel

if [ -f "${APP_DIR}/requirements.txt" ]; then
    echo "Installing requirements from ${APP_DIR}/requirements.txt..."
    "${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
fi

# 5. Install & Enable systemd service
echo "[5/6] Configuring systemd service 'aura-quant'..."
cat << 'EOF' > /etc/systemd/system/aura-quant.service
[Unit]
Description=AURA QUANT AI Autonomous Trading Platform
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/aura-quant
EnvironmentFile=/opt/aura-quant/.env
ExecStart=/opt/aura-quant/venv/bin/python run_agent.py --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
KillMode=mixed
TimeoutStopSec=20

# Resource Protections
LimitNOFILE=65535
MemoryHigh=850M
MemoryMax=950M

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=aura-quant

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable aura-quant.service

# 6. Allow ports in local firewall if UFW is active
if command -v ufw >/dev/null 2>&1; then
    ufw allow 8000/tcp comment "Aura Quant Web Dashboard" || true
    ufw allow 80/tcp comment "HTTP" || true
    ufw allow 443/tcp comment "HTTPS" || true
    ufw allow 22/tcp comment "SSH" || true
fi

# Start service if code and .env are ready
if [ -f "${APP_DIR}/run_agent.py" ] && [ -f "${APP_DIR}/.env" ]; then
    echo "[6/6] Starting AURA QUANT AI service..."
    systemctl restart aura-quant.service
    sleep 3
    systemctl status aura-quant.service --no-pager || true
else
    echo "[6/6] Service installed and enabled. Copy your project files and .env to ${APP_DIR} and run: systemctl start aura-quant"
fi

echo "======================================================================"
echo "  AURA QUANT AI Deployment Finished!                                  "
echo "======================================================================"
