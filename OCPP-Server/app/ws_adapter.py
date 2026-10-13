import starlette.websockets as _starlette_ws
from starlette.websockets import WebSocket, WebSocketDisconnect

# Selon la version de Starlette, un envoi sur un socket déjà fermé lève
# RuntimeError ou une classe dédiée (WebSocketDisconnected, observée en prod
# le 13/10) : on les traite toutes pareil, sans dépendre d'un nom précis.
_CLOSED_SOCKET_ERRORS = tuple(
    c for c in (WebSocketDisconnect, RuntimeError, getattr(_starlette_ws, "WebSocketDisconnected", None)) if c
)


class StarletteWebSocketAdapter:
    """Fait ressembler un WebSocket FastAPI/Starlette à une connexion
    websockets classique (recv/send), telle qu'attendue par la lib `ocpp`."""

    def __init__(self, websocket: WebSocket):
        self._ws = websocket

    async def recv(self) -> str:
        try:
            return await self._ws.receive_text()
        except WebSocketDisconnect:
            raise ConnectionError("WebSocket fermé par le client")

    async def send(self, message: str):
        # Une réponse peut partir sur un socket déjà fermé (rafale de
        # reconnexions : la nouvelle connexion de la borne ferme l'ancienne
        # pendant que celle-ci répond encore, observé le 13/10 entre 00h14 et
        # 00h21). Starlette lève alors WebSocketDisconnect ou RuntimeError
        # ("Cannot call send once a close message has been sent"), ce qui
        # remontait jusqu'à ocpp_endpoint comme une erreur ERROR avec trace
        # complète, et gonflait à tort l'indicateur « Erreurs serveur ». On la
        # convertit en ConnectionError, déjà traitée comme une simple
        # déconnexion (niveau INFO) par ocpp_endpoint.
        try:
            await self._ws.send_text(message)
        except _CLOSED_SOCKET_ERRORS as exc:
            raise ConnectionError(f"WebSocket déjà fermé : {exc}") from exc
