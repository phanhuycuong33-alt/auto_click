#!/usr/bin/env bash
# install.sh — one-shot setup for auto-click-copilot on Ubuntu.
# Installs system packages (scrot, tesseract-ocr) + Python packages (pip).
set -euo pipefail

echo "==> Installing system packages (needs sudo)..."
sudo apt update
sudo apt install -y python3-pip python3-venv scrot tesseract-ocr

echo "==> Creating virtual environment..."
python3 -m venv .venv

echo "==> Installing Python packages from requirements.txt..."
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

cat <<'EOF'

Done!

Run with:
    .venv/bin/python3 auto_click_copilot.py --dry-run

(Or activate the venv first:  source .venv/bin/activate
 then:                          python3 auto_click_copilot.py)
EOF
