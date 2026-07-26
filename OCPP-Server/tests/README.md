# Tests manuels

Scripts utilisés pour valider le serveur pendant son développement. Pas une suite automatisée (pas de pytest), juste des scénarios à lancer à la main pendant que le serveur tourne.

Installation (hors container, sur ta machine) :

```
pip install -r ../requirements.txt httpx
```

Lancer le serveur (dans un premier terminal) :

```
cd ..
OCPP_DATA_DIR=/tmp/ocpp_data OCPP_ADMIN_PASSWORD=admin python -m uvicorn app.main:app --port 8000
```

Puis, dans un second terminal :

- `python test_client.py` : simule une borne en mode local, envoie tout le cycle d'une session de charge (Boot, StatusNotification, StartTransaction, MeterValues, StopTransaction).
- `python test_remote.py` : simule une borne qui reste connectée et écoute les commandes ; envoie en parallèle des requêtes API (démarrage à distance, lecture/écriture de configuration) pour vérifier le pilotage depuis le backoffice.
- `mock_official_server.py` + `test_relay.py` : lance d'abord `python mock_official_server.py` (simule un "serveur officiel" sur le port 9999), configure une borne en mode relais via l'API (`PUT /api/chargers/RELAY-CP-01/mode`), puis lance `python test_relay.py` pour vérifier que le relais transmet bien le trafic et capture les métriques au passage.
