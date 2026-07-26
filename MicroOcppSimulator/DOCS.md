# MicroOcpp Simulator

Cet add-on empaquette [MicroOcppSimulator](https://github.com/matth-x/MicroOcppSimulator), l'outil de test/démo officiel du projet MicroOCPP. Il simule une borne de recharge (côté Charge Point) et se connecte à un serveur OCPP central, comme l'intégration `ocpp` (lbbrhzn) de Home Assistant, sans qu'une borne physique soit nécessaire.

## Installation

L'add-on est compilé depuis les sources C++ du projet au moment du build (pas d'image pré-construite). Sur du matériel modeste (Raspberry Pi par exemple), la première installation peut prendre plusieurs minutes le temps de la compilation.

## Utilisation

1. Démarre l'add-on (bouton "Start"), puis ouvre son interface (voir la section ingress ci-dessous pour l'état actuel).
2. Dans l'interface du simulateur, section "Control Center", renseigne l'URL de ton serveur OCPP Home Assistant (ex. `ws://homeassistant.local:9000/CP_SIM`, adapte selon la configuration de ton intégration `ocpp`) et un identifiant de station.
3. Valide la connexion. Le simulateur devrait alors apparaître comme un nouvel appareil dans l'intégration OCPP de Home Assistant, avec ses entités (statut, mesures, switches).
4. Utilise l'interface pour déclencher des sessions de charge simulées, des changements de statut, etc.

## À propos de l'ingress

L'accès via le panneau latéral (ingress) fait transiter les requêtes par un sous-chemin dynamique généré par Home Assistant. Sur cet addon, l'ingress ne fonctionne pas correctement pour l'instant : la page se télécharge sous forme d'un fichier `.gz` au lieu de s'afficher. L'interface de MicroOcppSimulator est servie pré-compressée (bundle `.gz`), et tout laisse penser que le proxy d'ingress ne relaie pas l'en-tête `Accept-Encoding` de la même façon qu'un accès direct, ce qui fait que le navigateur reçoit le fichier compressé brut au lieu d'un contenu décompressé automatiquement.

**Solution actuelle** : active le mappage direct du port 8000 dans l'onglet "Réseau" de l'add-on, et accède à l'interface via `http://<ip-de-ton-serveur>:8000/` plutôt que par le panneau latéral.

## Licence

Ce projet embarque MicroOcppSimulator, distribué sous licence GPL-3.0 (en raison de sa dépendance à la bibliothèque Mongoose). Le code source complet reste disponible sur le [dépôt officiel](https://github.com/matth-x/MicroOcppSimulator).

## Support

Cet add-on n'est pas un projet officiel de Schneider Electric, Home Assistant, ni de l'auteur de MicroOCPP. Pour les problèmes liés au simulateur lui-même, se référer au [dépôt officiel](https://github.com/matth-x/MicroOcppSimulator/issues).
