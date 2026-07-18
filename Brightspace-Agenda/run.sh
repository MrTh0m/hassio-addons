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

# ─────────────────────────────────────────────────────────────────────────
# DIAGNOSTIC TEMPORAIRE v3 (à retirer une fois le 503 sur api.php expliqué) :
# v2 a confirme que root voit /var/www/html/data comme un dossier normal
# (stat -L, 0755 www-data:www-data) mais que www-data lui-meme echoue sur
# is_dir()/is_writable() du MEME chemin. v3 isole si le probleme est
# specifique a la traversee du lien symbolique ou touche /data en general
# pour www-data via PHP, verifie que su change bien d'UID effectif, et
# inspecte chaque niveau du chemin (permissions de traversee).
echo "[Brightspace Agenda][DIAG] UID effectif sous su www-data :"
su -s /bin/sh www-data -c 'id'
echo "[Brightspace Agenda][DIAG] is_dir/is_writable PHP sur /data DIRECTEMENT (sans passer par le lien), en www-data :"
su -s /bin/sh www-data -c "php -r 'var_dump(is_dir(\"/data\")); var_dump(is_writable(\"/data\"));'"
echo "[Brightspace Agenda][DIAG] ls -la sur chaque niveau du chemin :"
ls -ld / /var /var/www /var/www/html /var/www/html/data /data 2>&1
echo "[Brightspace Agenda][DIAG] namei (detail complet du chemin, si disponible) :"
namei -mo /var/www/html/data 2>&1 || echo "[Brightspace Agenda][DIAG] namei indisponible"
# ─────────────────────────────────────────────────────────────────────────

# Publication du service Discovery Supervisor en arrière-plan.
# Le script attend que config.json soit prêt avant de publier, donc
# il n'y a pas de race condition avec le bootstrap ci-dessus.
# L'exécution en arrière-plan (&) évite de bloquer le démarrage d'Apache
# si le Supervisor est temporairement indisponible.
sh /usr/local/bin/bsa-publish-discovery.sh &

echo "[Brightspace Agenda] Démarrage Apache sur le port 8099 (Ingress + accès direct)"
exec apache2-foreground
