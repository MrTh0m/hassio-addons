# Brightspace Agenda - addon Home Assistant

Auto-hebergement de [Brightspace Agenda](https://github.com/MrTh0m/Brightspace_agenda)
directement depuis Home Assistant : conteneur Docker gere par le Supervisor,
panneau dans le menu lateral (Ingress), et une URL directe stable pour
l'installation PWA, les liens de partage et l'integration HACS.

## Installation

1. Ajoute ce depot dans **Parametres -> Modules complementaires -> Boutique du
   Supervisor -> ⋮ -> Depots** : `https://github.com/MrTh0m/hassio-addons`
2. Installe **Brightspace Agenda**, configure les options si besoin (voir
   ci-dessous), puis demarre l'addon.
3. Le panneau **Brightspace Agenda** apparait dans le menu lateral de Home
   Assistant.

## Configuration (onglet "Configuration" de l'addon)

| Option | Description |
|---|---|
| `timezone` | Fuseau horaire PHP du conteneur, utilise pour l'export ICS (`date('c')`). Par defaut `Europe/Paris`. |
| `dashboard_password` | Optionnel. Renseigne uniquement au tout premier demarrage : cree automatiquement le compte "mode connecte" (equivalent de passer par `/setup.php`). Laisse vide pour configurer manuellement. Sans effet une fois `data/config.json` deja cree, meme si tu changes cette valeur ensuite. |

## Deux facons d'y acceder

- **Panneau lateral (Ingress)** : acces authentifie via ta session Home
  Assistant, pratique au quotidien. C'est ce que tu ouvres depuis le menu.
- **URL directe** `http://<ip-de-ton-serveur-ha>:8099/` : necessaire pour tout
  ce qui ne passe pas par une session Home Assistant deja ouverte dans le
  navigateur :
  - installation en PWA (Ajouter a l'ecran d'accueil) ;
  - liens de partage en lecture seule (`?share=TOKEN`) ;
  - flux ICS abonnables (`?action=export_ics&token=...`) ;
  - polling de l'**integration HACS** `Brightspace_agenda_HACS`, a pointer
    vers `http://<ip>:8099/api.php`.

## Premiere configuration du mode connecte

Deux options, au choix :

- **Automatique** : renseigne `dashboard_password` dans la configuration de
  l'addon avant le tout premier demarrage (ou avant de recreer `data/`).
- **Manuelle** : laisse `dashboard_password` vide et visite
  `http://<ip>:8099/setup.php` (ou via le panneau Ingress) comme sur un
  hebergement classique.

Dans les deux cas, l'URL ICS Brightspace se configure ensuite depuis les
**Parametres** de l'app elle-meme (jamais stockee dans la configuration
Home Assistant).

## Persistance des donnees

`data/config.json` et `data/state.json` vivent dans `/data`, le volume
persistant standard fourni par le Supervisor pour cet addon : ils survivent
aux mises a jour et redemarrages du conteneur. Une sauvegarde Home Assistant
classique (Parametres -> Systeme -> Sauvegardes) inclut ce volume si l'addon
y est inclus.

## Particularite technique : cookie de session et Ingress

Le code source recupere depuis le depot public est utilise tel quel, a une
exception pres : le flag `Secure` du cookie de session est rendu
"Ingress-aware" (suit l'en-tete `X-Forwarded-Proto`) au moment du build de
l'image Docker, pour que la connexion au mode "connecte" fonctionne meme si
ton instance Home Assistant est servie en HTTP en local. Voir
`rootfs-build/patch-ingress-cookie.sh` pour le detail exact du correctif.

## Mises a jour

L'addon recupere le code source depuis la branche/tag `main` (branche stable
par defaut du depot, pas `dev` qui est la branche de travail active) au
moment du **build** de l'image (`ARG BSA_REF` dans le `Dockerfile`), pas a
l'execution. Pour publier une nouvelle version de l'addon :

1. Merge les changements valides de `dev` vers `main` dans le depot app.
2. Epingle `BSA_REF` sur le tag/commit voulu (recommande plutot que `main`
   directement pour une version publiee, afin d'avoir un build reproductible).
3. Incremente `version` dans `config.yaml` (ce qui casse aussi le cache Docker
   du `git clone`, voir plus haut).
4. Ajoute une entree dans `CHANGELOG.md`.
5. Reconstruis l'addon depuis l'onglet **Infos** (bouton **Reconstruire**).

## Support

Bugs et suggestions : [github.com/MrTh0m/Brightspace_agenda/issues](https://github.com/MrTh0m/Brightspace_agenda/issues)
