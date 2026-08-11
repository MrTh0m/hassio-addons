## 0.19.21

- **Nouveau** : entités MQTT enrichies par connecteur, pour construire des cartes Lovelace complètes (état, charge, véhicule, coût) :
  - `courant` (A) et `tension` (V), auparavant stockés en base mais jamais publiés en MQTT.
  - `énergie totale` (renommé depuis `énergie`, aucun changement de valeur ni de `unique_id`) : c'est le registre `Energy.Active.Import.Register` brut de la borne, cumulatif à vie, à utiliser pour le tableau de bord Energy de Home Assistant.
  - **`énergie session`** (nouveau) : énergie de la seule charge en cours (registre moins le relévé de départ de la transaction), repart de 0 à chaque nouvelle charge. ⚠️ à ne pas ajouter en plus de `énergie totale` au tableau de bord Energy, ce serait compter deux fois la même énergie.
  - **`coût session`** (nouveau) : coût calculé en direct pendant la charge (réutilise la même logique de découpage tarifaire que le coût final).
  - **`début de session`** (nouveau, horodatage) et **groupe « dernière charge »** (énergie, coût), publiés à l'arrêt d'une charge (aussi bien via `StopTransaction` que via la fermeture automatique d'une transaction restée active).
  - Les compteurs de session (énergie, coût, durée) sont remis à zéro explicitement au démarrage de chaque nouvelle charge, pour ne pas laisser traîner les valeurs de la session précédente (les topics MQTT sont retenus).
- **Nouveau** : chaque **véhicule** est désormais publié comme un appareil MQTT à part entière (pas un simple texte accroché à la borne), pour une carte Lovelace dédiée à la voiture, indépendante de la borne utilisée pour la recharger :
  - `En charge` (oui/non), `En charge sur` (borne + connecteur), `début de session`, `énergie session`, `coût session`, `durée de charge` : reflètent en direct la charge en cours de ce véhicule, quelle que soit la borne.
  - `Dernière charge énergie/coût/borne`, `Kilométrage`, `Capacité batterie`.
  - Créé/mis à jour à la création ou l'édition d'un véhicule ; retiré côté HA uniquement sur suppression DÉFINITIVE (la désactivation, réversible, laisse les entités en place).
  - Pas de test automatisé dédié pour cette fonctionnalité (nécessiterait de mocker un client MQTT, absent de la suite actuelle) : la logique de calcul réutilisée (`compute_session_cost`) est déjà couverte par ailleurs.
- **Corrigé** : la réduction nocturne de luminosité (introduite en 0.19.20) ne s'appliquait qu'en mode Auto alors qu'elle devait être indépendante du mode (sur la valeur fixe elle-même en mode Fixe). `compute_light_target` gère maintenant les deux cas ; le planificateur réévalue aussi les bornes en mode Fixe dès que la réduction nocturne y est activée (pas seulement le mode Auto), y compris pour restaurer automatiquement la valeur pleine une fois la fenêtre nocturne terminée. Tests dédiés ajoutés (`test_light_intensity.py`, jamais déposé sur E: lors de son introduction en 0.19.20 malgré avoir été validé en local à l'époque, corrigé au passage).
- **Amélioré** : refonte de la section **Luminosité** de l'onglet Réglages (maquette validée au préalable) :
  - Encadrée dans une carte pleine largeur, cohérente avec le reste du formulaire (ne détonne plus dans une colonne étroite à 340px).
  - « Réduire la nuit » sorti du bloc Auto : réglage partagé affiché sous les deux modes, avec la précision « (s'applique en Fixe comme en Auto) ».
  - Panneau nuit redessiné en grille (Réduction sur sa ligne, Début/Fin côte à côte) au lieu d'une ligne qui débordait sur deux lignes.
  - Le curseur du mode Fixe suit le même schéma visuel que ceux du mode Auto (libellé + valeur au-dessus, curseur en dessous).
  - Retrait de « (optionnel) » sur le champ Nom d'affichage.
- **Corrigé** : un véhicule sans historique de charge affichait « Inconnu » sur tous ses capteurs MQTT de session (En charge, En charge sur, énergie/coût/durée de session), faute d'une quelconque valeur jamais publiée sur ces topics. Désormais des valeurs par défaut cohérentes (pas en charge, compteurs à 0) sont publiées à la création d'un véhicule, et republiées à chaque reconnexion MQTT en interrogeant s'il a une session active (au lieu de rester silencieux comme avant) — corrige aussi rétroactivement les véhicules existants dès la prochaine reconnexion.
- **Corrigé/amélioré** : le capteur « Kilométrage » du véhicule prêtait à confusion (odomètre brut à la dernière charge, pas les km parcourus depuis la charge précédente). Renommé en « Odomètre (dernière charge) », et ajout d'un nouveau capteur séparé **« Km depuis la charge précédente »** (delta, même calcul que celui déjà utilisé dans l'historique). Au passage, nouvelle fonction centrale `publish_vehicle_last_charge` (recalcule le résumé « dernière charge » à partir de la base, plutôt que de propager des valeurs au coup par coup) appelée :
  - à la fin d'une session OCPP réelle (StopTransaction, et la fermeture automatique d'une transaction restée active) ;
  - **surtout** lors de la modification a posteriori d'une session (`PUT /api/sessions/{id}`) : l'odomètre est en pratique presque toujours saisi APRÈS la charge, pas au moment même de `StopTransaction`, et rien ne republiait le MQTT à ce moment-là jusqu'ici (point identifié mais laissé de côté lors de l'introduction initiale de cette fonctionnalité) ;
  - à la création d'une charge externe.

## 0.19.20

- **Nouveau** : pilotage automatique de la luminosité (`LightIntensity`), en plus du curseur manuel existant. Choix exclusif par borne entre deux modes :
  - **Fixe** (comportement historique) : le curseur unique manuel, rien d'automatique.
  - **Auto** : deux valeurs distinctes, "en charge" (véhicule branché, quel que soit le connecteur sur une borne à plusieurs) et "libre" (aucun véhicule), poussées automatiquement à chaque changement d'occupation. Réduction nocturne optionnelle en points de pourcentage (pas un facteur) entre deux horaires, appliquée par-dessus la valeur active. Les valeurs des deux modes sont mémorisées indépendamment : basculer entre fixe et auto ne fait rien perdre.
  - Apprentissage automatique et passif de la prise en charge de l'extinction totale (0%) par la borne (observé lors de tout essai réel, manuel ou automatique) : sert de plancher à la réduction nocturne, 1% tant que non confirmé, 0% si confirmé accepté.
  - N'a d'effet que sur une borne possédant réellement la clé `LightIntensity`.
  - ⚠️ Les horaires de réduction nocturne sont évalués en UTC (comme les heures creuses existantes) : à vérifier que le fuseau du conteneur correspond à l'heure locale souhaitée.

## 0.19.19

- **Corrigé** : quand une borne refuse une valeur de configuration OCPP (`Rejected`/`NotSupported`) ou que la requête échoue, le champ concerné (curseur de luminosité y compris) revient automatiquement à la dernière valeur réellement confirmée par la borne, au lieu de rester affiché sur la valeur refusée comme si elle avait été appliquée. S'applique à tous les champs de l'onglet Configuration OCPP, pas seulement à la luminosité.

## 0.19.18

- **Amélioré** : le champ Luminosité (LED) devient un curseur (slider) par pas de 5 %, avec la valeur affichée en direct, au lieu d'un champ numérique à taper. L'enregistrement se déclenche au relâchement du curseur, sans bouton séparé.

## 0.19.17

- **Corrigé** : le champ Luminosité (LED) ajouté en 0.19.16 précise désormais l'échelle, 0 à 100 (pourcentage de l'intensité maximale), plutôt qu'une plage non confirmée. Basé sur la documentation OCPP 1.6 d'un autre constructeur (ABB) qui documente la même clé `LightIntensity` avec cette sémantique de pourcentage.

## 0.19.16

- **Nouveau** : la clé OCPP `LightIntensity` (luminosité du bandeau LED, observée sur les bornes Schneider) est désormais remontée dans l'onglet **Réglages** sous forme d'un simple champ numérique avec bouton Enregistrer, visible même hors mode debug, plutôt que de nécessiter un passage par la configuration OCPP brute. Elle n'apparaît que si la borne possède réellement cette clé (absente sur les simulateurs MicroOcpp). Après revue de l'ensemble des clés d'une borne réelle, c'est la seule identifiée comme pertinente pour ce traitement à ce stade : les autres clés modifiables touchent au réseau, à la sécurité (ex. `SecurityProfile`, risqué à exposer sans garde-fou) ou à des comportements protocolaires trop techniques pour un réglage grand public.

## 0.19.15

- **Corrigé** : incohérence d'affichage quand une borne est hors ligne. Les puces de connecteur ("Conn. 1 · Disponible", etc.) continuaient d'afficher le dernier statut connu en base, même quand la borne elle-même était marquée hors ligne, ce qui pouvait laisser croire à tort qu'on pouvait démarrer une charge. Désormais, dès qu'une borne est hors ligne, tous ses connecteurs s'affichent "Indisponible" (Accueil, liste Bornes, fiche borne), via une fonction centralisée (`effectiveConnectorStatus`) réutilisée partout pour rester cohérent.
- **Amélioré** : refonte de l'onglet **Configuration OCPP** :
  - Le tableau ne déborde plus de la modale (PC comme mobile) : colonnes à largeur fixe et coupure de mot sur les valeurs longues sans espace.
  - Ajout d'un bouton "i" par clé affichant une description (rôle, type attendu, valeurs possibles) pour les clés standard du protocole OCPP 1.6. Les clés propres au constructeur (ex. préfixe `Cst_` chez Schneider) affichent un message générique faute de documentation officielle.
  - Les clés booléennes reconnues (ex. `AuthorizeRemoteTxRequests`) sont désormais éditées via une liste déroulante true/false plutôt qu'un champ texte libre, pour éliminer tout risque de faute de frappe.
  - Cet onglet est désormais réservé au **mode debug** (même réglage que Logs et Base de données) : c'est un outil avancé, pas un réglage grand public.

## 0.19.14

- **Nouveau** : gestion du statut `RebootRequired` renvoyé par une borne à un `ChangeConfiguration`. Jusqu'ici, toute réponse OCPP différente d'une erreur HTTP affichait le même message de succès générique, même quand la borne signalait qu'un redémarrage était nécessaire pour appliquer la valeur (ex. `Cst_BackendUrl` sur le simulateur MicroOcpp). Désormais :
  - L'onglet **Configuration OCPP** affiche un bandeau et un badge ⏳ sur les clés dont la borne a accepté la nouvelle valeur mais attend un redémarrage pour l'appliquer (suivi côté serveur, remis à zéro automatiquement au `BootNotification` suivant).
  - Nouveau bouton **Redémarrer la borne** (Reset OCPP, redémarrage logiciel), avec confirmation renforcée si une charge est en cours sur un connecteur.
  - Le toast de confirmation distingue maintenant `Accepted` (succès), `RebootRequired` (avertissement), `Rejected`/`NotSupported` (échec), au lieu d'un message identique dans tous les cas.
  - Améliorations diverses de la modale : compteur de clés, bouton de rafraîchissement, bouton d'enregistrement désactivé pendant l'appel.

## 0.19.13

- **Fix critique** : la charge externe (ajouter une charge sur borne tierce) échouait systématiquement avec une erreur 500 (`NOT NULL constraint failed: transactions.charger_id`), depuis l'introduction de cette fonctionnalité. La table `transactions` avait été créée avant que `charger_id` devienne nullable côté modèle ; l'ancienne contrainte NOT NULL restait gravée dans le schéma SQLite existant et bloquait toute insertion sans borne associée. Migration automatique au démarrage (recréation de la table selon le schéma à jour, données et MeterValues préservés).
- L'énergie (kWh) d'une charge externe est désormais optionnelle à la saisie : utile pour tracer une charge dont la valeur réelle est inconnue ou perdue, à compléter plus tard via Modifier. Une valeur non renseignée s'affiche distinctement d'une charge ayant réellement délivré zéro kWh.

## 0.19.12

- **Fix critique** : une session pouvait hériter des MeterValues (donc de l'énergie et du coût) d'une toute autre charge, sur une autre borne et à une autre date. Cause : "Supprimer tout l'historique" effaçait les transactions sans toucher à leurs MeterValues, qui restaient orphelins ; SQLite réutilisant les id auto-increment libérés, une nouvelle session pouvait retomber sur un id d'une ancienne transaction supprimée et hériter de ses relevés.
  - La suppression totale de l'historique efface désormais aussi les MeterValues.
  - Le calcul d'énergie filtre les MeterValues par borne et connecteur, pas seulement par id de session.
  - Garde supplémentaire : tout relevé dont l'horodatage tombe hors de la fenêtre de la session (marge de 10 min) est ignoré.
  - Les sessions déjà affectées se corrigent avec le bouton **Recalculer depuis compteur** (Historique → Modifier).

## 0.19.10

- **Fix critique** : le décorateur `@on(Action.meter_values)` avait disparu sur le handler `MeterValues` en mode local (régression post-0.19.5). Les trames MeterValues envoyées par une borne locale (dont la Schneider) étaient rejetées sans routage, donc jamais enregistrées : le kWh et le coût d'une charge en cours restaient figés à vide pendant toute la session. Restauré.
- Fix : en mode relais, l'énergie annoncée en kWh par la borne n'était pas normalisée en Wh avant publication MQTT (incohérence de facteur 1000 sur le capteur énergie), contrairement au mode local. Harmonisé.
- Fix : la modale de détail d'un connecteur affichait l'UUID technique de la borne au lieu de son nom d'affichage.
- Fix : id HTML dupliqué (`ocpp-tab-body`) entre le placeholder et le contenu chargé de l'onglet Configuration OCPP.
- Fix : un token de session expiré laissait certaines pages vides sans jamais rediriger vers l'écran de connexion (la plupart des appels ne passaient pas par la fonction qui gérait déjà le 401). Un intercepteur global détecte maintenant n'importe quel 401 et redéclenche la connexion.
- Fix : le compte `admin` était modifiable comme un utilisateur normal (permissions, associations de véhicules/bornes, mot de passe) alors qu'il voit tout par définition et que son mot de passe est géré par la configuration de l'add-on. Bloqué côté serveur (pas seulement masqué côté interface).
- Sécurité : les routes du journal OCPP (`/api/logs*`) n'étaient protégées que côté interface, la permission « Voir les logs OCPP » n'était jamais vérifiée côté serveur. Désormais réservées admin, comme la Base de données.
- Nettoyage : suppression d'un dictionnaire mort (`_TABLE_BY_NAME`) dans l'export/import, jamais référencé (puis réintroduit à bon escient pour alimenter le nouveau navigateur de tables ci-dessous).
- Amélioration : versions des dépendances figées dans `requirements.txt` (étaient jusque-là sans version, donc sujettes à changer silencieusement à chaque rebuild d'image).

### Nouveau : diagnostic « Base de données » et évolution du Mode débug

- Nouvel onglet en lecture seule pour parcourir le contenu brut des tables (bornes, sessions, MeterValues, tarifs, véhicules, conditions de charge, clés de configuration, statuts de connecteur), paginé, trié du plus récent au plus ancien. Ne couvre pas la table des utilisateurs.
- Le réglage « Mode débug » (Réglages → Avancé, réservé admin, persisté en base) contrôle maintenant à la fois l'onglet Base de données et l'onglet Logs (auparavant lié à une permission par utilisateur, retirée du formulaire de droits).
- Export CSV disponible sur Historique, Logs et Base de données (bouton « Exporter (CSV) » sur chaque vue, respecte les filtres actifs).
- Utilisateur connecté (rôle + nom) affiché dans Réglages → Mon compte.

### Nouveau : désactivation / suppression définitive des véhicules

- Un véhicule « supprimé » (ex bouton Supprimer) est en réalité désactivé depuis toujours (suppression logique, `deleted_at`), mais restait jusqu'ici totalement invisible dans l'UI classique, y compris quand il n'était lié à aucune transaction (aucun intérêt à le garder caché dans ce cas). Il apparaît maintenant grisé dans la liste des véhicules plutôt que d'être masqué, avec la mention « (désactivé) ». Son historique reste consultable, mais il ne peut plus recevoir de nouvelle charge (démarrage à distance ou charge externe) tant qu'il n'est pas réactivé.
- Nouveau bouton **Réactiver** sur un véhicule désactivé (annule la désactivation).
- Nouveau bouton **Supprimer définitivement**, réservé aux véhicules déjà désactivés : efface réellement le véhicule ET tout son historique de charge (sessions + MeterValues associées), avec double confirmation explicite avant exécution. Irréversible.
- Fix : deux véhicules pouvaient porter exactement le même nom (actif ou non), sans aucun avertissement, source de confusion pour associer une charge au bon véhicule. Le nom est maintenant vérifié (insensible à la casse, sur les véhicules actifs et désactivés) à la création et à la modification.

### Nouveau : désactivation des bornes, réactivation automatique

- Une borne « supprimée » (désactivation logique existante, `deleted_at`) était jusqu'ici totalement invisible dans l'UI. Elle apparaît maintenant grisée dans la liste des bornes, avec la mention « (désactivée) ». Contrairement aux véhicules, pas de suppression définitive pour les bornes : l'identifiant (chargePointId) est imposé par la borne elle-même, pas de risque de doublon de nom à gérer.
- **Réactivation automatique** : si une borne désactivée se reconnecte physiquement (BootNotification en mode local, premier signal reçu en mode relais), elle redevient active toute seule, avec le même id. Pas de bouton « Réactiver » manuel : la connexion physique fait foi. Corrige une incohérence de la version précédente, où la borne était bien retrouvée à la reconnexion (même ligne en base, pas de doublon) mais restait invisible malgré tout, contrairement à ce que documentait le code.
- Tant qu'elle est désactivée, seule la modification du nom d'affichage reste possible (nouvelle modale dédiée). Changer son mode, son authentification, son tarif, pousser une configuration OCPP, ou démarrer/arrêter une charge sont bloqués côté serveur.

## 0.19.9

- Fix : bouton Recalculer (showToast non défini).

## 0.19.8

- Fix : bouton Modifier dans l'historique (variable myRole non définie causait un crash silencieux de la modale).

## 0.19.7

- Fix : bouton Modifier dans l'historique (comparaison id en entier).
- Fix : UUID dans le filtre de bornes des logs remplacé par le nom d'affichage.

## 0.19.6

- Fix : bouton Modifier dans l'historique cassé quand la session n'était pas encore en cache (appel depuis l'Accueil).
- Fix : UUID des bornes remplacé par le nom d'affichage dans les logs OCPP et dans la gestion des utilisateurs (checkboxes d'association).

## 0.19.5

- Fix : MeterValues correctement associés à la session active (résolution par connector_id plutôt que transaction_id OCPP, qui diffère de notre id interne). Les données temps réel (puissance, énergie) s'affichent maintenant pendant la charge.
- Fix : bouton Arrêter masqué en statut Finishing (charge déjà terminée côté borne).
- Config OCPP : filtre texte, scroll interne, bouton OK compact toujours visible.

## 0.19.4

### Nouveau : onglet Configuration OCPP dans la modale de borne

- La modale de configuration de chaque borne (bouton engrenage) dispose désormais d'un onglet **Configuration OCPP** qui lit directement les clés de la borne via GetConfiguration.
- Toutes les clés sont listées, triées par pertinence. Les clés importantes pour le suivi temps réel (MeterValueSampleInterval, MeterValuesSampledData...) apparaissent en premier, mises en valeur.
- Les clés accessibles en écriture sont modifiables depuis l'UI ; les clés en lecture seule sont affichées mais grisées.
- Pour activer les données temps réel sur la Schneider : régler MeterValueSampleInterval=60 et MeterValuesSampledData=Energy.Active.Import.Register,Power.Active.Import.

## 0.19.3

### Correction : énergie et coût fantômes sur session active

- **Session active** : le calcul d'énergie ignore désormais meter_stop (qui peut être parasite après un redémarrage ou un reconcile) et ne se base que sur les MeterValues reçus pendant la session.
- Résultat : une session active sans MeterValues affiche correctement 0.00 kWh au lieu de recycler les données de la session précédente.
- **Note Schneider** : la borne Schneider EVH5A n'envoie pas de MeterValues par défaut. Configurer MeterValueSampleInterval=60 et MeterValuesSampledData=Energy.Active.Import.Register,Power.Active.Import via l'onglet Config de la borne pour avoir les données en temps réel.

## 0.19.2

### Correction : calcul d'énergie avec les bornes à index absolu élevé

- **Normalisation des unités MeterValues** : certaines bornes (dont Schneider) envoient l'énergie en kWh au lieu de Wh dans les SampledValues. La valeur est désormais convertie en Wh avant stockage et avant calcul, quelle que soit l'unité déclarée.
- **Calcul correct sur index absolu** : `meterStart` est l'index cumulé depuis la mise en service de la borne (ex. 96 543 Wh). L'énergie de session est bien calculée comme `index_fin - index_début`, pas comme l'index brut.
- **Occupation de l'alimentation** : la puissance affichée est désormais cohérente avec l'énergie délivrée réelle.

## 0.19.1

### Amélioration

- **Nom d'affichage des bornes** : champ optionnel dans la configuration de chaque borne. Si renseigné, il remplace l'UUID technique (ex. « 4d1e481d-cbf1-... ») dans toute l'interface (Accueil, Bornes, charge en cours, modales). L'UUID reste visible en dessous pour référence. Migration automatique en base au démarrage.

## 0.19.0

Gestion multi-utilisateurs, push temps-réel (SSE), et affichage des charges différées.

### Nouveau : Gestion des utilisateurs

- **Comptes utilisateurs** : l'admin peut créer des users avec login/mot de passe depuis Réglages → Utilisateurs.
- **Associations** : chaque user est lié à 0..N voitures et 0..N bornes ; une voiture ou une borne peut être associée à plusieurs users.
- **Droits granulaires** configurables par l'admin : réglages borne, abonnements électriques, gestion des voitures, consultation des logs, export/import.
- **Filtrage automatique** : un user ne voit que les voitures et bornes qui lui sont associées (Accueil, Bornes, Véhicules, Historique).
- **Démarrer/arrêter une charge** : autorisé à un user uniquement si la borne ET la voiture en cours sont toutes deux associées à son compte.
- **Créer une voiture** : la voiture est automatiquement associée au créateur.
- **Modifier l'historique** : un user peut modifier les sessions de ses voitures associées.
- **Changer son mot de passe** : accessible depuis Réglages → Mon compte, pour tout user.
- **Réinitialiser un mot de passe** : l'admin peut réinitialiser le mot de passe de n'importe quel user depuis sa fiche.
- **Suppression douce** : un user supprimé n'apparaît plus dans la liste ; ses données restent en base.

### Nouveau : Push temps-réel (SSE)

- **Server-Sent Events** : l'interface se met à jour automatiquement à chaque changement d'état OCPP (connexion/déconnexion borne, changement de statut connecteur, début/fin de transaction) sans attendre le prochain cycle de polling.
- **Aucun impact sur les formulaires** : le rafraîchissement est ignoré si un champ ou une modale est en cours d'édition (protection isFocusWithin déjà en place).
- **Reconnexion automatique** en cas d'erreur réseau (délai 10 s).
- Le polling de secours passe de 5 s à 8 s.

### Correction : Affichage des charges différées

- **Carte « En attente »** distincte dans « Charge en cours » quand une charge est intentionnellement suspendue par une programmation (départ différé, heures creuses) : pas de timer trompeur, message explicite (ex. « Démarrage après 19:00 · Câble verrouillé, charge non démarrée »), liseré orangé.
- **** exposé dans l'API REST et rempli au  quand une condition de programmation bloque la charge immédiate.

### Zone de danger (Réglages, admin uniquement)

- Nouveau bouton **Supprimer tout l'historique** : efface définitivement toutes les sessions (OCPP et externes). Double confirmation requise. Les bornes, véhicules et abonnements sont conservés.

### Autres

-  : retourne le profil complet du user connecté (rôle, permissions, associations).
-  : changement de mot de passe en self-service.
-  : suppression totale de l'historique (admin).
- Les onglets Logs et Abo. électrique sont masqués pour les users sans droit correspondant.
- Le formulaire d'ajout de voiture est masqué pour les users sans .
- Les boutons Configuration et Supprimer sur les cartes de borne sont masqués selon .

## 0.18.0

Raffinages de l'interface et regroupement des réglages dans le bandeau.

### Corrections

- **Accueil, « Bornes disponibles »** : le statut « Disponible » apparaissait en double sur un connecteur libre. Chaque connecteur tient désormais sur une seule ligne : identité et statut à gauche, action (bouton « Démarrer » ou puce « Démarrage auto ») à droite.
- **Historique** : le tableau, très large, écrasait ses dernières colonnes (dont « kWh/100 »). Il défile maintenant horizontalement en gardant chaque colonne lisible. (Rappel : « km parcourus » et « kWh/100km » ne se calculent qu'à partir de deux charges du même véhicule dont l'odomètre est renseigné.)

### Améliorations

- **Bornes** : chaque carte affiche désormais le statut de chacun de ses connecteurs, et un badge « Programmée » quand une ou plusieurs conditions de charge sont actives sur la borne.
- **Réglages** : nouveau bouton engrenage dans le bandeau du haut ouvrant une fenêtre « Réglages ». La sauvegarde/restauration des données (export/import JSON), auparavant en bas de l'Historique, y est déplacée. La fenêtre annonce la future gestion des profils utilisateurs (affectation de véhicules et permissions par action).
- **Bandeau** : le bouton « Déconnexion » est aligné tout à droite.

## 0.17.0

Respect effectif de la programmation de charge, édition des charges revue, et sauvegarde/restauration des données.

### Corrections

- **Départ différé désormais respecté** même quand la charge est lancée depuis la borne : dès le démarrage de la transaction (et dès le branchement), le serveur impose une limite 0 W (SuspendedEVSE) si une condition de programmation l'exige, sans attendre le prochain cycle du planificateur. Le câble reste branché et verrouillé ; la charge démarre à l'heure prévue.
- **Édition des anciennes charges** : la modification (affecter un véhicule, corriger le kilométrage, les niveaux de batterie) se fait maintenant dans une fenêtre dédiée, lisible et utilisable sur mobile, au lieu de l'édition en ligne d'un tableau à 17 colonnes.
- **« Dernière charge »** : le formulaire de modification s'affiche désormais immédiatement au clic (il fallait auparavant changer de focus pour le voir apparaître).
- **« km parcourus »** : le calcul de la distance depuis la charge précédente fonctionne même lorsque cette charge précédente est en dehors de la fenêtre d'historique affichée (recherche en base de l'odomètre antérieur).

### Améliorations

- **Détection du support SmartCharging** dès la connexion de la borne (lecture de `SupportedFeatureProfiles`). Si la borne ne le supporte pas, la programmation de charge est désactivée et clairement signalée (elle repose sur la limitation de puissance).
- **Configuration d'une borne** : un seul bouton « Enregistrer les modifications » applique désormais le mode, l'autorisation et le tarif en une fois.
- **Programmation** : le connecteur se choisit dans une liste déroulante (« Tous » ou un connecteur détecté) au lieu d'une saisie numérique.
- **Sauvegarde et restauration** : export de l'intégralité des données et de la configuration au format JSON, et réimportation au choix en mode « Fusionner » (complète sans supprimer) ou « Remplacer » (restauration fidèle). Le mot de passe administrateur n'est pas inclus dans l'export.
- **Vue « Logs »** : nouveau journal de diagnostic en direct des messages OCPP échangés avec les bornes, tous modes confondus (local et relais, les deux sens borne↔serveur). Filtrable par borne, type de message et sens ; chaque ligne dévoile la trame brute au clic. Les MeterValues, très fréquents, sont masqués par défaut et activables par un interrupteur. Journal en mémoire (non conservé au redémarrage), sans impact sur la base de données.
- **Mode relais** : propagation des en-têtes d'authentification du handshake (par ex. HTTP Basic / clé OCPP `AuthorizationKey`) vers le serveur officiel, nécessaire pour les CSMS qui l'exigent (dont EcoStruxure). Journalisation des `DataTransfer` et des mesurandes propriétaires observés, pour diagnostiquer ce que la borne expose réellement.
- **Identifiant de borne (chargePointId)** affiché explicitement sur les cartes de l'onglet Bornes et dans la fiche, avec en mode relais l'URL complète relayée vers le serveur officiel.

## 0.16.1

Affinages de l'interface d'administration (aucun changement backend, l'API REST est inchangée).

### Améliorations

- **Accueil** : un badge de programmation apparaît désormais sur chaque connecteur concerné (dans « Charge en cours » comme dans « Bornes disponibles ») quand une condition est active : « Heures creuses », « Départ différé après HH:MM » ou « Prête pour HH:MM ».
- **Charge en cours** : le sélecteur de véhicule est remonté dans l'en-tête de la carte et enregistre automatiquement au changement (plus de bouton « Assigner »).
- **Fiche borne** (icône « i ») repensée : identité, statut/mode/connexion, statistiques de la borne, liste des connecteurs avec accès au détail, puis un historique propre à la borne (sans les notions de véhicule que sont les km et les kWh/100 km). Un bouton « Configurer » y renvoie vers la configuration.
- **Configuration d'une borne** déplacée dans une modale dédiée (icône engrenage) : plus de déroulé sous la carte. Chaque borne expose trois actions : fiche/statistiques, configuration, suppression.
- **Charge externe** : le bouton d'ajout est aussi accessible depuis l'Accueil (en plus de l'Historique).

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
