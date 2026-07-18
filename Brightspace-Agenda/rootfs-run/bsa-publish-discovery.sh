#!/bin/sh
# bsa-publish-discovery.sh
#
# Publie le service Discovery Supervisor pour que l'intégration HACS
# puisse détecter l'addon automatiquement et récupérer le share_token
# sans intervention manuelle de l'utilisateur.
#
# Appelé depuis run.sh en arrière-plan, après le bootstrap PHP.
# Attend que config.json soit disponible avant de publier.

set -e

CONFIG_FILE="/data/config.json"
SUPERVISOR_URL="http://supervisor"
MAX_WAIT=30   # secondes max pour attendre config.json

log() { echo "[Brightspace Agenda][Discovery] $1"; }

# ── Attente de config.json ─────────────────────────────────────────────────
i=0
while [ ! -f "$CONFIG_FILE" ]; do
    if [ "$i" -ge "$MAX_WAIT" ]; then
        log "TIMEOUT : config.json toujours absent après ${MAX_WAIT}s, Discovery non publié."
        exit 1
    fi
    sleep 1
    i=$((i + 1))
done

# ── Lecture du share_token ─────────────────────────────────────────────────
TOKEN=$(php -r "
    \$c = json_decode(file_get_contents('${CONFIG_FILE}'), true);
    echo \$c['share_token'] ?? '';
")

if [ -z "$TOKEN" ]; then
    log "ERREUR : share_token introuvable dans config.json, Discovery non publié."
    exit 1
fi

# ── Lecture du port mappé via l'API Supervisor ─────────────────────────────
# On interroge /addons/self/info pour obtenir le port réellement mappé par
# l'utilisateur (qui peut différer du port par défaut 8099).
ADDON_INFO=$(curl -sf \
    -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
    -H "Content-Type: application/json" \
    "${SUPERVISOR_URL}/addons/self/info" 2>/dev/null || echo "{}")

PORT=$(php -r "
    \$info = json_decode('$(echo "$ADDON_INFO" | sed "s/'/\\\\'/" | tr -d '\n')', true);
    \$net  = \$info['data']['network'] ?? [];
    foreach (\$net as \$k => \$v) {
        if (strpos((string)\$k, '8099') !== false && \$v !== null) {
            echo (int)\$v;
            exit;
        }
    }
    echo 8099;
")

log "Token récupéré, port mappé : ${PORT}. Publication en cours..."

# ── Publication Discovery ──────────────────────────────────────────────────
PAYLOAD=$(printf '{"service":"brightspace_agenda","config":{"token":"%s","port":%s}}' \
    "$TOKEN" "$PORT")

RESULT=$(curl -sf \
    -X POST \
    -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "${SUPERVISOR_URL}/discovery" 2>/dev/null || echo '{"error":"curl_failed"}')

if echo "$RESULT" | grep -q '"uuid"'; then
    UUID=$(php -r "
        \$r = json_decode('$(echo "$RESULT" | tr -d '\n')', true);
        echo \$r['data']['uuid'] ?? \$r['uuid'] ?? 'unknown';
    ")
    log "Service Discovery publié avec succès (uuid: ${UUID}, port: ${PORT})."
else
    log "AVERTISSEMENT : publication Discovery échouée. L'intégration HACS devra être configurée manuellement. Réponse : ${RESULT}"
fi
