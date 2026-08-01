# OCPP Backoffice Server

Serveur OCPP 1.6 (CSMS) auto-hébergé : accueille une ou plusieurs bornes, avec pour chacune un choix entre pilotage local complet ou relais transparent vers le serveur officiel du fabricant. Expose une API REST, une page d'administration basique, et un pont MQTT vers Home Assistant (ou tout autre système compatible MQTT Discovery).

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

Le serveur publie automatiquement, pour chaque borne connectée, les entités suivantes via le protocole MQTT Discovery de Home Assistant (aucune configuration à faire côté HA au-delà d'avoir un broker MQTT actif, ex. l'add-on Mosquitto broker) :

- **Capteurs** : statut, puissance (W), énergie (Wh), durée de la charge en cours (min)
- **Switch "Autoriser la charge"** (borne en mode local uniquement) : bascule ON pour démarrer une charge à distance (connecteur 1), OFF pour l'arrêter

Les entités apparaissent dans HA sous un appareil nommé "Borne \<identifiant\>", regroupées automatiquement.

**Configuration du broker** (onglet "Configuration" de l'add-on) :

| Option | Par défaut | Description |
|---|---|---|
| `mqtt_enabled` | `true` | Active/désactive le pont MQTT |
| `mqtt_host` | `core-mosquitto` | Nom d'hôte du broker (le nom standard de l'add-on Mosquitto officiel sur le réseau interne HA) |
| `mqtt_port` | `1883` | Port du broker |
| `mqtt_username` / `mqtt_password` | vide | Si le broker exige une authentification |

## Limitations connues de cette version

- **OCPP 1.6 uniquement.** Le 2.0.1 n'est pas encore implémenté.
- **Un seul compte administrateur.** Pas encore de gestion multi-utilisateurs.
- **Page admin et entités MQTT volontairement minimales** : pas de graphes ni de coûts, c'est un outil d'administration/supervision, pas l'app "consommateur final" prévue plus loin dans l'architecture.
- **Switch de pilotage limité au connecteur 1.** Pas de pilotage individuel du connecteur 2 pour l'instant, ni côté MQTT ni côté admin (l'API REST, elle, accepte déjà n'importe quel `connector_id`).
- **Mode relais, StopTransaction :** l'identifiant de transaction assigné par le serveur officiel n'est pas encore relié à la transaction correspondante en base à la clôture.

## Licence

Basé sur [`mobilityhouse/ocpp`](https://github.com/mobilityhouse/ocpp) (MIT) pour la couche protocole.
