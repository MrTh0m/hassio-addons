"""Test de GET /api/settings/timezones, qui peuple la liste déroulante de
fuseau horaire dans Réglages → Avancé (remplace l'ancien champ texte libre)."""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from test_api import setup_db, client, token, auth  # noqa: F401


def test_list_timezones_includes_utc_and_paris(client, auth):
    r = client.get("/api/settings/timezones", headers=auth)
    assert r.status_code == 200
    tzs = r.json()["timezones"]
    assert "UTC" in tzs
    assert "Europe/Paris" in tzs
    # Pas d'alias court hérité (ex. CET) : uniquement Région/Ville, plus UTC.
    assert all(tz == "UTC" or "/" in tz for tz in tzs)
    # Triée, pour un select stable et navigable.
    assert tzs[1:] == sorted(tzs[1:])


def test_saved_timezone_must_be_in_the_list(client, auth):
    r = client.get("/api/settings/timezones", headers=auth)
    tzs = r.json()["timezones"]
    r = client.put("/api/settings/timezone", json={"timezone": "Europe/Paris"}, headers=auth)
    assert r.status_code == 200
    assert "Europe/Paris" in tzs
