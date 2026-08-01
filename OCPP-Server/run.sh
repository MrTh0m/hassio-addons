#!/bin/sh
set -e

mkdir -p /data
eval "$(python3 /opt/ocpp-server/resolve_config.py)"

echo "[OCPP Backoffice] Démarrage sur le port 8000..."
cd /opt/ocpp-server
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
