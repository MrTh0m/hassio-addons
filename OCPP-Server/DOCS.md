# OCPP Server (Backoffice)

Serveur OCPP 1.6 (CSMS) auto-hébergé : accueille une ou plusieurs bornes, avec pour chacune un choix entre pilotage local complet ou relais transparent vers le serveur officiel du fabricant. API REST, page d'administration à onglets, pont MQTT vers Home Assistant, et suivi des coûts par véhicule et par tarif.

## Deux modes, par borne

- **Local** : le backoffice devient le central system de la borne. Contrôle complet (démarrer/arrêter une charge, lire/modifier sa configuration), entités MQTT avec switch de pilotage.
- **Relais** : tu indiques l'adresse du serveur OCPP officiel de la borne (celui de son fabricant), et l'add-on se place en intermédiaire transparent entre les deux. La borne continue de parler avec son serveur d'origine sans interruption ni modification du trafic, mais l'add-on capte au passage les métriques (statut, puissance, énergie) pour les exposer ici et dans Home Assistant. Utile si tu veux garder ta borne gérée par l'appli de son fabricant tout en ayant les métriques dans HA.

## Premier démarrage

1. Démarre l'add-on. Un compte `admin` est créé automatiquement, avec le mot de passe défini dans l'onglet "Configuration" de l'add-on (`admin_password`, à changer avant le premier démarrage si possible).
2. Ouvre `http://<ip-de-ton-serveur>:8000` (ou via le panneau latéral, ingress) : ça ouvre directement la page d'administration.
3. La documentation interactive Swagger reste disponible sur `/docs`.

## Connecter une borne

Configure la borne pour qu'elle se connecte à :

```
ws://<ip-de-ton-serveur>:8000/ocpp/<identifiant-de-la-borne>
```

À la première connexion, la borne est enregistrée automatiquement en mode `local`.

## La page d'administration, par onglet

**Bornes** : liste des bornes, avec pour chacune le réglage du mode (local/relais), le tarif appliqué, et le détail de ses connecteurs. Chaque connecteur a sa propre fenêtre (statut, contrôle démarrer/arrêter en mode local, sessions récentes, dernières valeurs de compteur), puisque plusieurs connecteurs sur une même borne peuvent être dans des états différents.

**Véhicules** : associe un idTag (badge) à un nom de véhicule (et, si tu veux, sa capacité de batterie, informatif). Dès qu'une session démarre avec ce badge, elle est automatiquement rattachée à ce véhicule, aussi bien en mode local qu'en mode relais.

**Tarifs** : un plan par contexte (ex. "Domicile", "Travail"), avec un prix fixe de secours et autant de plages horaires nommées que nécessaire (heures pleines/creuses classiques, mais aussi n'importe quel autre découpage : creuses l'après-midi, tarif week-end séparé, etc.). Un plan peut être marqué "par défaut" (utilisé pour les bornes sans tarif assigné explicitement). En cas de chevauchement entre deux plages définies sur un même plan, la première de la liste l'emporte. Le coût d'une session est calculé en découpant son énergie par tranche de temps entre relevés successifs et en appliquant le tarif actif à chaque tranche, donc une session qui chevauche plusieurs plages est bien répartie plutôt que facturée à un seul prix.

**Historique** : toutes les sessions, toutes bornes confondues, avec véhicule, kWh, coût et tarif appliqué, filtrable par véhicule.

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
- **Pas encore de graphes.** Les données existent (sessions, kWh, coûts), l'affichage graphique reste à construire.
- **Icône/logo génériques.** Une icône simple a été fournie séparément (voir le dépôt), rien n'empêche de la remplacer plus tard.
- **Mode relais, StopTransaction :** l'identifiant de transaction assigné par le serveur officiel n'est pas encore relié à la transaction correspondante en base à la clôture.
- **Association véhicule rétroactive impossible** : une session démarrée avant la création du véhicule correspondant (ou avec un idTag différent) ne sera pas rattachée après coup automatiquement.

## Licence

Basé sur [`mobilityhouse/ocpp`](https://github.com/mobilityhouse/ocpp) (MIT) pour la couche protocole.
