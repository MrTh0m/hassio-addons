import asyncio
from ocpp.routing import on
from ocpp.v16 import ChargePoint as ChargePoint16
from ocpp.v16 import call_result
from ocpp.v16.enums import RegistrationStatus, AuthorizationStatus
import websockets


class OfficialChargePoint(ChargePoint16):
    @on("BootNotification")
    async def on_boot(self, **kwargs):
        print("[serveur officiel] BootNotification reçu", kwargs)
        return call_result.BootNotification(
            current_time="2026-01-01T00:00:00Z", interval=300,
            status=RegistrationStatus.accepted,
        )

    @on("StatusNotification")
    async def on_status(self, **kwargs):
        return call_result.StatusNotification()

    @on("StartTransaction")
    async def on_start(self, **kwargs):
        return call_result.StartTransaction(
            transaction_id=42, id_tag_info={"status": AuthorizationStatus.accepted}
        )

    @on("MeterValues")
    async def on_meter(self, **kwargs):
        return call_result.MeterValues()

    @on("StopTransaction")
    async def on_stop(self, **kwargs):
        return call_result.StopTransaction(id_tag_info={"status": AuthorizationStatus.accepted})

    @on("Heartbeat")
    async def on_heartbeat(self, **kwargs):
        return call_result.Heartbeat(current_time="2026-01-01T00:00:00Z")


async def handler(websocket):
    path = websocket.request.path if hasattr(websocket, "request") else websocket.path
    cp_id = path.strip("/").split("/")[-1]
    cp = OfficialChargePoint(cp_id, websocket)
    await cp.start()


async def main():
    async with websockets.serve(handler, "0.0.0.0", 9999, subprotocols=["ocpp1.6"]):
        await asyncio.Future()


asyncio.run(main())
