# OCPP Server (Backoffice)

Serveur OCPP 1.6 (CSMS) auto-hébergé : accueille une ou plusieurs bornes, avec pour chacune un choix entre pilotage local complet ou relais transparent vers le serveur officiel du fabricant. Expose une API REST, une page d'administration, et un pont MQTT vers Home Assistant (ou tout autre système compatible MQTT Discovery), avec des entités séparées par connecteur physique.

## Deux modes, par borne

- **Local** : le backoffice devient le central system de la borne. Contrôle complet (démarrer/arrêter une charge, lire/modifier sa configuration), entités MQTT avec switch de pilotage.
- **Relais** : tu indiques l'adresse du serveur OCPP officiel de la borne (celui de son fabricant), et l'add-on se place en intermédiaire transparent entre les deux. La borne continue de parler avec son serveur d'origine sans interruption ni modification du trafic, mais l'add-on capte au passage les métriques (statut, puissance, énergie) pour les exposer ici et dans Home Assistant. Utile si tu veux garder ta borne gérée par l'appli de son fabricant tout en ayant les métriques dans HA.

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

## Mode local vs relais, en pratique

Par défaut, une borne nouvellement connectée est en mode `local`. Pour basculer en mode relais, depuis la page admin : ouvre les "détails" de la borne, choisis "relais", renseigne l'URL du serveur officiel, "Enregistrer". En mode relais, le pilotage est désactivé (API comme MQTT), seules les métriques et l'historique restent disponibles.

## Page d'administration

Liste des bornes, avec pour chacune : le réglage du mode (local/relais), et le détail de ses connecteurs. Chaque connecteur a sa propre fiche (fenêtre dédiée) avec son statut, le contrôle démarrer/arrêter (en mode local), ses sessions récentes et ses dernières valeurs de compteur, puisque plusieurs connecteurs sur une même borne peuvent être dans des états différents (l'un en charge, l'autre disponible).

## Intégration Home Assistant (MQTT)

Le serveur publie automatiquement, via MQTT Discovery :

- Un capteur de **statut global** par borne (reflète le connecteur 0, "la borne elle-même" au sens de la norme OCPP, pas un connecteur physique)
- Par **connecteur** physique : statut, puissance (W), énergie (Wh), durée de la charge en cours (min), et un switch "Autoriser la charge" (borne en mode local uniquement)

Toutes les entités d'une même borne sont regroupées sous un seul appareil "Borne \<identifiant\>" dans HA.

**Identifiants du broker : rien à faire dans le cas courant.** L'add-on déclare officiellement vouloir utiliser le service MQTT de Home Assistant (`services: mqtt:want`), ce qui lui donne accès, au démarrage, aux identifiants auto-provisionnés pour les add-ons (l'utilisateur `addons`, avec les permissions déjà accordées par le Supervisor) — le même mécanisme qu'utilisent Zigbee2MQTT et la plupart des add-ons du même genre. Si un add-on Mosquitto broker officiel est installé, ça fonctionne sans aucune configuration.

**Configuration manuelle** (onglet "Configuration" de l'add-on), seulement si besoin d'un broker externe ou différent (y compris sur une autre instance Home Assistant) :

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
- **Pas de graphes ni de calcul de coûts.** C'est un outil d'administration/supervision, pas l'app "consommateur final" prévue plus loin dans l'architecture (association véhicules, tarifs électriques, historiques de coûts : à venir).
- **Icône/logo par défaut.** Pas encore d'identité visuelle personnalisée pour l'add-on (icône Material Design générique en attendant).
- **Mode relais, StopTransaction :** l'identifiant de transaction assigné par le serveur officiel n'est pas encore relié à la transaction correspondante en base à la clôture.

## Licence

Basé sur [`mobilityhouse/ocpp`](https://github.com/mobilityhouse/ocpp) (MIT) pour la couche protocole.
