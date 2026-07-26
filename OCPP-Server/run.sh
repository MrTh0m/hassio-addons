#!/bin/sh
set -e

mkdir -p /data

# Lit le mot de passe admin choisi dans la config de l'add-on (Supervisor
# écrit les options dans /data/options.json, y compris pour les add-ons
# locaux qui n'utilisent pas bashio).
if [ -f /data/options.json ]; then
    ADMIN_PASSWORD=$(python3 -c "import json;print(json.load(open('/data/options.json')).get('admin_password','admin'))")
    export OCPP_ADMIN_PASSWORD="$ADMIN_PASSWORD"
fi

# Génère une clé secrète JWT une seule fois et la conserve dans /data
# (persistant), pour que les sessions ne soient pas invalidées à chaque
# redémarrage de l'add-on.
if [ ! -f /data/.secret_key ]; then
    python3 -c "import secrets; open('/data/.secret_key', 'w').write(secrets.token_hex(32))"
fi
export OCPP_SECRET_KEY="$(cat /data/.secret_key)"

echo "[OCPP Backoffice] Démarrage sur le port 8000..."
cd /opt/ocpp-server
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
