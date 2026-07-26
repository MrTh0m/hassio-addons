# OCPP Backoffice Server

Serveur OCPP 1.6 (CSMS) auto-hébergé : accueille une ou plusieurs bornes, avec pour chacune un choix entre pilotage local complet ou relais transparent vers le serveur officiel du fabricant. Expose une API REST pour piloter les charges, lire les métriques et gérer la configuration.

## Premier démarrage

1. Démarre l'add-on. Un compte `admin` est créé automatiquement, avec le mot de passe défini dans l'onglet "Configuration" de l'add-on (`admin_password`, à changer avant le premier démarrage si possible).
2. Ouvre `http://<ip-de-ton-serveur>:8000/docs` (ou via le panneau latéral, ingress) : c'est la documentation interactive Swagger générée par l'API, utilisable directement pour tester chaque endpoint sans rien coder, tant qu'il n'y a pas encore d'interface web dédiée.
3. Authentifie-toi via `POST /api/auth/login` (`admin` + ton mot de passe) pour récupérer un token, à utiliser ensuite via le bouton "Authorize" de Swagger.

## Connecter une borne

Configure la borne (via son appli constructeur, eSetup, etc.) pour qu'elle se connecte à :

```
ws://<ip-de-ton-serveur>:8000/ocpp/<identifiant-de-la-borne>
```

L'identifiant est libre (ex. `garage-01`), c'est celui que tu retrouveras ensuite dans l'API. À la première connexion, la borne est enregistrée automatiquement en mode `local`.

## Mode local vs relais

Par défaut, une borne nouvellement connectée est en mode `local` : le serveur backoffice a le contrôle complet (démarrer/arrêter une charge, lire/modifier sa configuration).

Pour basculer une borne en mode relais (utile si elle doit rester gérée par le serveur de son fabricant, ex. Wiser) :

```
PUT /api/chargers/{id}/mode
{
  "mode": "relay",
  "relay_url": "wss://serveur-officiel.example.com/chemin"
}
```

En mode relais, le pilotage (démarrage/arrêt, configuration) est désactivé côté API (réponse 409), seules les métriques et l'historique des sessions restent disponibles : le relais capte passivement le trafic sans jamais le modifier.

## Limitations connues de cette version

- **OCPP 1.6 uniquement.** Le 2.0.1 n'est pas encore implémenté (la bibliothèque utilisée le permettrait, ce sera pour une prochaine version).
- **Un seul compte administrateur.** Pas encore de gestion multi-utilisateurs ni de rattachement borne↔utilisateur.
- **Mode relais, StopTransaction :** l'identifiant de transaction assigné par le serveur officiel (reçu dans sa réponse, pas dans la requête de la borne) n'est pas encore relié à la transaction correspondante en base à la clôture. Les valeurs de compteur en cours de charge sont bien capturées, seule la clôture propre de la session peut manquer une association.
- **Pas encore de pont MQTT/Home Assistant.** L'API REST existe, l'intégration côté HA est la prochaine étape.

## Licence

Basé sur [`mobilityhouse/ocpp`](https://github.com/mobilityhouse/ocpp) (MIT) pour la couche protocole.
