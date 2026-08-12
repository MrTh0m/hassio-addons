"""Tests du tampon de logs serveur (app/server_logs.py)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logging
import pytest

os.environ.setdefault("OCPP_DATA_DIR", "/tmp/test_ocpp")
os.environ.setdefault("OCPP_ADMIN_PASSWORD", "testpass")
os.environ.setdefault("OCPP_SECRET_KEY", "test-secret")

from app import server_logs


@pytest.fixture(autouse=True)
def reset_buffer():
    server_logs.clear()
    server_logs.set_capture_access_logs(False)
    yield
    server_logs.clear()
    server_logs.set_capture_access_logs(False)


def _emit(logger_name, level, msg, exc_info=None):
    record = logging.LogRecord(
        name=logger_name, level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=exc_info,
    )
    server_logs._handler.emit(record)


def test_capture_and_retrieve():
    _emit("mqtt-bridge", logging.WARNING, "Connexion MQTT indisponible")
    entries = server_logs.get_entries()
    assert len(entries) == 1
    assert entries[0]["logger"] == "mqtt-bridge"
    assert entries[0]["level"] == "WARNING"
    assert "Connexion MQTT indisponible" in entries[0]["message"]


def test_access_logs_excluded_by_default():
    _emit("uvicorn.access", logging.INFO, "GET /api/chargers 200 OK")
    _emit("ocpp-server", logging.INFO, "Borne connectée")
    entries = server_logs.get_entries()
    assert len(entries) == 1
    assert entries[0]["logger"] == "ocpp-server"


def test_access_logs_included_when_enabled():
    server_logs.set_capture_access_logs(True)
    _emit("uvicorn.access", logging.INFO, "GET /api/chargers 200 OK")
    entries = server_logs.get_entries()
    assert len(entries) == 1
    assert entries[0]["logger"] == "uvicorn.access"


def test_filter_by_logger_and_level():
    _emit("mqtt-bridge", logging.WARNING, "panne broker")
    _emit("ocpp-server", logging.ERROR, "erreur connexion")
    assert len(server_logs.get_entries(logger="mqtt-bridge")) == 1
    assert len(server_logs.get_entries(level="ERROR")) == 1
    assert len(server_logs.get_entries(logger="mqtt-bridge", level="ERROR")) == 0


def test_since_id_incremental_refresh():
    _emit("ocpp-server", logging.INFO, "premier")
    first_id = server_logs.get_entries()[0]["id"]
    _emit("ocpp-server", logging.INFO, "second")
    fresh = server_logs.get_entries(since_id=first_id)
    assert len(fresh) == 1
    assert fresh[0]["message"] == "second"


def test_known_loggers():
    _emit("mqtt-bridge", logging.INFO, "a")
    _emit("ocpp-server", logging.INFO, "b")
    assert server_logs.known_loggers() == ["mqtt-bridge", "ocpp-server"]


def test_clear():
    _emit("ocpp-server", logging.INFO, "test")
    assert len(server_logs.get_entries()) == 1
    server_logs.clear()
    assert server_logs.get_entries() == []


def test_traceback_included_on_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys as _sys
        _emit("ocpp-server", logging.ERROR, "échec", exc_info=_sys.exc_info())
    entries = server_logs.get_entries()
    assert "ValueError: boom" in entries[0]["message"]
    assert "Traceback" in entries[0]["message"]
