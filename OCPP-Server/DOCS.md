# OCPP Server (Backoffice)

Serveur OCPP 1.6 (CSMS) auto-hébergé : accueille une ou plusieurs bornes, avec pour chacune un choix entre pilotage local complet ou relais transparent vers le serveur officiel du fabricant. Interface d'administration complète, comptes multi-utilisateurs, suivi des coûts par véhicule et par tarif, programmation de charge, et intégration Home Assistant via MQTT Discovery.

## Deux modes, par borne

- **Local** : le backoffice devient le central system de la borne. Contrôle complet (démarrer/arrêter une charge, lire/modifier sa configuration OCPP, programmer la charge), entités MQTT avec switch de pilotage.
- **Relais** : tu indiques l'adresse du serveur OCPP officiel de la borne (celui de son fabricant), et l'add-on se place en intermédiaire transparent entre les deux. La borne continue de parler avec son serveur d'origine sans interruption ni modification du trafic, mais l'add-on capte au passage les métriques (statut, puissance, énergie) pour les exposer ici et dans Home Assistant. Utile si tu veux garder ta borne gérée par l'appli de son fabricant tout en ayant les métriques dans HA.

## Premier démarrage

1. Démarre l'add-on. Un compte `admin` est créé automatiquement, avec le mot de passe défini dans l'onglet "Configuration" de l'add-on (`admin_password`, à changer avant le premier démarrage si possible). Ce mot de passe reste synchronisé depuis la configuration HA à chaque redémarrage ; il ne se change pas depuis l'interface.
2. Ouvre `http://<ip-de-ton-serveur>:8000` (ou via le panneau latéral, ingress) : ça ouvre directement la page d'administration.
3. La documentation interactive Swagger reste disponible sur `/docs`.

## Connecter une borne

Configure la borne pour qu'elle se connecte à :

```
ws://<ip-de-ton-serveur>:8000/ocpp/<identifiant-de-la-borne>
```

À la première connexion, la borne est enregistrée automatiquement en mode `local`. Un nom d'affichage (facultatif) peut ensuite lui être donné depuis sa fiche de configuration, pour remplacer son identifiant technique (souvent un UUID peu lisible) dans toute l'interface.

## La page d'administration, par onglet

**Accueil** : synthèse de l'instant présent — occupation de l'alimentation électrique (puissance délivrée cumulée / puissance souscrite), charge(s) en cours avec bouton d'arrêt, bornes disponibles, dernière charge terminée (modifiable directement).

**Bornes** : liste des bornes (actives et désactivées, ces dernières grisées), avec pour chacune le réglage du mode (local/relais), l'autorisation de charge (libre ou badge/bouton requis), le tarif appliqué, et le détail de ses connecteurs. Chaque borne a sa propre fenêtre de configuration, avec deux onglets : **Réglages** (mode, autorisation, tarif, nom d'affichage, programmation de charge) et **Configuration OCPP** (clés lues et modifiables en direct sur la borne via GetConfiguration/ChangeConfiguration, avec filtre de recherche — utile notamment pour régler `MeterValueSampleInterval` et activer la remontée de données en temps réel sur les bornes qui ne le font pas par défaut). Une borne désactivée redevient active automatiquement si elle se reconnecte physiquement.

**Véhicules** : associe un idTag (badge) à un nom de véhicule (et, si tu veux, sa capacité de batterie, utilisée pour estimer le taux de recharge). Dès qu'une session démarre avec ce badge, elle est automatiquement rattachée à ce véhicule, aussi bien en mode local qu'en mode relais. Un véhicule désactivé reste visible (grisé) et consultable dans l'historique, mais ne peut plus recevoir de nouvelle charge tant qu'il n'est pas réactivé ; une suppression définitive (irréversible, avec tout son historique) reste possible depuis un véhicule déjà désactivé.

**Abo. électrique** : un plan tarifaire par contexte (ex. "Domicile", "Travail"), avec un prix fixe de secours et autant de plages horaires nommées que nécessaire (heures pleines/creuses classiques, mais aussi n'importe quel autre découpage : creuses l'après-midi, tarif week-end séparé, etc.). Un plan peut être marqué "par défaut" (utilisé pour les bornes sans tarif assigné explicitement), et chaque borne peut avoir son propre abonnement (puissance souscrite en kVA, utilisée pour l'indicateur d'occupation de l'Accueil). Le coût d'une session est calculé en découpant son énergie par tranche de temps entre relevés successifs et en appliquant le tarif actif à chaque tranche, donc une session qui chevauche plusieurs plages est bien répartie plutôt que facturée à un seul prix. Le coût et l'énergie d'une session terminée sont figés définitivement à sa clôture : une modification ultérieure des tarifs n'affecte jamais rétroactivement une charge déjà passée.

**Historique** : toutes les sessions, toutes bornes confondues (plus les charges ajoutées manuellement pour des bornes tierces), avec véhicule, durée, kWh, coût, tarif appliqué, kilométrage et consommation (kWh/100 km), filtrable par véhicule. Chaque session est modifiable a posteriori (véhicule, kilométrage, niveaux de batterie ; en plus l'énergie et le coût pour une charge externe). Une session dont l'énergie ou le coût semble incorrect peut être recalculée en un clic depuis `meter_start`/`meter_stop`, en ignorant les éventuels relevés de compteur corrompus. Export CSV disponible, filtres actifs respectés.

**Logs** *(réservé admin, nécessite le Mode débug)* : journal des messages OCPP échangés avec les bornes, en direct, filtrable par borne / type de message / sens. Non persisté (vidé au redémarrage). La capture des messages à fort volume (MeterValues) est désactivable séparément. Export CSV disponible.

**Base de données** *(réservé admin, nécessite le Mode débug)* : navigateur en lecture seule du contenu brut des tables (bornes, sessions, relevés de compteur, tarifs, véhicules, conditions de charge, clés de configuration, statuts de connecteur), paginé, trié du plus récent au plus ancien. Ne couvre pas la table des utilisateurs. Export CSV par table.

## Comptes et droits

Au-delà du compte `admin` (qui voit et peut tout faire par définition), des comptes secondaires peuvent être créés depuis Réglages → Utilisateurs, avec :

- Une association à un sous-ensemble de véhicules et de bornes (un compte ne voit et ne peut démarrer/arrêter une charge que sur ce qui lui est associé, sur les deux à la fois).
- Des droits granulaires optionnels : gérer les bornes, gérer les abonnements électriques, gérer les véhicules, exporter/importer les données. Les logs et la base de données restent réservés à `admin` avec le Mode débug activé, indépendamment de ces droits.
- La possibilité de changer son propre mot de passe depuis Réglages → Mon compte (sauf le compte `admin`, voir plus haut).

Un compte créant un véhicule en devient automatiquement associé.

## Programmation de la charge

Depuis la fiche de configuration d'une borne, une ou plusieurs conditions peuvent être ajoutées par connecteur (ou pour tous) : départ différé à une heure donnée, restriction aux heures creuses du tarif actif, ou prête pour une heure cible. Plusieurs conditions se combinent en ET.

La programmation repose sur le support SmartCharging de la borne (limitation de puissance à 0 W pour mettre la charge en pause sans jamais clore la transaction, ce qui préserve l'historique). Le support est détecté automatiquement à la connexion ; si une borne déclare explicitement ne pas le supporter, la création de conditions lui est refusée plutôt que de proposer un repli brutal par démarrage/arrêt.

## Sauvegarde et restauration

Réglages → Données propose un export JSON complet (toutes les tables sauf les comptes utilisateurs) et un import en deux modes : **remplacer** (vide puis restaure à l'identique) ou **fusionner** (insère/met à jour sans rien supprimer). Une suppression totale de l'historique (irréversible) est aussi disponible en zone de danger, réservée admin.

## Intégration Home Assistant (MQTT)

Le serveur publie automatiquement, via MQTT Discovery :

- Un capteur de **statut global** par borne (reflète le connecteur 0, "la borne elle-même" au sens de la norme OCPP, pas un connecteur physique)
- Par **connecteur** physique : statut, puissance (W), énergie (Wh), durée de la charge en cours (min), et un switch "Autoriser la charge" (borne en mode local uniquement)

Toutes les entités d'une même borne sont regroupées sous un seul appareil "Borne \<identifiant\>" (ou son nom d'affichage, si défini) dans HA.

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
- **Pas encore de graphes.** Les données existent (sessions, kWh, coûts, relevés de compteur), l'affichage graphique reste à construire.
- **Icône/logo génériques.** Une icône simple a été fournie séparément (voir le dépôt), rien n'empêche de la remplacer plus tard.
- **Mode relais, StopTransaction :** l'identifiant de transaction assigné par le serveur officiel n'est pas encore relié à la transaction correspondante en base à la clôture.
- **Association véhicule rétroactive impossible** : une session démarrée avant la création du véhicule correspondant (ou avec un idTag différent) ne sera pas rattachée après coup automatiquement.
- **Bornes sans remontée périodique de compteur** : certaines bornes (notamment en OCPP 1.6 basique) n'envoient de relevé qu'à la fin de la charge, pas pendant. L'énergie et la puissance en temps réel restent alors à zéro jusqu'à la clôture ; configurer `MeterValueSampleInterval` depuis l'onglet Configuration OCPP de la borne (si elle le supporte) résout ce point.

## Licence

Basé sur [`mobilityhouse/ocpp`](https://github.com/mobilityhouse/ocpp) (MIT) pour la couche protocole.
