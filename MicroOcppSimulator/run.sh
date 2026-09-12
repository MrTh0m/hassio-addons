#!/usr/bin/env bash
set -e

# Le simulateur écrit ses données persistantes (dont la config WebSocket) dans
# le chemin défini par MO_FILENAME_PREFIX, fixé dans le CMakeLists.txt du dépôt
# officiel à "./mo_store/" (chemin RELATIF, résolu par rapport au répertoire de
# travail du processus, /opt/microocppsimulator, et non /mo_store à la racine).
# Ce dossier n'est pas persistant par défaut : on le fait pointer vers /data (le
# stockage garanti persistant par Home Assistant pour chaque add-on) via un lien
# symbolique relatif, créé à chaque démarrage puisque /data n'existe pas encore
# au moment du build.
mkdir -p /data/mo_store

cd /opt/microocppsimulator

if [ ! -L mo_store ]; then
    rm -rf mo_store
    ln -s /data/mo_store mo_store
fi

echo "[MicroOcppSimulator] Démarrage sur le port 8000..."
exec ./build/mo_simulator
