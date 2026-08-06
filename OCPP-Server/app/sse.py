"""
Gestionnaire d'événements SSE (Server-Sent Events).

Chaque changement d'état OCPP significatif (connexion/déconnexion borne,
changement de statut connecteur, début/fin de transaction) déclenche un
événement que le client peut recevoir sans polling.

Le canal est un asyncio.Queue par client connecté.
"""
import asyncio
import json
import logging
from typing import AsyncGenerator

logger = logging.getLogger("sse")

# Ensemble des queues actives (une par client SSE connecté)
_subscribers: set[asyncio.Queue] = set()


def sse_notify(event_type: str, data: dict | None = None):
    """Émet un événement à tous les clients SSE connectés.

    Appelable depuis n'importe quel contexte (sync ou async) : on utilise
    put_nowait pour ne pas bloquer.
    """
    payload = json.dumps({"type": event_type, **(data or {})})
    dead = set()
    for q in _subscribers:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.add(q)
    _subscribers.difference_update(dead)


async def sse_stream() -> AsyncGenerator[str, None]:
    """Générateur SSE : chaque client obtient sa propre queue."""
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    _subscribers.add(q)
    try:
        # Ping initial pour confirmer la connexion
        yield "data: {\"type\":\"connected\"}\n\n"
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=25)
                yield f"data: {payload}\n\n"
            except asyncio.TimeoutError:
                # Keepalive : empêche les proxies de couper la connexion
                yield ": keepalive\n\n"
    finally:
        _subscribers.discard(q)
