# Brightspace Agenda - addon Home Assistant

Auto-hébergement de [Brightspace Agenda](https://github.com/MrTh0m/Brightspace_agenda)
directement depuis Home Assistant : conteneur Docker géré par le Supervisor,
panneau dans le menu latéral (Ingress), et une URL directe stable pour
l'installation PWA, les liens de partage et l'intégration HACS.

## Installation

1. Ajoute ce dépôt dans **Paramètres -> Modules complémentaires -> Boutique du
   Supervisor -> ⋮ -> Dépôts** : `https://github.com/MrTh0m/hassio-addons`
2. Installe **Brightspace Agenda**, renseigne `dashboard_password` dans
   l'onglet **Configuration** (obligatoire, voir ci-dessous), puis démarre
   l'addon.
3. Le panneau **Brightspace Agenda** apparaît dans le menu latéral de Home
   Assistant.

## Configuration (onglet "Configuration" de l'addon)

| Option | Description |
|---|---|
| `timezone` | Fuseau horaire PHP du conteneur, utilisé pour l'export ICS (`date('c')`). Par défaut `Europe/Paris`. |
| `dashboard_password` | **Obligatoire.** L'app fonctionne toujours en mode connecté, qui nécessite un compte. Utilisé uniquement au tout premier démarrage pour créer ce compte automatiquement (équivalent de passer par `/setup.php`). Sans effet une fois `data/config.json` déjà créé, même si tu changes cette valeur ensuite : le mot de passe se change alors depuis les Paramètres de l'app ou via `/setup.php`. |

## Deux façons d'y accéder

- **Panneau latéral (Ingress)** : accès authentifié via ta session Home
  Assistant, pratique au quotidien. C'est ce que tu ouvres depuis le menu.
- **URL directe** `http://<ip-de-ton-serveur-ha>:8099/` : nécessaire pour tout
  ce qui ne passe pas par une session Home Assistant déjà ouverte dans le
  navigateur :
  - installation en PWA (Ajouter à l'écran d'accueil) ;
  - liens de partage en lecture seule (`?share=TOKEN`) ;
  - flux ICS abonnables (`?action=export_ics&token=...`) ;
  - polling de l'**intégration HACS** `Brightspace_agenda_HACS`, à pointer
    vers `http://<ip>:8099/api.php` ;
  - connexion depuis l'**APK Android** en mode connecté : renseigne
    `http://<ip>:8099` comme URL de serveur, jamais une URL Ingress.
    L'Ingress exige une session Home Assistant déjà authentifiée dans un
    navigateur ; un appel HTTP direct depuis l'APK n'en a pas et sera rejeté,
    quelle que soit l'URL utilisée.

## Premier démarrage

Renseigne `dashboard_password` dans la configuration de l'addon **avant** le
tout premier démarrage : le compte "mode connecté" est créé automatiquement
à ce moment-là. Une fois `data/config.json` créé, cette option n'a plus
d'effet, le mot de passe se change alors depuis les **Paramètres** de l'app
ou via `/setup.php`.

L'URL ICS Brightspace se configure toujours depuis les **Paramètres** de
l'app elle-même (jamais stockée dans la configuration Home Assistant).

## Persistance des données

`data/config.json` et `data/state.json` vivent dans `/data`, le volume
persistant standard fourni par le Supervisor pour cet addon : ils survivent
aux mises à jour et redémarrages du conteneur. Une sauvegarde Home Assistant
classique (Paramètres -> Système -> Sauvegardes) inclut ce volume si l'addon
y est inclus.

Pour repartir de zéro (nouveau mot de passe, nouvelle installation) : `/data` est le volume persistant de l'addon, conçu pour survivre aux redémarrages et aux reconstructions d'image, c'est voulu. **Problème connu : même en cochant "Supprimer également les données de l'application" lors de la désinstallation, les anciennes données (compte, URL ICS) peuvent réapparaître à la réinstallation.** Deux causes possibles à distinguer avant de conclure :
1. Cache navigateur (session/cookie encore valide) plutôt que `/data` réellement conservé : teste en navigation privée.
2. `/data` effectivement pas vidé par le Supervisor pour cet addon : vérifie directement via un addon type **Studio Code Server** ou **Terminal & SSH** si `config.json`/`state.json` existent encore juste après une désinstallation avec suppression des données cochée.

## Particularités techniques : trois correctifs appliqués au build

Le code source récupéré depuis le dépôt public est utilisé tel quel, à trois
exceptions près, appliquées uniquement au moment du build de l'image Docker
(jamais dans le dépôt de l'app lui-même) :

- **Cookie de session** : le flag `Secure` suit désormais l'en-tête
  `X-Forwarded-Proto` au lieu d'être toujours `true`, pour que la connexion
  au mode connecté fonctionne même si Home Assistant est servi en HTTP en
  local. Voir `rootfs-build/patch-ingress-cookie.sh`.
- **Base des appels API** : `API_ORIGIN` (utilisé pour tous les appels à
  `api.php`/`proxy.php`) était fixé en dur à une chaîne vide, ce qui produit
  des chemins absolus (`/api.php`, `/proxy.php`). Ça fonctionne en accès
  direct mais casse tout appel réseau sous le panneau Ingress, qui sert la
  page sous un préfixe de session (`/api/hassio_ingress/<token>/...`) que
  ces chemins absolus ignorent. Corrigé en calculant la base dynamiquement
  depuis `location.pathname`. Voir `rootfs-build/patch-ingress-baseurl.sh`.
  **C'est ce correctif qui résout l'échec d'import ICS
  ("Vérifie que proxy.php est bien uploadé...")** rencontré sous Ingress.
- **Emplacement de `DATA_DIR`** : `api.php`/`setup.php` lisent la variable
  d'environnement `BSA_DATA_DIR` (définie sur `/data` dans le `Dockerfile`)
  au lieu de calculer un chemin relatif (`__DIR__.'/data'`) qui passait par
  un lien symbolique depuis `/var/www/html/data`. Ce lien traversait deux
  points de montage différents (couche d'image du conteneur d'un côté,
  volume `/data` du Supervisor de l'autre), ce que le confinement AppArmor
  des addons bloque même quand les permissions Unix classiques sont
  correctes de bout en bout. **C'est ce correctif qui résout le 503
  systématique sur `api.php` sous Ingress** (`code: NO_WRITE`, alors que
  `/data` était pourtant accessible en écriture). Voir
  `rootfs-build/patch-data-dir-env.sh`.

## Mises à jour

L'addon récupère le code source depuis la branche/tag `main` (branche stable
par défaut du dépôt, pas `dev` qui est la branche de travail active) au
moment du **build** de l'image (`ARG BSA_REF` dans le `Dockerfile`), pas à
l'exécution. Pour publier une nouvelle version de l'addon :

1. Merge les changements validés de `dev` vers `main` dans le dépôt app.
2. Épingle `BSA_REF` sur le tag/commit voulu (recommandé plutôt que `main`
   directement pour une version publiée, afin d'avoir un build reproductible).
3. Incrémente `version` dans `config.yaml` (ce qui casse aussi le cache Docker
   du `git clone`, voir plus haut).
4. Ajoute une entrée dans `CHANGELOG.md`.
5. Reconstruis l'addon depuis l'onglet **Infos** (bouton **Reconstruire**).

## Support

Bugs et suggestions : [github.com/MrTh0m/Brightspace_agenda/issues](https://github.com/MrTh0m/Brightspace_agenda/issues)
