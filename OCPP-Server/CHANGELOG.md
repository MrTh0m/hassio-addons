# Changelog

## 0.8.0

- **Fix majeur : statut et pilotage étaient au niveau de la borne, pas du connecteur.** Le pont MQTT publiait un seul statut/switch par borne (basé sur le connecteur 0, "la borne elle-même"), alors qu'une borne à plusieurs connecteurs peut avoir des états différents sur chacun (l'un en charge, l'autre disponible). Chaque connecteur a maintenant ses propres entités MQTT (statut, puissance, énergie, durée, switch), regroupées sous le même appareil que la borne. Testé avec des statuts différents sur deux connecteurs simultanément.
- Page admin réécrite : "détails" s'ouvre maintenant sur chaque connecteur individuellement (fenêtre modale dédiée, avec son propre contrôle démarrer/arrêter, ses sessions et ses valeurs de compteur), plutôt qu'un panneau mélangeant tout au niveau de la borne.
- Nouveaux filtres `connector_id` sur `GET /chargers/{id}/sessions` et `GET /chargers/{id}/metervalues`.
- Bandeau de la page admin renommé "OCPP Server" (le nom de l'add-on dans le store Home Assistant reste "OCPP Backoffice Server").
- Description de l'add-on étoffée, notamment sur le fonctionnement du mode relais.

## 0.7.0

- **Fix : aucune entité n'apparaissait côté Home Assistant malgré une connexion MQTT réussie.** La découverte n'était publiée qu'au moment précis où une borne se connectait au WebSocket. Si le client MQTT n'était pas encore prêt à cet instant (typiquement juste après un redémarrage du conteneur), la publication tombait dans le vide et n'était jamais retentée. La découverte (et le dernier statut connu) est maintenant republiée pour toutes les bornes connues à chaque (re)connexion réussie au broker.

## 0.6.0

- **Fix (auth MQTT) : "Not authorised" à la connexion.** L'add-on utilise maintenant le mécanisme officiel de service Supervisor (`services: ["mqtt:want"]`) pour récupérer automatiquement les identifiants MQTT provisionnés pour les add-ons (l'utilisateur `addons`), au lieu de demander de créer un utilisateur Mosquitto à la main. Configuration manuelle possible et prioritaire si besoin (broker externe, autre instance HA).
- Nouveau fichier `resolve_config.py`, appelé au démarrage par `run.sh`.

## 0.5.0

- `mqtt_base_topic` configurable, pour éviter tout conflit avec d'autres intégrations (Zigbee2MQTT, etc.) sur le même broker.
- Page `/admin` : bandeau titre avec numéro de version, bascule thème clair/sombre, pied de page.
- Nouvel endpoint `GET /api/version`.

## 0.4.0

- **Pont MQTT vers Home Assistant** : chaque borne connectée publie automatiquement des capteurs (statut, puissance, énergie, durée) et un switch "Autoriser la charge" (mode local uniquement), avec pilotage réel de la borne (RemoteStart/Stop) et reconnexion automatique au broker.

## 0.3.0

- Fix (conformité OCPP) : timestamps sans indicateur de fuseau UTC (`Z`). Corrigé.
- Fix (conformité OCPP) : le statut de chaque connecteur écrasait celui des autres, maintenant stocké séparément.
- Nouvel endpoint `GET /api/chargers/{id}/connectors`.
- Fix : `run.sh` en `#!/bin/sh` (bash absent de l'image Alpine).
- La route WebSocket OCPP est `/ocpp/{id_borne}`.

## 0.2.0

- Ajout d'une page d'administration basique (`/admin`).
- La racine `/` redirige vers `/admin`.

## 0.1.0

- Première version : cœur CSMS OCPP 1.6, pilotage à distance, lecture/écriture de configuration.
- Mode relais par borne : proxy transparent vers un serveur OCPP officiel, avec capture passive des métriques.
- API REST authentifiée (JWT), un compte admin créé au premier démarrage.
- Stockage SQLite dans `/data` (persistant).
- Support de 5 architectures (aarch64, amd64, armhf, armv7, i386).

### Limitations connues
- OCPP 2.0.1 non implémenté.
- Un seul compte administrateur, pas de gestion multi-utilisateurs.
- Pas de graphes ni de calcul de coûts (association véhicules, tarifs électriques : à venir).
- Rattachement de transaction incomplet sur StopTransaction en mode relais.
