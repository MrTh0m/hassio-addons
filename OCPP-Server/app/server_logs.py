"""Journal serveur (application) en mémoire vive.

Capture les messages émis via le module `logging` standard de Python (loggers
"ocpp-server", "mqtt-bridge", "ocpp", "uvicorn.error", ...), en complément du
journal OCPP structuré (voir ocpp_logs.py) qui ne couvre que les échanges
protocolaires. Objectif : diagnostiquer depuis l'interface un problème qui
touche l'infrastructure plutôt que le protocole OCPP lui-même (panne MQTT,
exception non gérée, etc.) — exactement ce qui a permis d'identifier
l'incident MQTT corrigé en 0.19.22, jusque-là visible uniquement dans le
journal brut du conteneur (Supervisor Home Assistant), pas dans l'appli.

Même principe que ocpp_logs.py : tampon circulaire en mémoire, rien n'est
persisté, perdu au redémarrage de l'addon.

Les logs d'accès HTTP (`uvicorn.access`, une ligne par requête, y compris le
polling de l'interface toutes les quelques secondes) sont très bruyants et
peu utiles au diagnostic : ignorés par défaut, activables comme les
MeterValues dans ocpp_logs.py.
"""

import itertools
import logging
import threading
from collections import deque
from datetime import datetime

MAX_ENTRIES = 2000

_buffer: "deque[dict]" = deque(maxlen=MAX_ENTRIES)
_lock = threading.Lock()
_counter = itertools.count(1)

_capture_access_logs = False


def set_capture_access_logs(enabled: bool) -> None:
    global _capture_access_logs
    _capture_access_logs = bool(enabled)


def is_capturing_access_logs() -> bool:
    return _capture_access_logs


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if record.name == "uvicorn.access" and not _capture_access_logs:
            return
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        entry = {
            "id": next(_counter),
            "ts": datetime.utcnow().isoformat(),
            "logger": record.name,
            "level": record.levelname,
            "message": message,
        }
        with _lock:
            _buffer.append(entry)


_handler = _BufferHandler()
_handler.setLevel(logging.INFO)


def install() -> None:
    """À appeler une fois au démarrage : attache le tampon au logger racine,
    donc à tous les loggers de l'application et de ses dépendances (ocpp,
    aiomqtt, uvicorn...), sans avoir à les lister un par un. N'affecte pas la
    sortie console existante (basicConfig) : ceci ajoute un second handler,
    ne remplace rien."""
    root = logging.getLogger()
    if _handler not in root.handlers:
        root.addHandler(_handler)


def get_entries(logger: str = None, level: str = None,
                since_id: int = 0, limit: int = 500) -> list:
    """Renvoie les entrées, de la plus récente à la plus ancienne, après
    application des filtres. `since_id` permet de ne récupérer que les
    nouvelles entrées depuis un identifiant donné (rafraîchissement
    incrémental)."""
    with _lock:
        items = list(_buffer)
    out = []
    for e in reversed(items):
        if e["id"] <= since_id:
            continue
        if logger and e["logger"] != logger:
            continue
        if level and e["level"] != level:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def known_loggers() -> list:
    """Liste des loggers ayant produit au moins une entrée dans le tampon."""
    with _lock:
        return sorted({e["logger"] for e in _buffer})


def clear() -> None:
    with _lock:
        _buffer.clear()
