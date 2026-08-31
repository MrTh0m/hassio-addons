# OCPP Backoffice Server (Home Assistant Addon)

Serveur OCPP 1.6 auto-hébergé (CSMS) pour piloter une ou plusieurs bornes de recharge de véhicule électrique, avec API REST, interface d'administration complète et intégration native à Home Assistant. Cœur backoffice d'un écosystème de recharge personnel : une seule ou plusieurs bornes, en local ou en relais transparent vers leur serveur officiel, avec suivi des coûts, des véhicules et de la consommation au fil des charges.

![Addon Stage][stage-badge]
![Supports aarch64 Architecture][aarch64-badge]
![Supports amd64 Architecture][amd64-badge]
![Supports armhf Architecture][armhf-badge]
![Supports armv7 Architecture][armv7-badge]
![Supports i386 Architecture][i386-badge]

## Fonctionnalités

- **Deux modes par borne** : pilotage local complet (démarrer/arrêter une charge, lire et modifier sa configuration OCPP), ou relais transparent vers le serveur officiel du fabricant tout en captant les métriques au passage.
- **Suivi des coûts** : abonnements électriques avec plages horaires nommées (heures pleines/creuses ou tout autre découpage), coût calculé au prorata par tranche tarifaire et figé définitivement à la clôture de chaque charge.
- **Gestion des véhicules** : association automatique par badge (idTag), historique par véhicule, kilométrage, consommation (kWh/100 km), taux de recharge estimé.
- **Programmation de la charge** : départ différé, restriction aux heures creuses, ou prête pour une heure donnée, pilotée via SmartCharging quand la borne le supporte.
- **Multi-utilisateurs** : comptes secondaires avec droits granulaires et association à un sous-ensemble de véhicules/bornes.
- **Intégration Home Assistant** : entités MQTT Discovery automatiques (statut, puissance, énergie, durée, switch de pilotage) par connecteur.
- **Interface complète** : page d'accueil temps réel (SSE), historique filtrable et exportable (CSV), journal OCPP en direct, navigateur de base de données en lecture seule, export/import complet des données, PWA installable.

Voir [DOCS.md](DOCS.md) pour le détail de chaque fonctionnalité, la connexion d'une borne, la bascule local/relais et les limitations connues de cette version.

## Stack technique

Python / [FastAPI](https://fastapi.tiangolo.com/), SQLAlchemy + SQLite, [`mobilityhouse/ocpp`](https://github.com/mobilityhouse/ocpp) pour la couche protocole OCPP 1.6, authentification JWT + bcrypt, pont MQTT vers Home Assistant, interface d'administration en HTML/JS natif (aucun framework front).

```
app/            code de l'add-on (FastAPI, CSMS OCPP, pont MQTT, planificateur, interface admin)
tests/          suite de tests (pytest) sur le calcul de coût/énergie et l'API REST
Dockerfile      image de l'add-on
run.sh          point d'entrée, lit la config Home Assistant (resolve_config.py)
config.json     manifeste de l'add-on (options, ports, architectures supportées)
```

## Statut

Add-on expérimental en développement actif, utilisé en conditions réelles sur bornes simulées (MicroOCPP) et sur borne physique (Schneider EVH5A22N400F). Pas encore de version stable garantie ; voir le [CHANGELOG](CHANGELOG.md) pour l'historique détaillé des versions.

## Licence

Basé sur [`mobilityhouse/ocpp`](https://github.com/mobilityhouse/ocpp) (MIT) pour la couche protocole.

[aarch64-badge]: https://img.shields.io/badge/aarch64-yes-green.svg?style=for-the-badge
[amd64-badge]: https://img.shields.io/badge/amd64-yes-green.svg?style=for-the-badge
[armhf-badge]: https://img.shields.io/badge/armhf-yes-green.svg?style=for-the-badge
[armv7-badge]: https://img.shields.io/badge/armv7-yes-green.svg?style=for-the-badge
[i386-badge]: https://img.shields.io/badge/i386-yes-green.svg?style=for-the-badge
[stage-badge]: https://img.shields.io/badge/Addon%20stage-experimental%20🧪-yellow.svg?style=for-the-badge
