#!/usr/bin/env bash
# ==============================================================================
# MayaCardio Free Cloud Deployment Helper Script
# Deploys cloud receiver service to 100% free tier infrastructure (Render/Fly.io)
# ==============================================================================
set -euo pipefail

echo "======================================================================"
echo "MayaCardio Cloud Service - Free Deployment Manager"
echo "Note: Uses 100% free services strictly conforming to the 0-paid-tier rule."
echo "======================================================================"

echo "Option 1: Deploy to Render Free Tier (Automated with Blueprint)"
echo "  1. Push this repository to GitHub."
echo "  2. Go to https://dashboard.render.com/blueprints"
echo "  3. Connect repository and select 'deploy/render.yaml'."
echo "  4. Set device MAYACARDIO_CLOUD_URL=https://<your-service>.onrender.com"

echo ""
echo "Option 2: Deploy to Fly.io Free Allowance"
echo "  1. Install flyctl: curl -L https://fly.io/install.sh | sh"
echo "  2. fly launch --name mayacardio-cloud --region sea --now"
echo "  3. fly volumes create mayacardio_data --size 1"
echo "  4. Set device MAYACARDIO_CLOUD_URL=https://mayacardio-cloud.fly.dev"

echo ""
echo "Option 3: Run standalone cloud server locally or on cloud VM"
echo "  Command: python -m src.cloud.app --host 0.0.0.0 --port 8080 --db cloud_data.db"
