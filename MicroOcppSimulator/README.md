# MicroOcppSimulator_ADDON

Dépôt d'add-on Home Assistant pour [MicroOcppSimulator](https://github.com/matth-x/MicroOcppSimulator), un simulateur de borne de recharge OCPP. Utile pour tester l'intégration `ocpp` (lbbrhzn) sans borne physique.

## Installation

1. Dans Home Assistant : **Paramètres > Add-ons > Boutique des add-ons**.
2. Menu ⋮ (en haut à droite) > **Dépôts**.
3. Ajouter : `https://github.com/MrTh0m/MicroOcppSimulator_ADDON`
4. Fermer, puis rafraîchir la page. L'add-on "MicroOcpp Simulator" apparaît dans la liste.
5. L'installer (la compilation depuis les sources peut prendre plusieurs minutes), puis le démarrer.
6. Accéder à son interface via le panneau latéral (ingress).

Voir [microocppsimulator/DOCS.md](microocppsimulator/DOCS.md) pour l'utilisation détaillée.

## Contenu

- `microocppsimulator/` : l'add-on lui-même (config.yaml, Dockerfile, run.sh, docs)
- `repository.yaml` : métadonnées du dépôt d'add-ons

## Licence

L'add-on compile [MicroOcppSimulator](https://github.com/matth-x/MicroOcppSimulator) (GPL-3.0) depuis ses sources officielles au moment du build. Aucun code n'est vendored dans ce dépôt.
