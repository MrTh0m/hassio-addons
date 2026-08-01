# Changelog

## 0.3.0

- Fix (conformité OCPP) : les timestamps envoyés par le serveur (BootNotification, Heartbeat) n'incluaient pas d'indicateur de fuseau UTC (`Z`), non conforme au type `dateTime` de la norme. Corrigé.
- Fix (conformité OCPP) : le statut de chaque connecteur écrasait celui des autres connecteurs dans un unique champ. Chaque connecteur a maintenant son propre statut stocké séparément (nouvelle table `connector_statuses`), le champ résumé de la borne ne reflétant que le connecteur 0 (la borne elle-même, au sens de la norme), pas un connecteur physique.
- Nouvel endpoint `GET /api/chargers/{id}/connectors`, affiché dans la page admin.
- Fix : `run.sh` utilisait `#!/usr/bin/env bash`, absent de l'image Alpine. Passage à `#!/bin/sh`.
- La route WebSocket OCPP est bien `/ocpp/{id_borne}` (à rappeler dans la config du charge point : Backend URL = `ws://<ip>:8000/ocpp`, pas `ws://<ip>:8000`).

## 0.2.0

- Ajout d'une page d'administration basique (`/admin`) : connexion, liste des bornes, bascule local/relais, démarrage/arrêt de charge, sessions et dernières valeurs de compteur par borne.
- La racine `/` redirige vers `/admin`.

## 0.1.0

- Première version : cœur CSMS OCPP 1.6 (Boot, Heartbeat, Authorize, Status, StartTransaction, StopTransaction, MeterValues), pilotage à distance (RemoteStart/Stop), lecture/écriture de configuration (GetConfiguration/ChangeConfiguration).
- Mode relais par borne : proxy transparent vers un serveur OCPP officiel, avec capture passive des métriques.
- API REST authentifiée (JWT), un compte admin créé au premier démarrage.
- Stockage SQLite dans `/data` (persistant).
- Support de 5 architectures (aarch64, amd64, armhf, armv7, i386).

### Limitations connues
- OCPP 2.0.1 non implémenté.
- Un seul compte administrateur, pas de gestion multi-utilisateurs.
- Rattachement de transaction incomplet sur StopTransaction en mode relais.
- Pas de pont MQTT/Home Assistant (prévu pour une prochaine version).
- La page `/admin` est volontairement minimale (pas de graphes/coûts) : c'est un outil d'administration, pas encore l'app consommateur prévue plus loin dans l'architecture.
