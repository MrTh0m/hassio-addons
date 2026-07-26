# MicroOcpp Simulator (Home Assistant Addon)

Simule une borne de recharge OCPP (client) pour tester l'intégration `ocpp` (lbbrhzn) de Home Assistant sans borne physique. Basé sur [MicroOcppSimulator](https://github.com/matth-x/MicroOcppSimulator).

![Addon Stage][stage-badge]
![Supports aarch64 Architecture][aarch64-badge]
![Supports amd64 Architecture][amd64-badge]
![Supports armhf Architecture][armhf-badge]
![Supports armv7 Architecture][armv7-badge]
![Supports i386 Architecture][i386-badge]

## À propos

Cet addon compile [MicroOcppSimulator](https://github.com/matth-x/MicroOcppSimulator) depuis ses sources officielles au moment du build (pas d'image pré-construite), donc la première installation peut prendre plusieurs minutes selon le matériel.

Voir [DOCS.md](DOCS.md) pour l'utilisation détaillée (connexion au serveur OCPP, remarques sur l'ingress, etc.).

## Licence

MicroOcppSimulator est distribué sous licence GPL-3.0 (dépendance à la bibliothèque Mongoose). Aucun code n'est vendored dans ce dépôt, les sources sont récupérées depuis le dépôt officiel au moment du build.

[aarch64-badge]: https://img.shields.io/badge/aarch64-yes-green.svg?style=for-the-badge
[amd64-badge]: https://img.shields.io/badge/amd64-yes-green.svg?style=for-the-badge
[armhf-badge]: https://img.shields.io/badge/armhf-yes-green.svg?style=for-the-badge
[armv7-badge]: https://img.shields.io/badge/armv7-yes-green.svg?style=for-the-badge
[i386-badge]: https://img.shields.io/badge/i386-yes-green.svg?style=for-the-badge
[stage-badge]: https://img.shields.io/badge/Addon%20stage-experimental%20🧪-yellow.svg?style=for-the-badge
