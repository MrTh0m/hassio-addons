#!/bin/sh
# patch-ingress-autologin.sh
#
# Auto-connexion en mode connecté pour les requêtes qui passent par
# l'Ingress Home Assistant. Le proxy Ingress du Supervisor connecte
# toujours l'addon depuis une IP interne fixe (172.30.32.2), propre au
# réseau Docker privé de l'addon, non falsifiable depuis l'extérieur : un
# client qui frappe le port direct (8099) ne peut jamais se faire passer
# pour cette IP. L'accès direct continue donc d'exiger le mot de passe
# normalement (APK, intégration HACS, navigateur externe).
#
# Insère juste après la définition de STATE_FILE (a besoin de getConfig()
# et donc de DATA_DIR déjà défini) et avant tout traitement de requête.

set -e

TARGET_FILE="/var/www/html/api.php"
NEEDLE="define('STATE_FILE',  DATA_DIR . '/state.json');"

if [ ! -f "$TARGET_FILE" ]; then
    echo "[patch-ingress-autologin] ERREUR : $TARGET_FILE introuvable" >&2
    exit 1
fi

if [ "$(grep -cF "$NEEDLE" "$TARGET_FILE")" -ne 1 ]; then
    echo "[patch-ingress-autologin] ERREUR : point d'insertion introuvable ou en nombre inattendu, code source modifié ?" >&2
    exit 1
fi

INSERT='\
\
// Auto-connexion sous Ingress Home Assistant : requête reçue depuis l'"'"'IP\
// interne fixe du proxy Supervisor (non falsifiable depuis le port direct).\
if (empty($_SESSION['"'"'emmgo_auth'"'"']) \&\& ($_SERVER['"'"'REMOTE_ADDR'"'"'] ?? '"'"''"'"') === '"'"'172.30.32.2'"'"') {\
    $autoCfg = getConfig();\
    if (!empty($autoCfg['"'"'password_hash'"'"'])) {\
        $_SESSION['"'"'emmgo_auth'"'"'] = true;\
        $_SESSION['"'"'login_time'"'"'] = time();\
    }\
}'

sed -i "s#$NEEDLE#$NEEDLE$INSERT#" "$TARGET_FILE"

if ! grep -qF "Auto-connexion sous Ingress" "$TARGET_FILE"; then
    echo "[patch-ingress-autologin] ERREUR : le patch ne s'est pas appliqué correctement" >&2
    exit 1
fi

echo "[patch-ingress-autologin] OK : auto-connexion Ingress ajoutée"
