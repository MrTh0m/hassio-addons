# Changelog

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
