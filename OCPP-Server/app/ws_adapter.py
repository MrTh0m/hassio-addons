from starlette.websockets import WebSocket, WebSocketDisconnect


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
        await self._ws.send_text(message)
