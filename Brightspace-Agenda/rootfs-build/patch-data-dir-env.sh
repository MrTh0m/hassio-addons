#!/bin/sh
# patch-data-dir-env.sh
#
# api.php et setup.php definissent DATA_DIR ainsi :
#   define('DATA_DIR', __DIR__ . '/data');
# soit /var/www/html/data. Ce chemin passait jusqu'ici par un lien
# symbolique vers /data (le volume persistant fourni par le Supervisor),
# cree par run.sh. Ce lien traverse deux points de montage differents
# (la couche d'image du conteneur d'un cote, le volume Supervisor de
# l'autre), et le confinement AppArmor applique aux addons Home Assistant
# bloque ce type de traversee meme quand les permissions Unix classiques
# sont correctes de bout en bout (confirme par diagnostic : /data direct
# fonctionne pour www-data, /var/www/html/data echoue systematiquement).
#
# Correctif : DATA_DIR lit une variable d'environnement (BSA_DATA_DIR,
# positionnee sur /data via le Dockerfile) en priorite, et ne retombe sur
# __DIR__.'/data' que si elle est absente (comportement inchange pour tout
# hebergement hors addon Home Assistant). Plus de lien symbolique du tout :
# /data n'est alors jamais expose sous le docroot web, ce qui est meme plus
# sur que la protection par .htaccess utilisee jusqu'ici.

set -e

NEEDLE="define('DATA_DIR',    __DIR__ . '/data');"
REPLACEMENT="define('DATA_DIR',    getenv('BSA_DATA_DIR') ?: (__DIR__ . '/data'));"

for TARGET_FILE in /var/www/html/api.php /var/www/html/setup.php; do
    if [ ! -f "$TARGET_FILE" ]; then
        echo "[patch-data-dir-env] ERREUR : $TARGET_FILE introuvable" >&2
        exit 1
    fi

    if ! grep -qF "$NEEDLE" "$TARGET_FILE"; then
        echo "[patch-data-dir-env] ERREUR : motif DATA_DIR introuvable dans $TARGET_FILE, code source modifie ?" >&2
        exit 1
    fi

    sed -i "s#$NEEDLE#$REPLACEMENT#" "$TARGET_FILE"

    if ! grep -qF "BSA_DATA_DIR" "$TARGET_FILE"; then
        echo "[patch-data-dir-env] ERREUR : le patch ne s'est pas applique correctement dans $TARGET_FILE" >&2
        exit 1
    fi

    echo "[patch-data-dir-env] OK : DATA_DIR rendu configurable via BSA_DATA_DIR dans $TARGET_FILE"
done
