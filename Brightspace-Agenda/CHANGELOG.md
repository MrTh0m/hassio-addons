# Changelog - addon Brightspace Agenda

## 1.0.0 - 2026-07-18 (mise à jour 3)

### Diagnostic 503 corrigé (v1 testait le mauvais chemin)
- Le premier diagnostic testait `/data` (la cible réelle du lien symbolique)
  et concluait à tort que www-data pouvait écrire. Le log confirme pourtant
  que le 503 persiste (`code: NO_WRITE`) sur `api.php?action=ping`, seul
  point de sortie 503 possible pour cette action (l'autre, `NOT_CONFIGURED`,
  exclut explicitement `ping`).
- En cause : le code PHP teste `DATA_DIR = __DIR__.'/data'`, donc
  `/var/www/html/data` (le **lien symbolique lui-même**), pas `/data`
  directement. Un chemin accédé via un lien symbolique peut être médiatisé
  différemment par un profil de confinement même quand les permissions
  Unix classiques sont correctes de bout en bout.
- Diagnostic v2 dans `run.sh` : teste le bon chemin (`/var/www/html/data`)
  avec `is_dir()`/`is_writable()` de PHP directement (mêmes fonctions
  qu'`api.php`, exécutées en `www-data`), ajoute `stat -L` sur le chemin
  résolu et un contrôle AppArmor (`/proc/self/attr/current`, `dmesg`).

## 1.0.0 - 2026-07-18 (mise à jour 2)

### En cours d'investigation : 503 systématique sur api.php sous Ingress
- Confirmé par les logs Apache : `api.php` renvoie 503 (`code: NO_WRITE`) sur
  tout appel, `is_writable(DATA_DIR)` échouant côté processus Apache/www-data
  alors que `bsa-bootstrap.php` (exécuté en root au démarrage) écrit sans
  problème dans le même dossier. `proxy.php` et les fichiers statiques ne
  sont pas concernés (aucune dépendance à `DATA_DIR`).
- Diagnostic temporaire ajouté dans `run.sh` (bloc marqué `DIAGNOSTIC
  TEMPORAIRE`, à retirer une fois la cause confirmée) : compare les
  permissions réelles de `/data`, simule un test d'écriture en `www-data`,
  et recherche une éventuelle directive `open_basedir` côté SAPI Apache
  (qui n'affecterait pas le CLI utilisé par le bootstrap).

## 1.0.0 - 2026-07-18 (mise à jour)

### Corrections sur la publication Discovery
- **`curl` (binaire CLI) installé**, absent de l'image jusqu'ici : seuls `libcurl4-openssl-dev` (en-têtes de dev) et l'extension PHP curl étaient installés, aucun des deux ne fournit `/usr/bin/curl`. `bsa-publish-discovery.sh` échouait donc silencieusement à chaque démarrage (erreur "command not found" avalue par son propre `|| echo "{}"`).
- **`hassio_role` remonté à `manager`** (était `default`) : role requis par l'API Supervisor pour publier un service Discovery (`POST /discovery`), sous peine de 403.
- **Interpolation JSON→PHP fragile corrigée** dans `bsa-publish-discovery.sh` : les réponses de l'API Supervisor étaient collées directement dans une chaîne PHP via le shell (escaping incomplet, incohérent entre les deux usages). Remplacé par des fichiers temporaires lus via `file_get_contents()`, qui élimine le problème par construction plutôt que de rafistiner l'escaping.

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
  de travail active (potentiellement instable à tout moment) : un
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
