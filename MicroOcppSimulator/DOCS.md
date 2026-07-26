# MicroOcpp Simulator

Cet add-on empaquette [MicroOcppSimulator](https://github.com/matth-x/MicroOcppSimulator), l'outil de test/démo officiel du projet MicroOCPP. Il simule une borne de recharge (côté Charge Point) et se connecte en OCPP 1.6 à n'importe quel serveur central, Home Assistant ou non.

## Installation

L'add-on est compilé depuis les sources au moment du build (C++ via cmake, plus une reconstruction du dashboard web via npm), pas d'image pré-construite. La première installation peut prendre plusieurs minutes selon le matériel.

## Utilisation

1. Démarre l'add-on, puis ouvre son interface (panneau latéral ou port direct).
2. Dans la section "OCPP 1.6 Connection", renseigne l'URL de ton serveur OCPP (ex. `ws://homeassistant.local:9000/CP_SIM` pour l'intégration `ocpp` de Home Assistant) et un identifiant de station, puis valide.
3. Le simulateur devrait apparaître comme un nouvel appareil côté serveur OCPP, avec ses entités (statut, mesures, switches).
4. Utilise l'interface pour déclencher des sessions de charge simulées, des changements de statut, etc.

## Connecteurs simulés

Le nombre de connecteurs (bornes) simulés n'est pas configurable depuis l'interface : c'est une constante fixée à la compilation dans le code source du projet (`MO_NUMCONNECTORS`). Le code source ne gère que deux cas précis :

- `MO_NUMCONNECTORS=3` (valeur par défaut de cet add-on) → 2 connecteurs simulés (id 1 et 2)
- toute autre valeur → 1 seul connecteur simulé (id 1)

Ce n'est pas un compteur libre, une valeur arbitraire cassera la compilation. Pour changer ce choix, modifie la valeur par défaut de l'argument `MO_NUMCONNECTORS` directement dans le `Dockerfile` (`ARG MO_NUMCONNECTORS=3`), puis reconstruis l'add-on. Ce n'est plus réglable via `build.json`, qui n'est plus lu par Home Assistant depuis Supervisor 2026.04.0.

## Notes techniques sur les correctifs appliqués

Trois problèmes sont corrigés dans le Dockerfile de cet add-on par rapport au projet upstream :

- **"Unable to fetch connectors"** : le dashboard livré par le projet est pré-compilé avec l'URL d'API figée en dur sur `http://localhost:8000/api`, ce qui casse tout accès via une IP ou un nom d'hôte différent de `localhost` ([issue upstream](https://github.com/matth-x/MicroOcppSimulator/issues/5)). Le dashboard est donc reconstruit au moment du build avec une URL d'API relative.
- **Téléchargement d'un fichier `.gz` via l'ingress** : le serveur intégré sert par défaut la page pré-compressée en gzip. Ce header ne survit pas correctement au passage par le proxy d'ingress de Home Assistant. La page est donc servie non compressée à la place (l'impact sur la taille est négligeable, ~170 Ko).
- **Échec de compilation avec les CMake récents** : une dépendance interne (`mbedtls`) déclare un `cmake_minimum_required` trop ancien, rejeté par les versions récentes de CMake. Corrigé via `-DCMAKE_POLICY_VERSION_MINIMUM=3.5`.

## Licence

Ce projet embarque MicroOcppSimulator, distribué sous licence GPL-3.0 (dépendance à la bibliothèque Mongoose). Le code source complet reste disponible sur le [dépôt officiel](https://github.com/matth-x/MicroOcppSimulator).

## Support

Cet add-on n'est pas un projet officiel de Schneider Electric, Home Assistant, ni de l'auteur de MicroOCPP. Pour les problèmes liés au simulateur lui-même (hors correctifs ci-dessus), se référer au [dépôt officiel](https://github.com/matth-x/MicroOcppSimulator/issues).
