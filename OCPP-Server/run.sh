#!/bin/sh
set -e

mkdir -p /data

# Lit les options de l'add-on (Supervisor écrit /data/options.json, y compris
# pour les add-ons locaux qui n'utilisent pas bashio).
if [ -f /data/options.json ]; then
    ADMIN_PASSWORD=$(python3 -c "import json;print(json.load(open('/data/options.json')).get('admin_password','admin'))")
    export OCPP_ADMIN_PASSWORD="$ADMIN_PASSWORD"

    export MQTT_ENABLED=$(python3 -c "import json;print(str(json.load(open('/data/options.json')).get('mqtt_enabled', True)).lower())")
    export MQTT_HOST=$(python3 -c "import json;print(json.load(open('/data/options.json')).get('mqtt_host','core-mosquitto'))")
    export MQTT_PORT=$(python3 -c "import json;print(json.load(open('/data/options.json')).get('mqtt_port',1883))")
    MQTT_USER=$(python3 -c "import json;print(json.load(open('/data/options.json')).get('mqtt_username') or '')")
    MQTT_PASS=$(python3 -c "import json;print(json.load(open('/data/options.json')).get('mqtt_password') or '')")
    [ -n "$MQTT_USER" ] && export MQTT_USERNAME="$MQTT_USER"
    [ -n "$MQTT_PASS" ] && export MQTT_PASSWORD="$MQTT_PASS"
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
