# Changelog - addon Brightspace Agenda

## 1.0.0 - 2026-07-18

Premiere version de l'addon.

### Correctifs (suite au premier essai d'installation en echec)
- **Branche source corrigee : `main` au lieu de `dev`**. `dev` est la branche
  de travail active de Thomas (potentiellement instable a tout moment) : un
  addon public ne doit jamais builder dessus. Verifie via `origin/HEAD` du
  depot local (`E:\GIT\Brightspace_agenda`) que `main` est bien la branche
  par defaut/stable sur GitHub. `ARG BSA_REF` passe de `dev` a `main`.
- **Build Docker corrige** : `docker-php-ext-install curl` echouait
  (`Package 'libcurl', required by 'virtual:world', not found`) car il
  manquait les en-tetes de dev de libcurl (`libcurl4-openssl-dev`), non
  fournies par l'image de base `php:8.2-apache`. Ajoutees avant la
  compilation de l'extension.
- **`arch: armv7` retire** de `config.yaml` : valeur signalee comme
  deprecated par le Supervisor (`App config 'arch' uses deprecated values
  ['armv7']`). L'hote observe dans le log est en `aarch64`, deja couvert.

### Ajouts
- Conteneur Docker `php:8.2-apache` construit directement depuis le depot
  public [MrTh0m/Brightspace_agenda](https://github.com/MrTh0m/Brightspace_agenda)
  (branche `dev` par defaut, via `ARG BSA_REF`), sans copie locale a
  synchroniser dans ce depot d'addon.
- Panneau lateral Home Assistant via Ingress (port interne 8099).
- Acces direct conserve sur le meme port 8099 pour l'installation PWA, les
  liens de partage, l'export ICS abonnable et le polling de l'integration
  HACS `Brightspace_agenda_HACS`.
- Persistance de `data/config.json` et `data/state.json` sur le volume
  `/data` fourni par le Supervisor (survit aux mises a jour/redemarrages).
- Option `dashboard_password` : provisionnement automatique du compte "mode
  connecte" au premier demarrage, sans passer par `/setup.php`.
- Option `timezone` (defaut `Europe/Paris`) appliquee au PHP du conteneur.

### Notes techniques
- `config.yaml` suit la convention 2026 post-migration BuildKit (pas de
  `build.yaml`, base image et labels directement dans le `Dockerfile`).
- Un seul correctif applique au code source au moment du build (voir
  `rootfs-build/patch-ingress-cookie.sh`) : le flag `Secure` des 2 cookies de
  session d'`api.php` suit desormais l'en-tete `X-Forwarded-Proto` transmis
  par le reverse-proxy Ingress, pour que le mode connecte fonctionne meme si
  Home Assistant est servi en HTTP en local. Aucun autre changement au code
  source.

### A faire avant une utilisation en production
- Ajouter `icon.png` (128x128) et `logo.png` (~250x100) a la racine de
  l'addon (par exemple a partir de `icon-512.png` de l'app).
- Epingler `BSA_REF` sur un tag/commit precis plutot que `dev` pour un build
  reproductible.
- Tester le build reel (`docker build .` ou installation directe dans le
  Supervisor) : le Dockerfile et les scripts n'ont pas pu etre executes dans
  cet environnement de travail (pas d'acces reseau/Docker ici).
