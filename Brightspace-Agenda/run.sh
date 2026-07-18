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
# DIAGNOSTIC TEMPORAIRE v2 (à retirer une fois le 503 sur api.php expliqué) :
# Le v1 testait /data (la cible reelle) et disait WRITABLE, mais le code PHP
# teste DATA_DIR = __DIR__.'/data' = /var/www/html/data (le lien symbolique
# lui-meme), pas /data directement. Ce v2 teste le bon chemin, avec is_writable()
# et is_dir() de PHP directement (memes fonctions qu'api.php, executees en
# www-data), et ajoute un contrôle AppArmor : un chemin accédé via un lien
# symbolique peut être mediatise differemment d'un acces direct par un
# profil de confinement, meme quand les permissions Unix classiques sont ok.
echo "[Brightspace Agenda][DIAG] test -w/-d sur /var/www/html/data (chemin reellement teste par PHP) :"
su -s /bin/sh www-data -c 'test -d /var/www/html/data && echo "[Brightspace Agenda][DIAG] is_dir shell : OK" || echo "[Brightspace Agenda][DIAG] is_dir shell : ECHEC"'
su -s /bin/sh www-data -c 'test -w /var/www/html/data && echo "[Brightspace Agenda][DIAG] is_writable shell : OK" || echo "[Brightspace Agenda][DIAG] is_writable shell : ECHEC"'
echo "[Brightspace Agenda][DIAG] memes tests via les fonctions PHP exactes (is_dir/is_writable), en www-data :"
su -s /bin/sh www-data -c "php -r 'var_dump(is_dir(\"/var/www/html/data\")); var_dump(is_writable(\"/var/www/html/data\"));'"
echo "[Brightspace Agenda][DIAG] stat -L /var/www/html/data (cible reelle apres resolution du lien) :"
stat -L /var/www/html/data 2>&1
echo "[Brightspace Agenda][DIAG] confinement AppArmor de ce process :"
cat /proc/self/attr/current 2>&1 || echo "[Brightspace Agenda][DIAG] pas d'info AppArmor accessible"
dmesg 2>/dev/null | grep -i apparmor | tail -5 || echo "[Brightspace Agenda][DIAG] dmesg indisponible ou aucune ligne apparmor"
# ─────────────────────────────────────────────────────────────────────────

# Publication du service Discovery Supervisor en arrière-plan.
# Le script attend que config.json soit prêt avant de publier, donc
# il n'y a pas de race condition avec le bootstrap ci-dessus.
# L'exécution en arrière-plan (&) évite de bloquer le démarrage d'Apache
# si le Supervisor est temporairement indisponible.
sh /usr/local/bin/bsa-publish-discovery.sh &

echo "[Brightspace Agenda] Démarrage Apache sur le port 8099 (Ingress + accès direct)"
exec apache2-foreground
