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

# ─────────────────────────────────────────────────────────────────────────
# DIAGNOSTIC TEMPORAIRE (à retirer une fois le 503 sur api.php expliqué) :
# is_writable(DATA_DIR) échoue côté Apache/www-data alors que le bootstrap
# ci-dessus (root) écrit sans problème dans le même dossier. On compare
# permissions reelles et test d'ecriture simule en www-data pour trancher
# entre un problème d'ownership et une restriction open_basedir du SAPI
# Apache (qui n'affecterait pas le CLI utilise par le bootstrap).
echo "[Brightspace Agenda][DIAG] ls -la /data :"
ls -la /data
echo "[Brightspace Agenda][DIAG] ls -la -L /var/www/html/data (via symlink) :"
ls -la -L /var/www/html/data 2>&1
echo "[Brightspace Agenda][DIAG] test d'ecriture simule en www-data :"
su -s /bin/sh www-data -c 'test -w /data && echo "[Brightspace Agenda][DIAG] /data : WRITABLE pour www-data" || echo "[Brightspace Agenda][DIAG] /data : NON WRITABLE pour www-data"'
echo "[Brightspace Agenda][DIAG] open_basedir eventuel (CLI) :"
php -i 2>/dev/null | grep -i open_basedir || echo "[Brightspace Agenda][DIAG] (aucune sortie php -i)"
echo "[Brightspace Agenda][DIAG] open_basedir eventuel dans les fichiers ini Apache :"
grep -ri open_basedir /usr/local/etc/php/php.ini /usr/local/etc/php/conf.d/*.ini 2>/dev/null || echo "[Brightspace Agenda][DIAG] aucune directive open_basedir trouvee dans les .ini"
# ─────────────────────────────────────────────────────────────────────────

# Publication du service Discovery Supervisor en arrière-plan.
# Le script attend que config.json soit prêt avant de publier, donc
# il n'y a pas de race condition avec le bootstrap ci-dessus.
# L'exécution en arrière-plan (&) évite de bloquer le démarrage d'Apache
# si le Supervisor est temporairement indisponible.
sh /usr/local/bin/bsa-publish-discovery.sh &

echo "[Brightspace Agenda] Démarrage Apache sur le port 8099 (Ingress + accès direct)"
exec apache2-foreground
