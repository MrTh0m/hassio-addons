import asyncio
import websockets
from ocpp.v16 import ChargePoint as ChargePoint16
from ocpp.v16 import call


class TestChargePoint(ChargePoint16):
    pass


async def main():
    async with websockets.connect(
        "ws://localhost:8000/ocpp/TEST-CP-01",
        subprotocols=["ocpp1.6"],
    ) as ws:
        cp = TestChargePoint("TEST-CP-01", ws)
        task = asyncio.create_task(cp.start())

        boot = await cp.call(call.BootNotification(
            charge_point_vendor="TestVendor", charge_point_model="TestModel"
        ))
        print("BootNotification ->", boot)

        hb = await cp.call(call.Heartbeat())
        print("Heartbeat ->", hb)

        await cp.call(call.StatusNotification(
            connector_id=1, error_code="NoError", status="Available"
        ))
        print("StatusNotification OK")

        start = await cp.call(call.StartTransaction(
            connector_id=1, id_tag="TESTTAG", meter_start=1000,
            timestamp="2026-01-01T00:00:00Z",
        ))
        print("StartTransaction ->", start)
        txn_id = start.transaction_id

        await cp.call(call.MeterValues(
            connector_id=1, transaction_id=txn_id,
            meter_value=[{
                "timestamp": "2026-01-01T00:01:00Z",
                "sampled_value": [
                    {"value": "1500", "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
                    {"value": "7400", "measurand": "Power.Active.Import", "unit": "W"},
                ],
            }],
        ))
        print("MeterValues OK")

        stop = await cp.call(call.StopTransaction(
            transaction_id=txn_id, meter_stop=1500, timestamp="2026-01-01T00:05:00Z",
        ))
        print("StopTransaction ->", stop)

        task.cancel()


asyncio.run(main())
