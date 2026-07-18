# Changelog - addon Brightspace Agenda

## 1.0.0 - 2026-07-18

Première version de l'addon.

### Correctifs (suite au retour de test après installation)
- **Bug Ingress critique corrigé : import ICS et appels API cassés sous le
  panneau latéral.** `index.html` calcule `API_ORIGIN` comme une chaîne
  vide, ce qui produit des chemins absolus (`/api.php`, `/proxy.php`) au
  lieu de chemins relatifs à la page. Fonctionnait en accès direct, cassait
  tout appel réseau sous Ingress (préfixe de session ignoré). Corrigé au
  build via `rootfs-build/patch-ingress-baseurl.sh` : la base est calculée
  dynamiquement depuis `location.pathname`.
- **Mot de passe rendu obligatoire.** L'app fonctionne toujours en mode
  connecté : `dashboard_password` n'a plus de valeur par défaut dans
  `config.yaml`, le Supervisor bloque désormais le démarrage tant qu'il
  n'est pas renseigné.
- **Icône et logo remplacés** par des dérivés de la vraie icône de l'app
  (`icon-192.png`/`icon-512.png`), au lieu d'un glyphe générique généré.
- **Badges d'architecture ajoutés** dans `README.md` (style shields.io).
- **Branche source corrigée : `main` au lieu de `dev`**. `dev` est la branche
  de travail active de Thomas (potentiellement instable à tout moment) : un
  addon public ne doit jamais builder dessus. Vérifié via `origin/HEAD` du
  dépôt local (`E:\GIT\Brightspace_agenda`) que `main` est bien la branche
  par défaut/stable sur GitHub. `ARG BSA_REF` passe de `dev` à `main`.
- **Build Docker corrigé** : `docker-php-ext-install curl` échouait
  (`Package 'libcurl', required by 'virtual:world', not found`) car il
  manquait les en-têtes de dev de libcurl (`libcurl4-openssl-dev`), non
  fournies par l'image de base `php:8.2-apache`. Ajoutées avant la
  compilation de l'extension.
- **`arch: armv7` retiré** de `config.yaml` : valeur signalée comme
  deprecated par le Supervisor (`App config 'arch' uses deprecated values
  ['armv7']`). L'hôte observé dans le log est en `aarch64`, déjà couvert.
- **Accents français corrigés** dans `config.yaml`, `DOCS.md`, `README.md`,
  et les commentaires du `Dockerfile`/scripts (ASCII sans accents à
  l'origine, non voulu).

### Ajouts
- Conteneur Docker `php:8.2-apache` construit directement depuis le dépôt
  public [MrTh0m/Brightspace_agenda](https://github.com/MrTh0m/Brightspace_agenda)
  (branche `main`, via `ARG BSA_REF`), sans copie locale à synchroniser dans
  ce dépôt d'addon.
- Panneau latéral Home Assistant via Ingress (port interne 8099).
- Accès direct conservé sur le même port 8099 pour l'installation PWA, les
  liens de partage, l'export ICS abonnable et le polling de l'intégration
  HACS `Brightspace_agenda_HACS`.
- Persistance de `data/config.json` et `data/state.json` sur le volume
  `/data` fourni par le Supervisor (survit aux mises à jour/redémarrages).
- Option `dashboard_password` (obligatoire) : provisionnement automatique du
  compte "mode connecté" au premier démarrage, sans passer par `/setup.php`.
- Option `timezone` (défaut `Europe/Paris`) appliquée au PHP du conteneur.

### Notes techniques
- `config.yaml` suit la convention 2026 post-migration BuildKit (pas de
  `build.yaml`, base image et labels directement dans le `Dockerfile`).
- Deux correctifs appliqués au code source au moment du build, jamais dans
  le dépôt de l'app lui-même : le cookie de session Ingress-aware
  (`rootfs-build/patch-ingress-cookie.sh`) et la base des appels API
  Ingress-aware (`rootfs-build/patch-ingress-baseurl.sh`).

### À faire avant une utilisation en production
- Épingler `BSA_REF` sur un tag/commit précis plutôt que `main` pour un
  build reproductible.
- Tester le build réel (`docker build .` ou installation directe dans le
  Supervisor) : le Dockerfile et les scripts n'ont pas pu être exécutés dans
  cet environnement de travail (pas d'accès réseau/Docker ici), seule la
  logique des scripts de patch a été testée en isolation contre des copies
  fidèles des fichiers réels.
