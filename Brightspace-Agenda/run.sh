#!/bin/sh
# run.sh - point d'entrée du conteneur (voir Dockerfile : CMD [ "/run.sh" ])
set -e

DATA_DIR="/data"

echo "[Brightspace Agenda] Préparation du stockage persistant (${DATA_DIR})"

# /data est fourni et persiste automatiquement par le Supervisor Home Assistant.
# api.php/setup.php lisent BSA_DATA_DIR (variable d'environnement, definie
# dans le Dockerfile) pour pointer directement sur /data. Pas de lien
# symbolique depuis /var/www/html/data : le confinement AppArmor des addons
# bloque la traversee d'un tel lien entre la couche d'image du conteneur et
# un volume monte separement, meme avec des permissions Unix par ailleurs
# correctes de bout en bout (diagnostique le 18/07 : /data direct
# fonctionnait pour www-data, /var/www/html/data echouait systematiquement).
mkdir -p "$DATA_DIR/sessions"
chown -R www-data:www-data "$DATA_DIR"

php -f /usr/local/bin/bsa-bootstrap.php

# Publication du service Discovery Supervisor en arrière-plan.
# Le script attend que config.json soit prêt avant de publier, donc
# il n'y a pas de race condition avec le bootstrap ci-dessus.
# L'exécution en arrière-plan (&) évite de bloquer le démarrage d'Apache
# si le Supervisor est temporairement indisponible.
sh /usr/local/bin/bsa-publish-discovery.sh &

echo "[Brightspace Agenda] Démarrage Apache sur le port 8099 (Ingress + accès direct)"
exec apache2-foreground
