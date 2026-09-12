"""Journal OCPP en mémoire vive.

Un tampon circulaire borné conserve les derniers messages OCPP échangés avec
les bornes, tous modes confondus (local et relais). Objectif : diagnostic en
direct depuis l'interface (« que se passe-t-il sur ma borne à l'instant T »),
sans aucun impact sur la base de données. Rien n'est persisté : le journal est
volontairement perdu au redémarrage de l'addon.

Un point d'entrée unique, `record()`, est appelé depuis le handler local
(csms_local.py) comme depuis le snoop du relais (relay.py). Les MeterValues,
très fréquents, ne sont journalisés que si l'utilisateur l'a activé.
"""

import itertools
import threading
from collections import deque
from datetime import datetime

# Taille maximale du tampon (nombre d'entrées conservées, toutes bornes
# confondues). Au-delà, les plus anciennes sont écartées automatiquement.
MAX_ENTRIES = 2000

# Actions considérées comme « à fort volume » : journalisées seulement quand
# l'utilisateur active explicitement leur capture.
HIGH_VOLUME_ACTIONS = {"MeterValues"}

_buffer: "deque[dict]" = deque(maxlen=MAX_ENTRIES)
_lock = threading.Lock()
_counter = itertools.count(1)

# Interrupteur d'exécution : capture (ou non) les messages à fort volume.
_capture_high_volume = False


def set_capture_high_volume(enabled: bool) -> None:
    global _capture_high_volume
    _capture_high_volume = bool(enabled)


def is_capturing_high_volume() -> bool:
    return _capture_high_volume


def record(charger_id: str, direction: str, action: str,
           summary: str = "", payload=None, connector_id=None) -> None:
    """Ajoute une entrée au journal.

    - direction : "in" (borne -> CSMS) ou "out" (CSMS/officiel -> borne).
    - action    : nom du message OCPP (BootNotification, StatusNotification...).
    - summary   : résumé lisible (facultatif).
    - payload   : charge utile brute (dict/JSON-serialisable) ou None.
    - connector_id : connecteur concerné si connu.

    Les messages à fort volume (MeterValues) sont ignorés tant que leur capture
    n'a pas été activée via set_capture_high_volume(True).
    """
    if action in HIGH_VOLUME_ACTIONS and not _capture_high_volume:
        return
    entry = {
        "id": next(_counter),
        "ts": datetime.utcnow().isoformat(),
        "charger_id": charger_id,
        "connector_id": connector_id,
        "direction": direction,
        "action": action,
        "summary": summary or "",
        "payload": payload,
    }
    with _lock:
        _buffer.append(entry)


def get_entries(charger_id: str = None, action: str = None,
                direction: str = None, since_id: int = 0, limit: int = 500) -> list:
    """Renvoie les entrées, de la plus récente à la plus ancienne, après
    application des filtres. `since_id` permet de ne récupérer que les nouvelles
    entrées depuis un identifiant donné (rafraîchissement incrémental)."""
    with _lock:
        items = list(_buffer)
    out = []
    for e in reversed(items):
        if e["id"] <= since_id:
            continue
        if charger_id and e["charger_id"] != charger_id:
            continue
        if action and e["action"] != action:
            continue
        if direction and e["direction"] != direction:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


def known_chargers() -> list:
    """Liste des bornes ayant produit au moins une entrée dans le tampon."""
    with _lock:
        return sorted({e["charger_id"] for e in _buffer})


def clear() -> None:
    with _lock:
        _buffer.clear()
