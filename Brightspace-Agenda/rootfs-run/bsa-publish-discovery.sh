#!/bin/sh
# bsa-publish-discovery.sh
#
# Publie le service Discovery Supervisor pour que l'intégration HACS
# puisse détecter l'addon automatiquement et récupérer le share_token
# sans intervention manuelle de l'utilisateur.
#
# Appelé depuis run.sh en arrière-plan, après le bootstrap PHP.
# Attend que config.json soit disponible avant de publier.
#
# Les réponses JSON de l'API Supervisor sont toujours écrites dans des
# fichiers temporaires, jamais interpolées directement dans une chaîne PHP
# via le shell : un JSON contenant un guillemet simple ou un backslash
# casserait une interpolation naïve, même sans intention malveillante
# (le contenu vient de l'API Supervisor, pas d'un attaquant, mais autant
# rester robuste).

set -e

CONFIG_FILE="/data/config.json"
SUPERVISOR_URL="http://supervisor"
MAX_WAIT=30   # secondes max pour attendre config.json
TMP_INFO="/tmp/bsa-discovery-info.json"
TMP_RESULT="/tmp/bsa-discovery-result.json"

log() { echo "[Brightspace Agenda][Discovery] $1"; }

cleanup() { rm -f "$TMP_INFO" "$TMP_RESULT"; }
trap cleanup EXIT

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
if ! curl -sf \
    -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
    -H "Content-Type: application/json" \
    "${SUPERVISOR_URL}/addons/self/info" -o "$TMP_INFO"; then
    log "AVERTISSEMENT : appel à /addons/self/info échoué (curl), port par défaut 8099 utilisé."
    echo '{}' > "$TMP_INFO"
fi

PORT=$(php -r "
    \$info = json_decode(file_get_contents('${TMP_INFO}'), true);
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

if ! curl -sf \
    -X POST \
    -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "${SUPERVISOR_URL}/discovery" -o "$TMP_RESULT"; then
    log "AVERTISSEMENT : publication Discovery échouée (curl). L'intégration HACS devra être configurée manuellement."
    exit 0
fi

if grep -qF '"uuid"' "$TMP_RESULT"; then
    UUID=$(php -r "
        \$r = json_decode(file_get_contents('${TMP_RESULT}'), true);
        echo \$r['data']['uuid'] ?? \$r['uuid'] ?? 'unknown';
    ")
    log "Service Discovery publié avec succès (uuid: ${UUID}, port: ${PORT})."
else
    RESULT_BODY=$(cat "$TMP_RESULT")
    log "AVERTISSEMENT : publication Discovery échouée. L'intégration HACS devra être configurée manuellement. Réponse : ${RESULT_BODY}"
fi
