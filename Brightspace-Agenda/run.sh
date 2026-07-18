#!/bin/sh
# run.sh - point d'entrée du conteneur (voir Dockerfile : CMD [ "/run.sh" ])
set -e

DATA_DIR="/data"

echo "[Brightspace Agenda] Préparation du stockage persistant (${DATA_DIR})"

# /data est fourni et persiste automatiquement par le Supervisor Home Assistant.
# On le fait pointer vers le dossier data/ attendu par l'app (chemin relatif en
# dur côté PHP : __DIR__.'/data') sans toucher au code source d'api.php/setup.php.
if [ -e /var/www/html/data ] && [ ! -L /var/www/html/data ]; then
    rm -rf /var/www/html/data
fi
[ -L /var/www/html/data ] || ln -s "$DATA_DIR" /var/www/html/data

chown -R www-data:www-data "$DATA_DIR"

php -f /usr/local/bin/bsa-bootstrap.php

echo "[Brightspace Agenda] Démarrage Apache sur le port 8099 (Ingress + accès direct)"
exec apache2-foreground
