# OCPP Backoffice Server

Serveur OCPP 1.6 (CSMS) auto-hébergé : accueille une ou plusieurs bornes, avec pour chacune un choix entre pilotage local complet ou relais transparent vers le serveur officiel du fabricant. Expose une API REST, une page d'administration, et un pont MQTT vers Home Assistant (ou tout autre système compatible MQTT Discovery).

## Premier démarrage

1. Démarre l'add-on. Un compte `admin` est créé automatiquement, avec le mot de passe défini dans l'onglet "Configuration" de l'add-on (`admin_password`, à changer avant le premier démarrage si possible).
2. Ouvre `http://<ip-de-ton-serveur>:8000` (ou via le panneau latéral, ingress) : ça ouvre directement la page d'administration (`/admin`).
3. La documentation interactive Swagger reste disponible sur `/docs`.

## Connecter une borne

Configure la borne pour qu'elle se connecte à :

```
ws://<ip-de-ton-serveur>:8000/ocpp/<identifiant-de-la-borne>
```

À la première connexion, la borne est enregistrée automatiquement en mode `local`.

## Mode local vs relais

Par défaut, une borne nouvellement connectée est en mode `local` : le serveur backoffice a le contrôle complet. Pour basculer en mode relais (utile si elle doit rester gérée par le serveur de son fabricant), depuis la page admin : ouvre les "détails" de la borne, choisis "relais", renseigne l'URL du serveur officiel, "Enregistrer". En mode relais, le pilotage est désactivé (API comme MQTT), seules les métriques et l'historique restent disponibles.

## Intégration Home Assistant (MQTT)

Le serveur publie automatiquement, pour chaque borne connectée, les entités suivantes via le protocole MQTT Discovery de Home Assistant :

- **Capteurs** : statut, puissance (W), énergie (Wh), durée de la charge en cours (min)
- **Switch "Autoriser la charge"** (borne en mode local uniquement) : bascule ON pour démarrer une charge à distance (connecteur 1), OFF pour l'arrêter

Les entités apparaissent dans HA sous un appareil nommé "Borne \<identifiant\>", regroupées automatiquement.

**Identifiants du broker : rien à faire dans le cas courant.** L'add-on déclare officiellement vouloir utiliser le service MQTT de Home Assistant (`services: mqtt:want`), ce qui lui donne accès, au démarrage, aux identifiants auto-provisionnés pour les add-ons (l'utilisateur `addons`, avec les permissions déjà accordées par le Supervisor) — le même mécanisme qu'utilisent Zigbee2MQTT et la plupart des add-ons du même genre. Si un add-on Mosquitto broker officiel est installé, ça fonctionne sans aucune configuration.

**Configuration manuelle** (onglet "Configuration" de l'add-on), seulement si besoin d'un broker externe ou différent :

| Option | Par défaut | Description |
|---|---|---|
| `mqtt_enabled` | `true` | Active/désactive le pont MQTT |
| `mqtt_host` | `core-mosquitto` | Utilisé seulement si `mqtt_username` est renseigné (sinon auto-détection) |
| `mqtt_port` | `1883` | idem |
| `mqtt_username` / `mqtt_password` | vide | Renseigner l'un ou l'autre désactive l'auto-détection et force ces identifiants |
| `mqtt_base_topic` | `ocppserver` | Préfixe des topics d'état/commande (pas ceux de discovery, qui restent sous `homeassistant/`). À changer si conflit avec une autre intégration sur le même broker. |

## Limitations connues de cette version

- **OCPP 1.6 uniquement.** Le 2.0.1 n'est pas encore implémenté.
- **Un seul compte administrateur.** Pas encore de gestion multi-utilisateurs.
- **Page admin et entités MQTT volontairement minimales** : pas de graphes ni de coûts, c'est un outil d'administration/supervision, pas l'app "consommateur final" prévue plus loin dans l'architecture.
- **Switch de pilotage limité au connecteur 1.** Pas de pilotage individuel du connecteur 2 pour l'instant, ni côté MQTT ni côté admin (l'API REST, elle, accepte déjà n'importe quel `connector_id`).
- **Mode relais, StopTransaction :** l'identifiant de transaction assigné par le serveur officiel n'est pas encore relié à la transaction correspondante en base à la clôture.

## Licence

Basé sur [`mobilityhouse/ocpp`](https://github.com/mobilityhouse/ocpp) (MIT) pour la couche protocole.
