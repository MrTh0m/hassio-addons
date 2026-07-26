#!/usr/bin/env bash
set -e

# Le simulateur écrit toujours ses données persistantes (dont la config WebSocket)
# dans /mo_store, un chemin absolu figé en dur dans le code source. Ce dossier
# n'est pas persistant par défaut : on le fait pointer vers /data (le stockage
# garanti persistant par Home Assistant pour chaque add-on) via un lien symbolique,
# créé à chaque démarrage puisque /data n'existe pas encore au moment du build.
mkdir -p /data/mo_store
if [ ! -L /mo_store ]; then
    rm -rf /mo_store
    ln -s /data/mo_store /mo_store
fi

echo "[MicroOcppSimulator] Démarrage sur le port 8000..."
cd /opt/microocppsimulator
exec ./build/mo_simulator
