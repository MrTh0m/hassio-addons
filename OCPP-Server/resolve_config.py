"""Résout la configuration de démarrage et imprime des lignes `export VAR=valeur`
destinées à être évaluées par run.sh (`eval "$(python3 resolve_config.py)"`).

Centraliser ça en Python plutôt qu'en shell évite d'enchaîner des dizaines de
`python3 -c "..."` fragiles dans run.sh, et permet un vrai parsing JSON/HTTP
propre pour l'auto-détection du broker MQTT via l'API service du Supervisor.
"""
import json
import os
import secrets
import urllib.request


def sh_quote(value) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def load_options() -> dict:
    if os.path.exists("/data/options.json"):
        try:
            with open("/data/options.json") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def fetch_supervisor_mqtt_service() -> dict:
    """Interroge l'API service du Supervisor (accessible sans hassio_api,
    voir la doc officielle) pour récupérer les identifiants MQTT
    auto-provisionnés pour les add-ons (l'utilisateur 'addons')."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return {}
    try:
        req = urllib.request.Request(
            "http://supervisor/services/mqtt",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.load(resp)
        return body.get("data", {}) or {}
    except Exception:
        return {}


def main():
    lines = []
    options = load_options()

    lines.append(f"export OCPP_ADMIN_PASSWORD={sh_quote(options.get('admin_password', 'admin'))}")

    mqtt_enabled = options.get("mqtt_enabled", True)
    lines.append(f"export MQTT_ENABLED={sh_quote(str(bool(mqtt_enabled)).lower())}")
    lines.append(f"export MQTT_BASE_TOPIC={sh_quote(options.get('mqtt_base_topic', 'ocppserver'))}")

    manual_username = options.get("mqtt_username") or ""
    host = options.get("mqtt_host", "core-mosquitto")
    port = options.get("mqtt_port", 1883)
    username = manual_username
    password = options.get("mqtt_password") or ""

    # Si aucun identifiant n'a été saisi à la main, on essaie l'auto-détection
    # via le service Supervisor plutôt que d'obliger à créer un utilisateur
    # Mosquitto manuellement (ce que l'add-on officiel MQTT broker ne permet
    # même pas de faire proprement pour un usage inter-add-ons).
    if not manual_username:
        service = fetch_supervisor_mqtt_service()
        if service:
            host = service.get("host", host)
            port = service.get("port", port)
            username = service.get("username", username)
            password = service.get("password", password)

    lines.append(f"export MQTT_HOST={sh_quote(host)}")
    lines.append(f"export MQTT_PORT={sh_quote(port)}")
    if username:
        lines.append(f"export MQTT_USERNAME={sh_quote(username)}")
    if password:
        lines.append(f"export MQTT_PASSWORD={sh_quote(password)}")

    secret_path = "/data/.secret_key"
    if not os.path.exists(secret_path):
        with open(secret_path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(secret_path) as f:
        lines.append(f"export OCPP_SECRET_KEY={sh_quote(f.read().strip())}")

    print("\n".join(lines))


if __name__ == "__main__":
    main()
