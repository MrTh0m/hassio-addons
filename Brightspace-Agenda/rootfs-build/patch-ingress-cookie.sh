#!/bin/sh
# patch-ingress-cookie.sh
#
# Unique modification apportée au code source récupéré depuis
# github.com/MrTh0m/Brightspace_agenda : le flag "Secure" du cookie de
# session doit suivre l'en-tête X-Forwarded-Proto envoyé par le reverse
# proxy Ingress de Home Assistant. Sans ce correctif, sur une instance HA
# servie en HTTP en local, le navigateur ne renverrait jamais le cookie
# Secure et la connexion au mode "connecté" ne survivrait pas (y compris
# juste après le login, qui régénère son propre cookie de session).
#
# Comportement inchangé pour tout hébergement hors Home Assistant : si
# l'en-tête est absent, on retombe sur "true" (valeur d'origine).
#
# api.php définit ce cookie à deux endroits (session_set_cookie_params en
# tête de fichier, puis setcookie() après session_regenerate_id() au login).
# Ce script échoue bruyamment (exit != 0, build Docker en erreur) si l'un des
# deux motifs attendus n'est plus présent, plutôt que de laisser passer un
# correctif partiel ou silencieusement inopérant.

set -e

TARGET_FILE="/var/www/html/api.php"

if [ ! -f "$TARGET_FILE" ]; then
    echo "[patch-ingress-cookie] ERREUR : $TARGET_FILE introuvable" >&2
    exit 1
fi

INGRESS_AWARE="(\$_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '') !== 'http'"

# 1) session_set_cookie_params() - première occurrence, avec son commentaire d'origine
NEEDLE_1="'secure'   => true,   // HTTPS uniquement"
REPLACEMENT_1="'secure'   => ${INGRESS_AWARE},   // Ingress-aware, voir patch-ingress-cookie.sh"

if ! grep -qF "$NEEDLE_1" "$TARGET_FILE"; then
    echo "[patch-ingress-cookie] ERREUR : motif 1/2 (session_set_cookie_params) introuvable, code source modifié ?" >&2
    exit 1
fi
sed -i "s#$NEEDLE_1#$REPLACEMENT_1#" "$TARGET_FILE"

# 2) setcookie() au login - deuxième occurrence, sans commentaire
NEEDLE_2="'secure'   => true,"
REPLACEMENT_2="'secure'   => ${INGRESS_AWARE},"

if [ "$(grep -cF "$NEEDLE_2" "$TARGET_FILE")" -ne 1 ]; then
    echo "[patch-ingress-cookie] ERREUR : motif 2/2 (setcookie au login) absent ou en nombre inattendu, code source modifié ?" >&2
    exit 1
fi
sed -i "s#$NEEDLE_2#$REPLACEMENT_2#" "$TARGET_FILE"

if [ "$(grep -cF 'HTTP_X_FORWARDED_PROTO' "$TARGET_FILE")" -ne 2 ]; then
    echo "[patch-ingress-cookie] ERREUR : les deux patchs ne se sont pas appliqués correctement" >&2
    exit 1
fi

echo "[patch-ingress-cookie] OK : les 2 cookies de session sont adaptés à l'Ingress Home Assistant"
