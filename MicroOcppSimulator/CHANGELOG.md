# Changelog

## 1.2.1

- Fix : les données persistantes (dont evPlugged/evReady/evseReady et la config WebSocket) étaient perdues à chaque redémarrage. Le lien symbolique vers `/data` était créé sur `/mo_store` (racine), alors que le simulateur écrit en réalité dans `./mo_store` (chemin RELATIF figé par `MO_FILENAME_PREFIX` dans le `CMakeLists.txt` du dépôt officiel), résolu depuis son répertoire de travail `/opt/microocppsimulator`. Le lien symbolique cible désormais `/opt/microocppsimulator/mo_store`, le bon emplacement.

## 1.2.0

- Fix : la configuration (dont l'URL du serveur OCPP) était perdue à chaque recréation du conteneur, car le simulateur écrit toujours dans `/mo_store` (chemin absolu figé en dur dans son code source), qui n'était pas relié au stockage persistant `/data` de l'add-on. `/mo_store` pointe maintenant vers `/data/mo_store` via un lien symbolique créé au démarrage.

## 1.1.0

- Fix : le dashboard web est reconstruit au moment du build avec une URL d'API relative (`API_ROOT=api`) au lieu de l'URL `http://localhost:8000/api` figée en dur par le projet upstream. Corrige l'erreur "Unable to fetch connectors" lors d'un accès via une IP/nom d'hôte autre que `localhost`.
- Fix : la page est désormais servie non compressée (`bundle.html` au lieu de `bundle.html.gz`), ce qui corrige le téléchargement du fichier `.gz` au lieu de l'affichage lors d'un accès via l'ingress de Home Assistant.
- Ajout d'un argument de build `MO_NUMCONNECTORS` pour choisir entre 1 ou 2 connecteurs simulés.
- Fix : compatibilité avec les versions récentes de CMake (`-DCMAKE_POLICY_VERSION_MINIMUM=3.5`).
- L'image de base est fixée directement dans le Dockerfile (`build.json` n'est plus lu par Home Assistant depuis Supervisor 2026.04.0).
- Support de 5 architectures (aarch64, amd64, armhf, armv7, i386).

## 1.0.0

- Première version : build de MicroOcppSimulator depuis les sources (branche `main`), support de l'ingress, interface web sur le port 8000.
