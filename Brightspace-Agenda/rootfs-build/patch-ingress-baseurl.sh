#!/bin/sh
# patch-ingress-baseurl.sh
#
# index.html définit `const API_ORIGIN = '';`, ce qui fait résoudre chaque
# appel à `${API_ORIGIN}/proxy.php` et `${API_ORIGIN}/api.php` en chemin
# ABSOLU `/proxy.php` / `/api.php` (racine du navigateur), pas relatif au
# dossier de la page.
#
# Ça fonctionne en accès direct (la racine du site EST la racine), mais pas
# du tout sous l'Ingress Home Assistant : la page y est servie sous un
# préfixe de session (/api/hassio_ingress/<token>/...) que ce chemin absolu
# ignore complètement. Le navigateur appelle alors la racine de Home
# Assistant elle-même (qui n'a pas ces fichiers), pas l'addon. D'où l'échec
# silencieux de l'import ICS et de tout autre appel API sous Ingress.
#
# Correctif : calculer la base au runtime depuis location.pathname (le
# dossier de la page actuellement chargée). Ce dossier contient déjà le bon
# préfixe quel que soit le mode d'accès :
#   - accès direct : location.pathname = "/"                          -> base = ""
#   - Ingress       : location.pathname = "/api/hassio_ingress/X/"    -> base = "/api/hassio_ingress/X"
# Comportement inchangé en accès direct (base = "" comme avant).

set -e

TARGET_FILE="/var/www/html/index.html"
NEEDLE="const API_ORIGIN = '';"
REPLACEMENT="const API_ORIGIN = (() => { const p = location.pathname; return p.substring(0, p.lastIndexOf('/')); })();"

if [ ! -f "$TARGET_FILE" ]; then
    echo "[patch-ingress-baseurl] ERREUR : $TARGET_FILE introuvable" >&2
    exit 1
fi

if [ "$(grep -cF "$NEEDLE" "$TARGET_FILE")" -ne 1 ]; then
    echo "[patch-ingress-baseurl] ERREUR : motif API_ORIGIN absent ou en nombre inattendu, code source modifié ?" >&2
    exit 1
fi

sed -i "s#$NEEDLE#$REPLACEMENT#" "$TARGET_FILE"

if ! grep -qF "location.pathname" "$TARGET_FILE"; then
    echo "[patch-ingress-baseurl] ERREUR : le patch ne s'est pas appliqué correctement" >&2
    exit 1
fi

echo "[patch-ingress-baseurl] OK : API_ORIGIN calculé dynamiquement (compatible Ingress)"
