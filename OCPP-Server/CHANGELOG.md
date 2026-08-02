# Changelog

## 0.11.0

- **Fix majeur : bornes et historique disparaissaient (et suppression d'un véhicule/tarif échouait) après la mise à jour 0.10.0.** `Base.metadata.create_all()` de SQLAlchemy ne modifie jamais les tables déjà existantes : les colonnes ajoutées en 0.10.0 (`tariff_plan_id`, `vehicle_id`, ...) n'étaient donc jamais créées sur une base existante, provoquant des erreurs "no such column" sur toute requête touchant ces tables (y compris les suppressions, qui doivent d'abord détacher les références). Une migration légère ajoute maintenant automatiquement les colonnes manquantes au démarrage. Reproduit sur une base à l'ancien schéma et vérifié : les données existantes sont conservées, plus aucune erreur.
- **Coût figé à la clôture d'une charge.** Le coût n'est plus recalculé à chaque consultation à partir des tarifs actuels : il est calculé une fois à la fin de la charge (montant, kWh, nom de l'abonnement utilisé) et stocké définitivement. Modifier ou supprimer un abonnement/une période plus tard n'affecte donc plus jamais le coût d'une charge déjà terminée.
- **Abonnements (anciennement "Tarifs")** : refonte de l'onglet.
  - Renommage "plan" → "abonnement", plus cohérent avec un contrat électrique.
  - Le prix par défaut apparaît comme une ligne du tableau des périodes ("7j/7, 24h/24"), plus seulement une mention à part.
  - Modification possible du nom, du prix par défaut et de la puissance souscrite d'un abonnement, et de chaque période (nom, prix, jours, horaires), sans avoir à les supprimer/recréer.
  - Nouveau bouton "définir comme actif" dédié, distinct de la création (le rôle "actif = utilisé par défaut pour les bornes sans tarif assigné" reste inchangé, juste plus lisible).
  - Nouveau champ puissance souscrite (kVA), informatif.
  - Histogramme hebdomadaire (SVG) par abonnement, montrant les plages colorées sur 7 jours.
- **Véhicules** : modification possible après création (nom, idTag, capacité batterie). Colonne "Batterie (kWh)".
- **Démarrage d'une charge depuis la page admin** : sélection d'un véhicule (plutôt que de taper un idTag à la main) quand plusieurs véhicules sont enregistrés.
- Nouveau logo, plus visible que la version précédente (probablement trop petit/transparent).

## 0.10.0

- **Véhicules** : nouvel onglet, association d'un idTag (badge) à un véhicule. Les sessions démarrées avec ce badge lui sont automatiquement rattachées (mode local et relais), pour le suivi des coûts et l'historique.
- **Tarifs** : nouvel onglet, plans tarifaires avec prix fixe de secours et plages horaires nommées (heures pleines/creuses, week-end, ou tout autre découpage). Assignables par borne. Le coût d'une session est calculé en découpant son énergie par tranche de temps entre relevés successifs et en appliquant le tarif actif à chaque tranche (gère correctement une session qui chevauche plusieurs plages).
- **Historique** : nouvel onglet listant toutes les sessions, tous chargeurs confondus, avec véhicule, coût et tarif appliqué, filtrable par véhicule.
- Nouveau module `pricing.py` (calcul de coût), testé isolément (session chevauchant creuses/pleines correctement répartie) avant intégration.
- Nouveaux endpoints : `GET/POST/PUT/DELETE /api/vehicles`, `GET/POST/PUT/DELETE /api/tariffs`, `POST/DELETE /api/tariffs/{id}/periods/...`, `PUT /api/chargers/{id}/tariff`, `GET /api/history`.
- Page admin réorganisée en onglets (Bornes, Véhicules, Tarifs, Historique).

## 0.9.0

- **Fix : l'ingress affichait "404: Not Found".** La racine `/` faisait une redirection HTTP vers le chemin absolu `/admin`, ce qui fait sortir le navigateur du sous-chemin dynamique de l'ingress de Home Assistant. La page est maintenant servie directement (sans redirection) à la fois sur `/` et `/admin`.
- **Fix : tous les appels de l'API depuis la page admin utilisaient des chemins absolus (`/api/...`)**, qui échouent aussi sous ingress pour la même raison. Passés en chemins relatifs (`api/...`).
- **Fix : entité fantôme "Autoriser la charge" au niveau de la borne**, résidu d'avant 0.8.0, supprimée automatiquement à la prochaine (re)connexion.
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
- Pas encore de graphes globaux (histogramme hebdomadaire par abonnement disponible ; consommation/coût dans le temps : à venir).
- Rattachement de transaction incomplet sur StopTransaction en mode relais.
- Une session sans véhicule ni tarif assigné à sa borne utilise l'abonnement marqué "actif" s'il existe, sinon aucun coût n'est calculé.
