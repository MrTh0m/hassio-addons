# Changelog

## 0.2.0

- Fix : `run.sh` utilisait `#!/usr/bin/env bash`, mais `bash` n'est pas installé dans l'image Alpine, provoquant l'erreur `env: can't execute 'bash'`. Passage à `#!/bin/sh`, seule syntaxe POSIX utilisée dans le script.
- Ajout d'une page d'administration basique (`/admin`), servie directement par le serveur (HTML/JS sans build), accessible aussi via l'ingress : connexion, liste des bornes, bascule local/relais, démarrage/arrêt de charge, sessions et dernières valeurs de compteur par borne.
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
