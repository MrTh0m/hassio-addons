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
chown -R www-data:www-data "$DATA_DIR"

php -f /usr/local/bin/bsa-bootstrap.php

# Publication du service Discovery Supervisor en arrière-plan.
# Le script attend que config.json soit prêt avant de publier, donc
# il n'y a pas de race condition avec le bootstrap ci-dessus.
# L'exécution en arrière-plan (&) évite de bloquer le démarrage d'Apache
# si le Supervisor est temporairement indisponible.
sh /usr/local/bin/bsa-publish-discovery.sh &

# ─────────────────────────────────────────────────────────────────────────
# DIAGNOSTIC TEMPORAIRE v4 (à retirer une fois le 503 sur api.php explique) :
# Le passage a BSA_DATA_DIR (variable d'environnement) au lieu du lien
# symbolique n'a pas resolu le 503 NO_WRITE (confirme par capture HAR :
# toujours 503, taille de reponse compatible avec le meme message d'erreur
# qu'avant). On verifie ici si BSA_DATA_DIR est reellement visible cote
# shell, PHP CLI, et PHP CLI en www-data (la variable ENV du Dockerfile est
# censee etre heritee par tous les processus du conteneur, y compris les
# workers Apache, mais on le confirme au lieu de le supposer).
echo "[Brightspace Agenda][DIAG] BSA_DATA_DIR vu par le shell : ${BSA_DATA_DIR:-(vide/absent)}"
echo "[Brightspace Agenda][DIAG] BSA_DATA_DIR vu par PHP CLI (root) :"
php -r 'var_dump(getenv("BSA_DATA_DIR"));'
echo "[Brightspace Agenda][DIAG] BSA_DATA_DIR vu par PHP CLI en www-data :"
su -s /bin/sh www-data -c "php -r 'var_dump(getenv(\"BSA_DATA_DIR\"));'"
echo "[Brightspace Agenda][DIAG] is_dir/is_writable sur le resultat reel de getenv, en www-data :"
su -s /bin/sh www-data -c "php -r '\$d = getenv(\"BSA_DATA_DIR\") ?: \"/var/www/html/data\"; echo \"chemin teste: \$d\n\"; var_dump(is_dir(\$d)); var_dump(is_writable(\$d));'"
# ─────────────────────────────────────────────────────────────────────────

echo "[Brightspace Agenda] Démarrage Apache sur le port 8099 (Ingress + accès direct)"
exec apache2-foreground
