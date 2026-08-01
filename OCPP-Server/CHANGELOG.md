# Changelog

## 0.9.0

- **Fix : l'ingress affichait "404: Not Found".** La racine `/` faisait une redirection HTTP vers le chemin absolu `/admin`, ce qui fait sortir le navigateur du sous-chemin dynamique de l'ingress de Home Assistant. La page est maintenant servie directement (sans redirection) à la fois sur `/` et `/admin`.
- **Fix : tous les appels de l'API depuis la page admin utilisaient des chemins absolus (`/api/...`)**, qui échouent aussi sous ingress pour la même raison. Passés en chemins relatifs (`api/...`), compatibles à la fois en accès direct et via l'ingress.
- **Fix : entité fantôme "Autoriser la charge" au niveau de la borne.** L'ancien switch publié avant 0.8.0 (avant le passage au pilotage par connecteur) restait affiché côté HA car son message de découverte MQTT était retenu sur le broker et n'avait jamais été explicitement retiré. Il est maintenant supprimé automatiquement à la prochaine (re)connexion.
- **Fix : le panneau d'un connecteur ouvert dans la page admin ne se rafraîchissait pas automatiquement.** Le rafraîchissement périodique (5s) ignorait complètement les panneaux ouverts. Il rafraîchit maintenant le panneau actuellement affiché plutôt que de l'ignorer.
- Bandeau renommé "OCPP Server" (déjà fait en 0.8.0, confirmé).

## 0.8.0

- **Fix majeur : statut et pilotage étaient au niveau de la borne, pas du connecteur.** Chaque connecteur a maintenant ses propres entités MQTT (statut, puissance, énergie, durée, switch), regroupées sous le même appareil que la borne.
- Page admin réécrite : "détails" s'ouvre sur chaque connecteur individuellement (fenêtre modale dédiée).
- Nouveaux filtres `connector_id` sur `GET /chargers/{id}/sessions` et `GET /chargers/{id}/metervalues`.
- Description de l'add-on étoffée, notamment sur le fonctionnement du mode relais.

## 0.7.0

- **Fix : aucune entité n'apparaissait côté Home Assistant malgré une connexion MQTT réussie**, la découverte n'étant publiée qu'au moment précis d'une connexion WebSocket, parfois manqué juste après un redémarrage. Republiée désormais à chaque (re)connexion au broker.

## 0.6.0

- **Fix (auth MQTT) : "Not authorised" à la connexion.** Utilisation du mécanisme officiel de service Supervisor (`services: ["mqtt:want"]`) pour les identifiants auto-provisionnés (utilisateur `addons`), configuration manuelle toujours possible et prioritaire.
- Nouveau fichier `resolve_config.py`.

## 0.5.0

- `mqtt_base_topic` configurable.
- Page `/admin` : bandeau titre, bascule thème clair/sombre, pied de page.
- Nouvel endpoint `GET /api/version`.

## 0.4.0

- **Pont MQTT vers Home Assistant** : capteurs et switch "Autoriser la charge", pilotage réel de la borne, reconnexion automatique.

## 0.3.0

- Fix (conformité OCPP) : timestamps sans indicateur de fuseau UTC (`Z`). Corrigé.
- Fix (conformité OCPP) : statut par connecteur stocké séparément.
- Nouvel endpoint `GET /api/chargers/{id}/connectors`.
- Fix : `run.sh` en `#!/bin/sh`.

## 0.2.0

- Ajout d'une page d'administration basique (`/admin`).

## 0.1.0

- Première version : cœur CSMS OCPP 1.6, pilotage à distance, lecture/écriture de configuration.
- Mode relais par borne : proxy transparent vers un serveur OCPP officiel, avec capture passive des métriques.
- API REST authentifiée (JWT), un compte admin créé au premier démarrage.
- Stockage SQLite dans `/data` (persistant).
- Support de 5 architectures (aarch64, amd64, armhf, armv7, i386).

### Limitations connues
- OCPP 2.0.1 non implémenté.
- Un seul compte administrateur, pas de gestion multi-utilisateurs.
- Pas de graphes, véhicules ni tarifs (en cours de conception).
- Rattachement de transaction incomplet sur StopTransaction en mode relais.
