# MicroOcpp Simulator

Cet add-on empaquette [MicroOcppSimulator](https://github.com/matth-x/MicroOcppSimulator), l'outil de test/démo officiel du projet MicroOCPP. Il simule une borne de recharge (côté Charge Point) et se connecte à un serveur OCPP central, comme l'intégration `ocpp` (lbbrhzn) de Home Assistant, sans qu'une borne physique soit nécessaire.

## Installation

L'add-on est compilé depuis les sources C++ du projet au moment du build (pas d'image pré-construite). Sur du matériel modeste (Raspberry Pi par exemple), la première installation peut prendre plusieurs minutes le temps de la compilation.

## Utilisation

1. Démarre l'add-on (bouton "Start"), puis ouvre son interface via le panneau latéral de Home Assistant (ingress) ou via "Ouvrir l'interface web".
2. Dans l'interface du simulateur, section "Control Center", renseigne l'URL de ton serveur OCPP Home Assistant (ex. `ws://homeassistant.local:9000/CP_SIM`, adapte selon la configuration de ton intégration `ocpp`) et un identifiant de station.
3. Valide la connexion. Le simulateur devrait alors apparaître comme un nouvel appareil dans l'intégration OCPP de Home Assistant, avec ses entités (statut, mesures, switches).
4. Utilise l'interface pour déclencher des sessions de charge simulées, des changements de statut, etc.

## À propos de l'ingress

L'accès via le panneau latéral (ingress) fait transiter les requêtes par un sous-chemin dynamique généré par Home Assistant. La grande majorité des fonctions de l'interface web du simulateur fonctionnent normalement de cette façon. Si tu rencontres un écran blanc ou des ressources qui ne se chargent pas, essaie l'accès direct via le port 8000 (à activer dans l'onglet "Réseau" de l'add-on) en remplacement de l'ingress.

## Licence

Ce projet embarque MicroOcppSimulator, distribué sous licence GPL-3.0 (en raison de sa dépendance à la bibliothèque Mongoose). Le code source complet reste disponible sur le [dépôt officiel](https://github.com/matth-x/MicroOcppSimulator).

## Support

Cet add-on n'est pas un projet officiel de Schneider Electric, Home Assistant, ni de l'auteur de MicroOCPP. Pour les problèmes liés au simulateur lui-même, se référer au [dépôt officiel](https://github.com/matth-x/MicroOcppSimulator/issues).
