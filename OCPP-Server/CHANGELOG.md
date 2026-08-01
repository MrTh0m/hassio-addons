# Changelog

## 0.7.0

- **Fix : aucune entité n'apparaissait côté Home Assistant malgré une connexion MQTT réussie.** La découverte n'était publiée qu'au moment précis où une borne se connectait au WebSocket. Si le client MQTT n'était pas encore prêt à cet instant (typiquement juste après un redémarrage du conteneur, quand la borne se reconnecte plus vite que MQTT), la publication tombait dans le vide et n'était jamais retentée tant que la borne restait connectée. La découverte (et le dernier statut connu) est maintenant republiée pour toutes les bornes connues à chaque (re)connexion réussie au broker.
- Testé : une borne déjà en base mais sans connexion WebSocket active reçoit bien sa configuration de découverte dès que le serveur se connecte au broker.

## 0.6.0

- **Fix (auth MQTT) : "Not authorised" à la connexion.** L'add-on utilise maintenant le mécanisme officiel de service Supervisor (`services: ["mqtt:want"]`) pour récupérer automatiquement les identifiants MQTT provisionnés pour les add-ons (l'utilisateur `addons`, avec les bonnes permissions déjà accordées), au lieu de demander de créer un utilisateur Mosquitto à la main. Si `mqtt_username` est laissé vide dans la configuration de l'add-on, la détection est automatique ; le renseigner manuellement reste possible et prioritaire (utile pour un broker externe à Home Assistant, y compris sur une autre instance).
- Nouveau fichier `resolve_config.py`, appelé au démarrage par `run.sh`, qui centralise la lecture des options et cette auto-détection.
- Testé : détection automatique (via un faux service Supervisor), configuration manuelle prioritaire, et échappement correct des valeurs contenant guillemets/`$`/espaces.

## 0.5.0

- Le "base topic" MQTT est maintenant configurable (`mqtt_base_topic`, par défaut `ocppserver`), pour éviter tout conflit si d'autres intégrations (Zigbee2MQTT, etc.) partagent le même broker.
- Page `/admin` : ajout d'un bandeau titre avec le numéro de version, d'une bascule thème clair/sombre (mémorisée, respecte la préférence système par défaut), et d'un pied de page (version, lien vers le dépôt, mention de licence).
- Nouvel endpoint `GET /api/version`.

## 0.4.0

- **Pont MQTT vers Home Assistant** (et tout autre système lisant le MQTT Discovery de HA) : chaque borne connectée publie automatiquement, via MQTT :
  - des capteurs : statut, puissance (W), énergie (Wh), durée de la charge en cours (min)
  - un switch "Autoriser la charge" (mode local uniquement), qui déclenche un vrai `RemoteStartTransaction`/`RemoteStopTransaction` sur la borne
  - Fonctionne aussi bien en mode local qu'en mode relais pour les métriques. En mode relais, pas de switch de pilotage.
  - Reconnexion automatique au broker en cas de coupure.
- Testé de bout en bout avec un vrai broker Mosquitto : découverte des entités, remontée des états, et commande MQTT déclenchant bien un ordre OCPP réel vers la borne.

## 0.3.0

- Fix (conformité OCPP) : timestamps sans indicateur de fuseau UTC (`Z`), non conforme au type `dateTime` de la norme. Corrigé.
- Fix (conformité OCPP) : le statut de chaque connecteur écrasait celui des autres. Chaque connecteur a maintenant son propre statut stocké séparément.
- Nouvel endpoint `GET /api/chargers/{id}/connectors`.
- Fix : `run.sh` utilisait `#!/usr/bin/env bash`, absent de l'image Alpine. Passage à `#!/bin/sh`.
- La route WebSocket OCPP est `/ocpp/{id_borne}` (Backend URL = `ws://<ip>:8000/ocpp`, pas `ws://<ip>:8000`).

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
- Le switch de pilotage MQTT n'agit que sur le connecteur 1.
- La page `/admin` reste un outil d'administration (pas de graphes/coûts), pas l'app "consommateur final" prévue plus loin dans l'architecture.
