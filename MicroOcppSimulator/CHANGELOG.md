# Changelog

## 1.1.0

- Fix : le dashboard web est reconstruit au moment du build avec une URL d'API relative (`API_ROOT=api`) au lieu de l'URL `http://localhost:8000/api` figée en dur par le projet upstream. Corrige l'erreur "Unable to fetch connectors" lors d'un accès via une IP/nom d'hôte autre que `localhost`.
- Fix : la page est désormais servie non compressée (`bundle.html` au lieu de `bundle.html.gz`), ce qui corrige le téléchargement du fichier `.gz` au lieu de l'affichage lors d'un accès via l'ingress de Home Assistant.
- Ajout d'un argument de build `MO_NUMCONNECTORS` (via `build.json`) pour choisir entre 1 ou 2 connecteurs simulés.
- Support de 5 architectures (aarch64, amd64, armhf, armv7, i386).

## 1.0.0

- Première version : build de MicroOcppSimulator depuis les sources (branche `main`), support de l'ingress, interface web sur le port 8000.
