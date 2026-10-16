#!/usr/bin/env bash
# ==============================================================================
# MayaCardio Raspberry Pi 4 (ARM64) Fresh Installation Script
# ==============================================================================
set -euo pipefail

echo "==> [1/5] Updating system packages on ARM64 Raspberry Pi OS..."
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv sqlite3 git curl

echo "==> [2/5] Creating data directory..."
sudo mkdir -p /var/lib/mayacardio
sudo chown -R "$USER":"$USER" /var/lib/mayacardio

echo "==> [3/5] Setting up Python virtual environment..."
python3 -m venv venv
# shellcheck source=/dev/null
source venv/bin/activate
pip install --upgrade pip
pip install -e .

echo "==> [4/5] Making CLI executable and adding to PATH..."
chmod +x pipeline
if ! grep -q 'maya_cardio' ~/.bashrc; then
  echo 'export PATH="$HOME/maya_cardio:$PATH"' >> ~/.bashrc
fi

echo "==> [5/5] Running self-test suite..."
python3 -m pytest tests/

echo "======================================================================"
echo "MayaCardio Pipeline installed successfully on ARM64 Raspberry Pi 4!"
echo "Usage:"
echo "  pipeline status"
echo "  pipeline export --session <ID> --from device"
echo "  pipeline results --session <ID>"
echo "======================================================================"
