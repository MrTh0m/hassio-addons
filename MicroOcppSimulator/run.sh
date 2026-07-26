#!/usr/bin/env bash
set -e

echo "[MicroOcppSimulator] Démarrage sur le port 8000..."
cd /opt/microocppsimulator
exec ./build/mo_simulator
