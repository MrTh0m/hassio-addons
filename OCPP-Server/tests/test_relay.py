import asyncio
import websockets
from ocpp.v16 import ChargePoint as ChargePoint16
from ocpp.v16 import call


class TestChargePoint(ChargePoint16):
    pass


async def main():
    async with websockets.connect(
        "ws://localhost:8000/ocpp/RELAY-CP-01",
        subprotocols=["ocpp1.6"],
    ) as ws:
        cp = TestChargePoint("RELAY-CP-01", ws)
        task = asyncio.create_task(cp.start())

        boot = await cp.call(call.BootNotification(
            charge_point_vendor="RealVendor", charge_point_model="RealModel"
        ))
        print("BootNotification (via relais) ->", boot)

        start = await cp.call(call.StartTransaction(
            connector_id=1, id_tag="REALTAG", meter_start=2000,
            timestamp="2026-01-01T00:00:00Z",
        ))
        print("StartTransaction (via relais) ->", start)

        await cp.call(call.MeterValues(
            connector_id=1, transaction_id=start.transaction_id,
            meter_value=[{
                "timestamp": "2026-01-01T00:01:00Z",
                "sampled_value": [
                    {"value": "3200", "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
                ],
            }],
        ))
        print("MeterValues (via relais) envoyé")

        task.cancel()


asyncio.run(main())
