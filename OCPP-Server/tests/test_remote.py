import asyncio
import httpx
import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as ChargePoint16
from ocpp.v16 import call, call_result
from ocpp.v16.enums import RemoteStartStopStatus, ConfigurationStatus


class TestChargePoint(ChargePoint16):
    @on("RemoteStartTransaction")
    async def on_remote_start(self, connector_id, id_tag, **kwargs):
        print(f"[borne] reçu RemoteStartTransaction connector={connector_id} idTag={id_tag}")
        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    @on("RemoteStopTransaction")
    async def on_remote_stop(self, transaction_id, **kwargs):
        print(f"[borne] reçu RemoteStopTransaction transaction={transaction_id}")
        return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)

    @on("GetConfiguration")
    async def on_get_configuration(self, key=None, **kwargs):
        print("[borne] reçu GetConfiguration")
        return call_result.GetConfiguration(configuration_key=[
            {"key": "HeartbeatInterval", "readonly": False, "value": "300"},
            {"key": "NumberOfConnectors", "readonly": True, "value": "1"},
        ])

    @on("ChangeConfiguration")
    async def on_change_configuration(self, key, value, **kwargs):
        print(f"[borne] reçu ChangeConfiguration {key}={value}")
        return call_result.ChangeConfiguration(status=ConfigurationStatus.accepted)


async def api_calls():
    await asyncio.sleep(1)  # laisse la borne finir son BootNotification
    async with httpx.AsyncClient() as client:
        login = await client.post(
            "http://localhost:8000/api/auth/login",
            data={"username": "admin", "password": "admin"},
        )
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        r = await client.post(
            "http://localhost:8000/api/chargers/TEST-CP-02/connectors/1/start",
            json={"id_tag": "USERTAG"}, headers=headers,
        )
        print("[api] start ->", r.status_code, r.json())

        r = await client.get(
            "http://localhost:8000/api/chargers/TEST-CP-02/config", headers=headers,
        )
        print("[api] get config ->", r.status_code, r.json())

        r = await client.put(
            "http://localhost:8000/api/chargers/TEST-CP-02/config/HeartbeatInterval",
            json={"value": "60"}, headers=headers,
        )
        print("[api] set config ->", r.status_code, r.json())


async def main():
    async with websockets.connect(
        "ws://localhost:8000/ocpp/TEST-CP-02",
        subprotocols=["ocpp1.6"],
    ) as ws:
        cp = TestChargePoint("TEST-CP-02", ws)
        serve_task = asyncio.create_task(cp.start())

        await cp.call(call.BootNotification(
            charge_point_vendor="TestVendor", charge_point_model="TestModel"
        ))

        await api_calls()
        await asyncio.sleep(0.5)
        serve_task.cancel()


asyncio.run(main())
