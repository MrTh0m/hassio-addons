"""
Vérifie que les publications MQTT ne font JAMAIS attendre le chemin de
réponse OCPP.

Contexte (incident du 10/10) : le broker MQTT devenait injoignable sans lever
d'erreur immédiate. Chaque publish() attendait alors sa confirmation (10 s par
défaut dans aiomqtt) et un seul MeterValues déclenchait une dizaine de
publications à la suite : la réponse à la borne partait ~40 s trop tard,
au-delà de son MessageTimeout (30 s), et la borne coupait sa connexion
WebSocket en boucle pendant la charge, sans plus aucun relevé enregistré.
_safe_publish() doit donc se contenter de déposer le message dans une file
d'attente, et rendre la main tout de suite, même si le broker ne répond plus.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import asyncio
import pytest

os.environ.setdefault("OCPP_DATA_DIR", "/tmp/test_ocpp")
os.environ.setdefault("OCPP_ADMIN_PASSWORD", "testpass")
os.environ.setdefault("OCPP_SECRET_KEY", "test-secret")

import app.mqtt_bridge as mqtt_bridge


class _HangingClient:
    async def publish(self, topic, payload, retain=True, timeout=None):
        await asyncio.sleep(3600)


class _RecordingClient:
    def __init__(self):
        self.calls = []

    async def publish(self, topic, payload, retain=True, timeout=None):
        self.calls.append((topic, payload, retain, timeout))


class _FailingClient:
    def __init__(self):
        self.attempts = 0

    async def publish(self, topic, payload, retain=True, timeout=None):
        self.attempts += 1
        raise Exception("Operation timed out")


@pytest.fixture(autouse=True)
def _clean_state():
    mqtt_bridge._outbox.clear()
    mqtt_bridge._wakeup = None
    mqtt_bridge._client = None
    yield
    mqtt_bridge._outbox.clear()
    mqtt_bridge._wakeup = None
    mqtt_bridge._client = None


def test_safe_publish_returns_immediately_when_broker_hangs():
    mqtt_bridge._client = _HangingClient()

    async def scenario():
        await asyncio.wait_for(
            mqtt_bridge.publish_connector_state(
                "charger-1", 1, power_w=5900, current_a=27.5, voltage_v=213.0,
                energy_wh=339589, session_energy_wh=1032, session_cost=0.21,
                session_duration_min=11.0,
            ),
            timeout=1,
        )

    asyncio.run(scenario())
    assert len(mqtt_bridge._outbox) == 7


def test_safe_publish_noop_without_client_does_not_queue():
    mqtt_bridge._client = None
    asyncio.run(mqtt_bridge._safe_publish("some/topic", "payload"))
    assert len(mqtt_bridge._outbox) == 0


def test_outbox_is_bounded():
    mqtt_bridge._client = _HangingClient()

    async def scenario():
        for i in range(mqtt_bridge.OUTBOX_MAX + 50):
            await mqtt_bridge._safe_publish(f"t/{i}", "x")

    asyncio.run(scenario())
    assert len(mqtt_bridge._outbox) == mqtt_bridge.OUTBOX_MAX
    assert mqtt_bridge._outbox[-1][0] == f"t/{mqtt_bridge.OUTBOX_MAX + 49}"


def test_publisher_drains_outbox_with_bounded_timeout():
    client = _RecordingClient()
    mqtt_bridge._client = client

    async def scenario():
        wakeup = asyncio.Event()
        mqtt_bridge._wakeup = wakeup
        await mqtt_bridge._safe_publish("a", "1")
        await mqtt_bridge._safe_publish("b", "2", retain=False)
        task = asyncio.create_task(mqtt_bridge._publisher(client, wakeup))
        await asyncio.sleep(0.05)
        await mqtt_bridge._safe_publish("c", "3")
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert [c[:3] for c in client.calls] == [("a", "1", True), ("b", "2", False), ("c", "3", True)]
    assert all(c[3] == mqtt_bridge.PUBLISH_TIMEOUT_S for c in client.calls)
    assert len(mqtt_bridge._outbox) == 0


def test_publisher_gives_up_after_consecutive_failures():
    client = _FailingClient()
    mqtt_bridge._client = client

    async def scenario():
        wakeup = asyncio.Event()
        mqtt_bridge._wakeup = wakeup
        for i in range(10):
            await mqtt_bridge._safe_publish(f"t/{i}", "x")
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(mqtt_bridge._publisher(client, wakeup), timeout=2)

    asyncio.run(scenario())
    assert client.attempts == mqtt_bridge.MAX_CONSECUTIVE_PUBLISH_FAILURES
