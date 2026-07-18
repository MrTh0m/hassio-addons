# Brightspace Agenda

📚 Suivi des devoirs, live sessions et ateliers de groupe pour tout
etablissement utilisant **Brightspace by D2L**, auto-heberge directement
depuis Home Assistant.

- **Panneau lateral** (Ingress) pour un acces rapide au quotidien
- **Acces direct** conserve sur le meme port pour l'installation PWA, les
  liens de partage, l'export ICS abonnable et l'**integration HACS**
  `Brightspace_agenda_HACS`
- Donnees persistantes entre mises a jour (`/data`, volume standard du
  Supervisor)
- Compte "mode connecte" provisionnable directement depuis la configuration
  de l'addon, ou via `/setup.php` comme sur un hebergement classique

Voir [DOCS.md](DOCS.md) pour l'installation et la configuration detaillees.

App source : [github.com/MrTh0m/Brightspace_agenda](https://github.com/MrTh0m/Brightspace_agenda)
(licence MIT).
