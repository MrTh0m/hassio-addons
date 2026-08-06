## 0.19.0

Gestion multi-utilisateurs, push temps-réel (SSE), et affichage des charges différées.

### Nouveau : Gestion des utilisateurs

- **Comptes utilisateurs** : l'admin peut créer des users avec login/mot de passe depuis Réglages > Utilisateurs.
- **Associations** : chaque user est lié à 0..N voitures et 0..N bornes ; une voiture ou une borne peut être associée à plusieurs users.
- **Droits granulaires** configurables par l'admin : réglages borne, abonnements électriques, gestion des voitures, consultation des logs, export/import.
- **Filtrage automatique** : un user ne voit que les voitures et bornes qui lui sont associées (Accueil, Bornes, Véhicules, Historique).
- **Démarrer/arrêter une charge** : autorisé à un user uniquement si la borne ET la voiture en cours sont toutes deux associées à son compte.
- **Créer une voiture** : la voiture est automatiquement associée au créateur.
- **Modifier l'historique** : un user peut modifier les sessions de ses voitures associées.
- **Changer son mot de passe** : accessible depuis Réglages > Mon compte, pour tout user.
- **Réinitialiser un mot de passe** : l'admin peut réinitialiser le mot de passe de n'importe quel user depuis sa fiche.
- **Suppression douce** : un user supprimé n'apparaît plus dans la liste ; ses données restent en base.

### Nouveau : Push temps-réel (SSE)

- **Server-Sent Events** : l'interface se met à jour automatiquement à chaque changement d'état OCPP (connexion/déconnexion borne, changement de statut connecteur, début/fin de transaction) sans attendre le prochain cycle de polling.
- **Aucun impact sur les formulaires** : le rafraîchissement est ignoré si un champ ou une modale est en cours d'édition.
- **Reconnexion automatique** en cas d'erreur réseau (délai 10 s).
- Le polling de secours passe de 5 s à 8 s.

### Correction : Affichage des charges différées

- **Carte "En attente"** distincte dans "Charge en cours" quand une charge est intentionnellement suspendue par une programmation (départ différé, heures creuses) : pas de timer trompeur, message explicite (ex. "Démarrage après 19:00 · Câble verrouillé, charge non démarrée"), liseré orangé.
- Champ `deferred_until` exposé dans l'API REST et rempli au StartTransaction quand une condition de programmation bloque la charge immédiate.

### Zone de danger (Réglages, admin uniquement)

- Nouveau bouton **Supprimer tout l'historique** : efface définitivement toutes les sessions (OCPP et externes). Double confirmation requise. Les bornes, véhicules et abonnements sont conservés.

### Autres

- `GET /api/auth/me` : retourne le profil complet du user connecté (rôle, permissions, associations).
- `POST /api/auth/change-password` : changement de mot de passe en self-service.
- `DELETE /api/history/all` : suppression totale de l'historique (admin).
- Les onglets Logs et Abo. électrique sont masqués pour les users sans droit correspondant.
- Le formulaire d'ajout de voiture est masqué pour les users sans `can_manage_vehicles`.
- Les boutons Configuration et Supprimer sur les cartes de borne sont masqués selon `can_manage_chargers`.

## 0.18.0

Raffinages de l'interface et regroupement des réglages dans le bandeau.

### Corrections

- **Accueil, "Bornes disponibles"** : le statut "Disponible" apparaissait en double sur un connecteur libre. Chaque connecteur tient désormais sur une seule ligne : identité et statut à gauche, action (bouton "Démarrer" ou puce "Démarrage auto") à droite.
- **Historique** : le tableau, très large, écrasait ses dernières colonnes (dont "kWh/100"). Il défile maintenant horizontalement en gardant chaque colonne lisible.

### Améliorations

- **Bornes** : chaque carte affiche désormais le statut de chacun de ses connecteurs, et un badge "Programmée" quand une ou plusieurs conditions de charge sont actives sur la borne.
- **Réglages** : nouveau bouton engrenage dans le bandeau du haut ouvrant une fenêtre "Réglages". La sauvegarde/restauration des données (export/import JSON), auparavant en bas de l'Historique, y est déplacée.
- **Bandeau** : le bouton "Déconnexion" est aligné tout à droite.

## 0.17.0

Respect effectif de la programmation de charge, édition des charges revue, et sauvegarde/restauration des données.

### Corrections

- **Départ différé désormais respecté** même quand la charge est lancée depuis la borne.
- **Édition des anciennes charges** : dans une fenêtre dédiée, lisible sur mobile.
- **"Dernière charge"** : le formulaire de modification s'affiche immédiatement au clic.
- **"km parcourus"** : fonctionne même quand la charge précédente est hors fenêtre d'historique.

### Améliorations

- **Détection du support SmartCharging** dès la connexion de la borne.
- **Configuration d'une borne** : un seul bouton "Enregistrer les modifications".
- **Programmation** : le connecteur se choisit dans une liste déroulante.
- **Sauvegarde et restauration** : export JSON, import fusionner/remplacer.
- **Vue "Logs"** : journal OCPP en direct, filtrable, non persisté.
- **Mode relais** : propagation des en-têtes d'authentification.

## 0.16.1

Affinages de l'interface (aucun changement backend).

- Badge de programmation sur chaque connecteur concerné.
- Sélecteur de véhicule dans l'en-tête de "Charge en cours".
- Fiche borne repensée, configuration dans une modale dédiée.
- Bouton "Charge externe" accessible depuis l'Accueil.

## 0.16.0

Programmation de la charge, suivi enrichi, fiches statistiques, correctif mot de passe.

- Fix : changement de mot de passe depuis la config HA désormais effectif.
- Conditions de charge (heures creuses, départ différé, prête pour).
- Nouvelles colonnes historique : puissance max, km parcourus, kWh/100km.
- Taux de recharge et estimation % final batterie.
- Charges externes (borne tierce).
- Fiches statistiques véhicule et borne.
- Occupation de l'alimentation (jauge puissance délivrée / souscrite).
- Suppression douce bornes et véhicules.

## 0.15.1

- Démarrage automatique effectif au branchement (mode "sans autorisation").
- Interface plus dense et lisible.
- Version + lien GitHub visibles sur mobile.

## 0.15.0

- Refonte complète de l'interface (responsive, mobile-first).
- Application installable (PWA).
- Mode d'autorisation par borne (free / authorized).
- Démarrage par véhicule.
- Dernière charge en lecture seule avec bouton Modifier.

## 0.14.0

- Fix sessions restant "actives" après coupure : rattrapage au démarrage + TriggerMessage StatusNotification à la reconnexion.

## 0.13.0

- Fix sessions "actives" indéfiniment quand la borne repasse Available sans StopTransaction.
- Fix rafraîchissement automatique effaçant les saisies en cours.
- Fix assignation véhicule pendant charge effaçant km/%.

## 0.12.0

- Nouvel onglet Accueil (charge en cours, bornes disponibles, dernière charge).
- Historique enrichi (durée, km, % batterie).
- Réattribution rétroactive d'un véhicule.
- Dates reformatées JJ/MM/AAAA HH:MM.

## 0.11.0

- Fix colonnes manquantes sur base existante (migration automatique).
- Coût figé à la clôture d'une charge.
- Refonte onglet Abonnements.
- Modification véhicules après création.

## 0.10.0

- Véhicules : association idTag, suivi des coûts.
- Tarifs : plans avec plages horaires.
- Historique toutes bornes.

## 0.9.0

- Fix ingress 404.
- Fix chemins API absolus -> relatifs.
- Fix entité fantôme.
- Fix modale connecteur non rafraîchie.

## 0.8.0

- Fix statut et pilotage au niveau connecteur (pas borne).

## 0.7.0

- Fix entités MQTT non publiées.

## 0.6.0

- Fix auth MQTT "Not authorised".

## 0.5.0

- mqtt_base_topic configurable, bandeau admin, endpoint /api/version.

## 0.4.0

- Pont MQTT vers Home Assistant.

## 0.3.0

- Fix timestamps OCPP, statut par connecteur.

## 0.2.0

- Page d'administration basique.

## 0.1.0

- Premiere version : CSMS OCPP 1.6, mode relais, API REST JWT, SQLite.
