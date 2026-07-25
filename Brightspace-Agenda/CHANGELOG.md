# Changelog - addon Brightspace Agenda

## 1.0.1 - 2026-07-19

### Cause racine du 503 systématique sur `api.php` sous Ingress
`api.php` renvoyait 503 (`code: NO_WRITE`) sur tout appel. `DATA_DIR` pointait
sur `/var/www/html/data`, un lien symbolique vers `/data` créé par `run.sh`.
Diagnostic : `/data` directement est accessible en écriture pour `www-data`
(confirmé), mais le même test via le lien symbolique échoue systématiquement,
alors que les permissions Unix sont correctes à chaque niveau du chemin.
Signature typique du confinement AppArmor des addons, qui bloque la
traversée d'un lien symbolique entre la couche d'image du conteneur et un
volume monté séparément, indépendamment des permissions Unix classiques.

**Correctif** : le lien symbolique est entièrement retiré. `api.php` et
`setup.php` lisent désormais `DATA_DIR` depuis la variable d'environnement
`BSA_DATA_DIR` (définie sur `/data` via `ENV` dans le `Dockerfile`),
troisième correctif appliqué au build (`rootfs-build/patch-data-dir-env.sh`).
Comportement inchangé pour tout hébergement hors addon (fallback sur
`__DIR__.'/data'` si la variable est absente). Bénéfice supplémentaire :
`/data` n'est plus jamais exposé sous le docroot web, donc
`bsa-allowoverride.conf` (protection par `.htaccess`) n'a plus lieu d'être
et a été retiré du `Dockerfile` (fichier à supprimer du dépôt). `run.sh`
simplifié en conséquence : ne garde que le `chown -R www-data:www-data /data`.

### Corrections sur la publication Discovery
- **`curl` (binaire CLI) installé**, absent de l'image jusqu'ici : seuls
  `libcurl4-openssl-dev` (en-têtes de dev) et l'extension PHP curl étaient
  installés, aucun des deux ne fournit `/usr/bin/curl`.
  `bsa-publish-discovery.sh` échouait donc silencieusement à chaque
  démarrage (erreur "command not found" avalée par son propre
  `|| echo "{}"`).
- **`hassio_role` remonté à `manager`** (était `default`) : role requis par
  l'API Supervisor pour publier un service Discovery (`POST /discovery`),
  sous peine de 403.
- **Interpolation JSON→PHP fragile corrigée** dans
  `bsa-publish-discovery.sh` : les réponses de l'API Supervisor étaient
  collées directement dans une chaîne PHP via le shell (escaping incomplet,
  incohérent entre les deux usages). Remplacé par des fichiers temporaires
  lus via `file_get_contents()`.

### À faire avant de considérer cette version stable
- Diagnostic v4 ajouté dans `run.sh` (le correctif `BSA_DATA_DIR` n'a pas
  résolu le 503 NO_WRITE, confirmé par capture HAR le 19/07) : vérifie si
  `getenv('BSA_DATA_DIR')` est réellement visible côté shell, PHP CLI, et
  PHP CLI en `www-data`, avant de conclure sur la cause.
- Retirer le bloc `DIAGNOSTIC TEMPORAIRE` de `run.sh` une fois `api.php?action=ping`
  confirmé en 200 dans les logs.
- Épingler `BSA_REF` sur un tag/commit précis plutôt que `main`.
- Supprimer `rootfs-build/bsa-allowoverride.conf` du dépôt (plus référencé).

## 1.0.0 - 2026-07-18

Première version de l'addon, avec correctifs suite au premier essai
d'installation.

### Corrections
- **Bug Ingress critique : import ICS et appels API cassés sous le panneau
  latéral.** `index.html` calcule `API_ORIGIN` comme une chaîne vide, ce qui
  produit des chemins absolus (`/api.php`, `/proxy.php`) au lieu de chemins
  relatifs à la page. Fonctionnait en accès direct, cassait tout appel
  réseau sous Ingress (préfixe de session ignoré). Corrigé au build via
  `rootfs-build/patch-ingress-baseurl.sh` : la base est calculée
  dynamiquement depuis `location.pathname`.
- **Cookie de session Ingress-aware** : le flag `Secure` suit désormais
  l'en-tête `X-Forwarded-Proto` au lieu d'être toujours `true`
  (`rootfs-build/patch-ingress-cookie.sh`).
- **Mot de passe rendu obligatoire** : `dashboard_password` n'a plus de
  valeur par défaut dans `config.yaml`, le Supervisor bloque le démarrage
  tant qu'il n'est pas renseigné.
- **Icône et logo** remplacés par des dérivés de la vraie icône de l'app.
- **Badges d'architecture** ajoutés dans `README.md` (style shields.io).
- **Branche source corrigée : `main` au lieu de `dev`** (branche de travail
  active, potentiellement instable). Vérifié via `origin/HEAD` du dépôt app.
- **Build Docker corrigé** : `docker-php-ext-install curl` échouait
  (en-têtes de dev `libcurl4-openssl-dev` manquantes dans l'image de base).
- **`arch: armv7` retiré** de `config.yaml` (deprecated côté Supervisor).
- **Accents français corrigés** dans tous les fichiers du dépôt.

### Ajouts
- Conteneur Docker `php:8.2-apache` construit directement depuis le dépôt
  public [MrTh0m/Brightspace_agenda](https://github.com/MrTh0m/Brightspace_agenda),
  sans copie locale à synchroniser dans ce dépôt d'addon.
- Panneau latéral Home Assistant via Ingress (port interne 8099) + accès
  direct conservé sur le même port pour PWA, liens de partage, export ICS
  et polling HACS.
- Persistance de `data/config.json` et `data/state.json` sur `/data`.
- Option `dashboard_password` (obligatoire) : provisionnement automatique
  du compte "mode connecté" au premier démarrage.
- Option `timezone` (défaut `Europe/Paris`).

### Notes techniques
- `config.yaml` suit la convention 2026 post-migration BuildKit (pas de
  `build.yaml`, base image et labels directement dans le `Dockerfile`).
