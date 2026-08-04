# Changelog

## 0.16.0

Grosse mise à jour fonctionnelle : programmation de la charge, suivi enrichi, fiches statistiques et correctif du mot de passe.

### Corrections

- **Fix : le changement de mot de passe depuis la configuration de l'add-on ne prenait pas effet.** Le hash de l'admin n'était écrit qu'à la toute première création du compte ; modifier `admin_password` dans les options HA ensuite n'avait aucun effet. Désormais, à chaque démarrage, le mot de passe de l'admin est resynchronisé avec la valeur des options (sentinelle `__keep__` pour ne pas toucher un mot de passe déjà changé en base si on laisse le champ à sa valeur par défaut).

### Programmation de la charge (nouveau)

- **Conditions de charge par borne** : heures creuses uniquement, départ différé après une heure, ou charge terminée pour une heure. Plusieurs conditions se cumulent (ET logique) et ne s'appliquent qu'à un connecteur branché. Un planificateur tourne en tâche de fond (tick 60 s).
- **Pilotage « les deux »** : le serveur utilise **SmartCharging** (`SetChargingProfile` / `ClearChargingProfile`) quand la borne le supporte, et bascule automatiquement sur démarrage/arrêt (`RemoteStart`/`RemoteStop`) sinon. Le support SmartCharging est détecté et mémorisé par borne.
- Nouveaux endpoints : `GET/POST /api/chargers/{id}/conditions`, `PUT/DELETE /api/chargers/{id}/conditions/{cid}`.
- À propos du délestage OCPP 1.6 côté CSMS : une borne délestée passe le connecteur en `SuspendedEVSE` (ou `SuspendedEV` si c'est le véhicule qui limite) **sans clore la transaction**. L'appli reconnaît ces états et les affiche comme « suspendu / délestage » (puce orangée) plutôt que comme une fin de charge ; la logique de clôture auto (qui ne se déclenche que sur `Available`) n'est donc pas triggerée à tort.

### Suivi et historique enrichis

- **Nouvelles colonnes dans l'historique** : puissance max de la charge, km parcourus depuis la charge précédente du même véhicule, et kWh/100km.
- **Taux de recharge de la batterie** : calculé à partir de la capacité du véhicule et de l'énergie délivrée, avec une **estimation du % final** à partir du « % init. ». Affiché dans l'historique et dans « Charge en cours ».
- **Renommage** : « % min » / « % max » deviennent « % init. » / « % fin » (plus clair : état au branchement / à la fin).
- **Charges externes** : possibilité d'enregistrer une charge faite sur une borne tierce (énergie, coût, lieu, date, km, %), pour garder une continuité de suivi par véhicule. Éditable et supprimable dans l'historique. Nouvel endpoint `POST /api/external-charges` ; `DELETE /api/sessions/{id}`.

### Fiches et indicateurs

- **Fiche statistiques par véhicule** : nombre de charges (dont externes), kWh et coût cumulés, distance, kWh/100km moyen, odomètre, et historique détaillé des charges.
- **Fiche statistiques par borne** : nombre de charges, kWh et coût cumulés, puissance max, et historique.
- **Occupation de l'alimentation** (Accueil) : par abonnement électrique, jauge de la puissance délivrée à l'instant T rapportée à la puissance souscrite (kVA), avec code couleur (orange ≥ 70 %, rouge ≥ 90 %). Nouvel endpoint `GET /api/occupancy`.

### Gestion et suppressions

- **Suppression d'une borne du CSMS** (suppression douce) : la borne disparaît de la liste mais **son historique de charges est conservé** ; si elle se reconnecte, elle réapparaît automatiquement.
- **Suppression d'un véhicule** (suppression douce) : ses charges passées restent dans l'historique, et son idTag est libéré.
- **Renommage de la page « Abonnements » en « Abo. électrique »** (« Abo. » sur mobile), et le champ « Batterie » du véhicule devient « Capacité de la batterie ».

### Interface

- **Bornes disponibles (Accueil)** : le bouton « Démarrer » n'apparaît que lorsqu'un véhicule est effectivement branché (état *Preparing*) ; un connecteur *Available* n'affiche plus qu'une puce de statut.
- **Détail d'un connecteur (onglet Bornes)** : la section démarrer/arrêter a été retirée (le détail devient lecture seule et renvoie vers l'Accueil pour agir), avec ajout d'une colonne « P. max » sur les sessions.
- Éléments regroupés sur une même ligne et boutons d'action (fiche / suppression) placés directement sur chaque carte de borne.

## 0.15.1

- **Démarrage automatique effectif au branchement (mode « sans autorisation »)** : en 0.15.0, une borne en mode `free` passait bien en *Preparing* quand on branchait un véhicule, mais rien ne lançait la charge (le simulateur — comme une borne sans free-vend configuré — n'émet pas de `StartTransaction` de lui-même). Le serveur envoie désormais un `RemoteStartTransaction` dès qu'un connecteur passe en *Preparing* en mode `free`, une seule fois par branchement (nouveau garde-fou `AUTO_START_ATTEMPTED`, réarmé au débranchement). Un arrêt manuel « véhicule encore branché » ne relance donc pas la charge.
- **Interface plus dense et plus lisible** : cartes, marges de sections et interlignes resserrés ; champs de saisie plus contrastés (nouveau token `--field-border` + légère ombre) ; les formulaires d'ajout (Véhicules, Abonnements) apparaissent comme une zone de saisie distincte (fond teinté + liseré). Sur mobile, séparateurs entre champs dans les cartes issues des tableaux.
- **Version + lien GitHub visibles** : ajout d'un pied de page (`v… · GitHub`) affiché sur toutes les tailles d'écran — le numéro de version n'apparaissait auparavant qu'en barre latérale, masquée sur mobile.

## 0.15.0

- **Refonte complète de l'interface** : nouvelle charte graphique (palette énergie/verte, typographie, cartes, puces de statut cohérentes), navigation latérale sur ordinateur et barre inférieure sur mobile, tableaux qui se replient en cartes sur petit écran. Fini l'aspect « maquette » : l'appli se veut réellement responsive et utilisable au quotidien depuis le téléphone. Les `alert()` sont remplacés par des notifications discrètes (toasts).
- **Application installable (PWA)** : manifeste web, icône vectorielle, et service worker (coquille hors-ligne). L'appli peut être « ajoutée à l'écran d'accueil » et lancée en plein écran. Le mode installable/hors-ligne complet fonctionne surtout via un accès HTTPS (Nabu Casa, reverse-proxy) ou en localhost ; sous l'ingress HA il s'enregistre dans le sous-chemin de l'ingress, et une éventuelle indisponibilité est sans effet sur le fonctionnement.
- **Mode d'autorisation par borne** (nouveau champ `auth_mode`, mode local) :
  - **Sans autorisation** (`free`, comportement historique) : tout idTag est accepté, la charge peut démarrer automatiquement au branchement, sans badge ni bouton.
  - **Avec autorisation** (`authorized`) : seuls un idTag associé à un véhicule connu, ou un démarrage explicite depuis l'appli / MQTT, sont acceptés ; un badge inconnu est refusé (`Authorize`/`StartTransaction` renvoient `Blocked`). Réglable dans le détail de chaque borne. Nouvel endpoint `PUT /api/chargers/{id}/auth-mode`.
  - Note : pour un vrai démarrage automatique au branchement, la borne elle-même doit être configurée en « charge libre » ; ce réglage pilote la décision d'autorisation côté serveur, indépendante du fabricant.
- **Démarrage par véhicule (et non plus par idTag)** : partout où l'on lance une charge, on choisit désormais un **véhicule** plutôt qu'un idTag brut. `POST /connectors/{id}/start` accepte `vehicle_id`. Une charge lancée pour un véhicule **sans** idTag est tout de même rattachée au bon véhicule (mémorisation du démarrage distant en attente). Un champ « avancé » permet encore de forcer un idTag précis dans la fenêtre d'un connecteur.
- **Accueil** : les bornes en état *Preparing* (véhicule branché, en attente) sont mises en avant ; en mode sans autorisation, une puce « Démarrage auto » remplace le bouton.
- **Dernière charge** : passe en lecture seule avec un bouton **Modifier** (au lieu de champs éditables en permanence), pour un comportement cohérent avec les autres vues.
- Icônes optionnelles haute résolution : si tu déposes `icon-192.png`, `icon-512.png`, `icon-512-maskable.png` dans `app/static/`, elles seront servies automatiquement (sinon l'icône SVG et `icon.png` suffisent).

## 0.14.0

- **Fix : des sessions restaient "actives" indéfiniment même après le correctif de la 0.13.0.** Ce correctif ne se déclenchait que sur un *nouveau* `StatusNotification` ; les sessions déjà figées en base (d'avant le correctif, ou après une coupure survenue pendant que le serveur était à l'arrêt) n'étaient jamais rattrapées puisqu'aucune notification ne les déclenchait plus. Deux ajouts :
  - **Rattrapage au démarrage du serveur** : toute transaction "active" dont le connecteur est déjà "Available" en base est clôturée immédiatement.
  - **Redemande explicite du statut à chaque (re)connexion d'une borne** : plutôt que d'attendre qu'elle daigne renvoyer spontanément son statut, le serveur lui envoie un `TriggerMessage(StatusNotification)` pour chaque connecteur connu dès la reconnexion. C'est le seul moyen fiable, au sens du protocole OCPP, de connaître l'état réel après une coupure (la perte de connexion à elle seule ne signifie pas qu'une charge est terminée : la borne peut se reconnecter alors que la voiture charge toujours). Si la borne ne supporte pas `TriggerMessage`, ignoré silencieusement.
  - Reproduit de bout en bout : charge démarrée, coupure brutale sans `StopTransaction`, connecteur figé sur "Charging" avant reconnexion, puis correctement clôturée dès la reconnexion suivante.

## 0.13.0

- **Fix majeur : une session pouvait rester "active" indéfiniment** si la borne redevenait "Available" sans jamais envoyer de `StopTransaction` (coupure réseau, redémarrage du simulateur, etc.). Désormais, quand une borne annonce elle-même qu'un connecteur est "Available" alors qu'une transaction y était encore active, le serveur la clôture lui-même (avec calcul de coût) plutôt que de la laisser trainer pour toujours dans "Charge en cours". Reproduit et vérifié (mode local et relais).
- **Fix majeur : le rafraîchissement automatique effaçait les saisies en cours** (le champ "Mode de la borne" ou le formulaire de la dernière charge disparaissaient avant d'avoir pu être validés). Le rafraîchissement périodique (Accueil, détail d'une borne, fenêtre d'un connecteur) ignore maintenant les zones où le focus est actuellement dans un champ.
- **Fix : assigner un véhicule pendant une charge en cours pouvait silencieusement effacer un km/% déjà renseigné** sur cette même session par ailleurs. `PUT /api/sessions/{id}` ne modifie plus que les champs effectivement envoyés.
- **Accueil** : "Charge en cours" affiche maintenant le coût en direct et permet d'assigner un véhicule sans attendre la fin de la charge.
- **Bornes** : dans la fenêtre d'un connecteur, le champ "ID transaction" est devenu une liste déroulante des sessions réellement actives sur ce connecteur, plutôt qu'une saisie libre.

## 0.12.0

- **Nouvel onglet Accueil**, placé en premier : vue synthétique avec la ou les charges en cours (avec animation), les bornes disponibles avec démarrage rapide (sélection d'un véhicule ou idTag manuel), et la dernière charge terminée avec ses champs éditables directement sur place. Les graphiques (nombre de charges / km / coût par période, par véhicule) ne sont volontairement pas encore inclus, ce sera un prochain chantier.
- **Historique enrichi** : nouvelles colonnes Durée (calculée), km (kilométrage renseignable), % min et % max (niveaux de batterie renseignables, "% max" pré-rempli à 100 par défaut dans le formulaire). Toutes ces colonnes, ainsi que le véhicule associé, sont modifiables directement dans le tableau, y compris a posteriori.
- **Réattribution rétroactive d'un véhicule à une charge** : nouvel endpoint `PUT /api/sessions/{id}`, utilisable même longtemps après la fin de la charge (utile si le badge n'a pas été présenté, ou pour une charge démarrée depuis Home Assistant).
- **Dates reformatées** en `JJ/MM/AAAA HH:MM` partout dans la page admin (au lieu du format ISO brut).
- **Fix : la fenêtre modale d'un connecteur ne se rafraîchissait jamais automatiquement** une fois ouverte (contrairement au reste de la page). Corrigé par sécurité, en plus de l'investigation sur un signalement de statut "Charging" figé (pipeline de statut vérifié sain de bout en bout de notre côté : le souci vient soit de MicroOcppSimulator qui n'envoie pas toujours un nouveau `StatusNotification`, soit de cette fenêtre modale restée ouverte).

## 0.11.0

- **Fix majeur : bornes et historique disparaissaient (et suppression d'un véhicule/tarif échouait) après la mise à jour 0.10.0.** `Base.metadata.create_all()` de SQLAlchemy ne modifie jamais les tables déjà existantes : les colonnes ajoutées en 0.10.0 (`tariff_plan_id`, `vehicle_id`, ...) n'étaient donc jamais créées sur une base existante, provoquant des erreurs "no such column" sur toute requête touchant ces tables (y compris les suppressions, qui doivent d'abord détacher les références). Une migration légère ajoute maintenant automatiquement les colonnes manquantes au démarrage. Reproduit sur une base à l'ancien schéma et vérifié : les données existantes sont conservées, plus aucune erreur.
- **Coût figé à la clôture d'une charge.** Le coût n'est plus recalculé à chaque consultation à partir des tarifs actuels : il est calculé une fois à la fin de la charge (montant, kWh, nom de l'abonnement utilisé) et stocké définitivement. Modifier ou supprimer un abonnement/une période plus tard n'affecte donc plus jamais le coût d'une charge déjà terminée.
- **Abonnements (anciennement "Tarifs")** : refonte de l'onglet.
  - Renommage "plan" → "abonnement", plus cohérent avec un contrat électrique.
  - Le prix par défaut apparaît comme une ligne du tableau des périodes ("7j/7, 24h/24"), plus seulement une mention à part.
  - Modification possible du nom, du prix par défaut et de la puissance souscrite d'un abonnement, et de chaque période (nom, prix, jours, horaires), sans avoir à les supprimer/recréer.
  - Nouveau bouton "définir comme actif" dédié, distinct de la création.
  - Nouveau champ puissance souscrite (kVA), informatif.
  - Histogramme hebdomadaire (SVG) par abonnement, montrant les plages colorées sur 7 jours.
- **Véhicules** : modification possible après création (nom, idTag, capacité batterie). Colonne "Batterie (kWh)".
- **Démarrage d'une charge depuis la page admin** : sélection d'un véhicule (plutôt que de taper un idTag à la main) quand plusieurs véhicules sont enregistrés.
- Nouveau logo, plus visible que la version précédente (probablement trop petit/transparent).

## 0.10.0

- **Véhicules** : nouvel onglet, association d'un idTag (badge) à un véhicule. Les sessions démarrées avec ce badge lui sont automatiquement rattachées (mode local et relais), pour le suivi des coûts et l'historique.
- **Tarifs** : nouvel onglet, plans tarifaires avec prix fixe de secours et plages horaires nommées (heures pleines/creuses, week-end, ou tout autre découpage). Assignables par borne. Le coût d'une session est calculé en découpant son énergie par tranche de temps entre relevés successifs et en appliquant le tarif actif à chaque tranche.
- **Historique** : nouvel onglet listant toutes les sessions, tous chargeurs confondus, avec véhicule, coût et tarif appliqué, filtrable par véhicule.
- Nouveau module `pricing.py` (calcul de coût).
- Nouveaux endpoints : `GET/POST/PUT/DELETE /api/vehicles`, `GET/POST/PUT/DELETE /api/tariffs`, `POST/DELETE /api/tariffs/{id}/periods/...`, `PUT /api/chargers/{id}/tariff`, `GET /api/history`.
- Page admin réorganisée en onglets (Bornes, Véhicules, Tarifs, Historique).

## 0.9.0

- **Fix : l'ingress affichait "404: Not Found".** La racine `/` faisait une redirection HTTP vers le chemin absolu `/admin`. Servie directement maintenant, sans redirection.
- **Fix : tous les appels de l'API depuis la page admin utilisaient des chemins absolus (`/api/...`)**. Passés en chemins relatifs (`api/...`).
- **Fix : entité fantôme "Autoriser la charge" au niveau de la borne**, supprimée automatiquement à la prochaine (re)connexion.
- **Fix : le panneau d'un connecteur ouvert dans la page admin ne se rafraîchissait pas automatiquement.**

## 0.8.0

- **Fix majeur : statut et pilotage étaient au niveau de la borne, pas du connecteur.** Chaque connecteur a maintenant ses propres entités MQTT (statut, puissance, énergie, durée, switch).
- Page admin réécrite : "détails" s'ouvre sur chaque connecteur individuellement (fenêtre modale dédiée).
- Nouveaux filtres `connector_id` sur `GET /chargers/{id}/sessions` et `GET /chargers/{id}/metervalues`.

## 0.7.0

- **Fix : aucune entité n'apparaissait côté Home Assistant malgré une connexion MQTT réussie.** Republiée désormais à chaque (re)connexion au broker.

## 0.6.0

- **Fix (auth MQTT) : "Not authorised" à la connexion.** Utilisation du mécanisme officiel de service Supervisor (`services: ["mqtt:want"]`).
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
- Pas encore de graphes (nombre de charges / km / coût par période et par véhicule).
- Rattachement de transaction incomplet sur StopTransaction en mode relais.
- Une session sans véhicule ni tarif assigné à sa borne utilise l'abonnement marqué "actif" s'il existe, sinon aucun coût n'est calculé.
