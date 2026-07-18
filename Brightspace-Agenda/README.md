# Brightspace Agenda (Home Assistant Addon)

![Addon Stage][stage-badge]
![Supports aarch64 Architecture][aarch64-badge]
![Supports amd64 Architecture][amd64-badge]
![Supports armhf Architecture][armhf-badge]
![Supports armv7 Architecture][armv7-badge]
![Supports i386 Architecture][i386-badge]

📚 Suivi des devoirs, live sessions et ateliers de groupe pour tout
établissement utilisant **Brightspace by D2L**, auto-hébergé directement
depuis Home Assistant.

- **Panneau latéral** (Ingress) pour un accès rapide au quotidien
- **Accès direct** conservé sur le même port pour l'installation PWA, les
  liens de partage, l'export ICS abonnable et l'**intégration HACS**
  `Brightspace_agenda_HACS`
- Données persistantes entre mises à jour (`/data`, volume standard du
  Supervisor)
- Compte "mode connecté" provisionné directement depuis la configuration
  de l'addon (obligatoire), ou modifiable ensuite via `/setup.php` comme sur
  un hébergement classique

Voir [DOCS.md](DOCS.md) pour l'installation et la configuration détaillées.

App source : [github.com/MrTh0m/Brightspace_agenda](https://github.com/MrTh0m/Brightspace_agenda)
(licence MIT).

[aarch64-badge]: https://img.shields.io/badge/aarch64-yes-green.svg?style=for-the-badge
[amd64-badge]: https://img.shields.io/badge/amd64-yes-green.svg?style=for-the-badge
[armhf-badge]: https://img.shields.io/badge/armhf-no-red.svg?style=for-the-badge
[armv7-badge]: https://img.shields.io/badge/armv7-no-red.svg?style=for-the-badge
[i386-badge]: https://img.shields.io/badge/i386-no-red.svg?style=for-the-badge
[install-url]: https://my.home-assistant.io/redirect/supervisor_addon?addon=f2da88f1_brightspace_agenda
[stage-badge]: https://img.shields.io/badge/Addon%20stage-experimental%20🧪-yellow.svg?style=for-the-badge
