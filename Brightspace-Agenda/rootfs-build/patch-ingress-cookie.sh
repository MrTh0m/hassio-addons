#!/bin/sh
# patch-ingress-cookie.sh
#
# Unique modification apportee au code source recupere depuis
# github.com/MrTh0m/Brightspace_agenda : le flag "Secure" du cookie de
# session doit suivre l'en-tete X-Forwarded-Proto envoye par le reverse
# proxy Ingress de Home Assistant. Sans ce correctif, sur une instance HA
# servie en HTTP en local, le navigateur ne renverrait jamais le cookie
# Secure et la connexion au mode "connecte" ne survivrait pas (y compris
# juste apres le login, qui regenere son propre cookie de session).
#
# Comportement inchange pour tout hebergement hors Home Assistant : si
# l'en-tete est absent, on retombe sur "true" (valeur d'origine).
#
# api.php definit ce cookie a deux endroits (session_set_cookie_params en
# tete de fichier, puis setcookie() apres session_regenerate_id() au login).
# Ce script echoue bruyamment (exit != 0, build Docker en erreur) si l'un des
# deux motifs attendus n'est plus present, plutot que de laisser passer un
# correctif partiel ou silencieusement inoperant.

set -e

TARGET_FILE="/var/www/html/api.php"

if [ ! -f "$TARGET_FILE" ]; then
    echo "[patch-ingress-cookie] ERREUR : $TARGET_FILE introuvable" >&2
    exit 1
fi

INGRESS_AWARE="(\$_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '') !== 'http'"

# 1) session_set_cookie_params() - premiere occurrence, avec son commentaire d'origine
NEEDLE_1="'secure'   => true,   // HTTPS uniquement"
REPLACEMENT_1="'secure'   => ${INGRESS_AWARE},   // Ingress-aware, voir patch-ingress-cookie.sh"

if ! grep -qF "$NEEDLE_1" "$TARGET_FILE"; then
    echo "[patch-ingress-cookie] ERREUR : motif 1/2 (session_set_cookie_params) introuvable, code source modifie ?" >&2
    exit 1
fi
sed -i "s#$NEEDLE_1#$REPLACEMENT_1#" "$TARGET_FILE"

# 2) setcookie() au login - deuxieme occurrence, sans commentaire
NEEDLE_2="'secure'   => true,"
REPLACEMENT_2="'secure'   => ${INGRESS_AWARE},"

if [ "$(grep -cF "$NEEDLE_2" "$TARGET_FILE")" -ne 1 ]; then
    echo "[patch-ingress-cookie] ERREUR : motif 2/2 (setcookie au login) absent ou en nombre inattendu, code source modifie ?" >&2
    exit 1
fi
sed -i "s#$NEEDLE_2#$REPLACEMENT_2#" "$TARGET_FILE"

if [ "$(grep -cF 'HTTP_X_FORWARDED_PROTO' "$TARGET_FILE")" -ne 2 ]; then
    echo "[patch-ingress-cookie] ERREUR : les deux patchs ne se sont pas appliques correctement" >&2
    exit 1
fi

echo "[patch-ingress-cookie] OK : les 2 cookies de session sont adaptes a l'Ingress Home Assistant"
