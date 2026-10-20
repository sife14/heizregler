import json
from datetime import datetime, timezone

import pytest

from heizregler import diagnose, einstellungen, parameter
from heizregler.quellen.bedienung import MQTTBedienung, diagnose_json, discovery_nachrichten, parameter_payload


@pytest.fixture
def nachrichten():
    return discovery_nachrichten("heizregler", "homeassistant", "9.9")


def _nach_domain(nachrichten, domain):
    return [p for t, p in nachrichten if t.split("/")[1] == domain]


def test_alle_entitaeten_vorhanden(nachrichten):
    assert len(_nach_domain(nachrichten, "number")) == len(parameter.ZAHLEN)
    assert len(_nach_domain(nachrichten, "switch")) == len(parameter.SCHALTER)
    sensoren = _nach_domain(nachrichten, "sensor") + _nach_domain(nachrichten, "binary_sensor")
    assert {p["unique_id"] for p in sensoren} == {f"heizregler_{k}" for k in diagnose.KEYS}
    assert len(_nach_domain(nachrichten, "select")) == 1
    assert len(_nach_domain(nachrichten, "climate")) == 1
    assert len(_nach_domain(nachrichten, "button")) == 1


def test_topics_ids_und_geraet(nachrichten):
    ids = [p["unique_id"] for _, p in nachrichten]
    assert len(ids) == len(set(ids))
    for topic, p in nachrichten:
        teile = topic.split("/")
        assert teile[0] == "homeassistant" and teile[2] == "heizregler" and teile[4] == "config"
        domain, key = teile[1], teile[3]
        erwartet = "climate.heizregler" if domain == "climate" else f"{domain}.heizregler_{key}"
        assert p["default_entity_id"] == erwartet
        assert p["device"]["identifiers"] == ["heizregler"]
        assert p["device"]["sw_version"] == "9.9"
        assert p["availability_topic"] == "heizregler/status"
        assert "object_id" not in p
        json.dumps(p)


def test_feste_ids(nachrichten):
    ids = {p["default_entity_id"] for _, p in nachrichten}
    assert {"climate.heizregler", "select.heizregler_betriebsart", "button.heizregler_i_zuruecksetzen",
            "number.heizregler_kp", "switch.heizregler_dither_aktiv", "sensor.heizregler_h",
            "binary_sensor.heizregler_fenster"} <= ids


def test_sensor_details(nachrichten):
    nach_id = {p["unique_id"]: p for _, p in nachrichten}
    zustand = nach_id["heizregler_zustand"]
    assert "unit_of_measurement" not in zustand and "state_class" not in zustand
    y = nach_id["heizregler_y"]
    assert y["unit_of_measurement"] == "°C" and y["device_class"] == "temperature"
    assert y["state_topic"] == "heizregler/diagnose" and "value_json.get('y')" in y["value_template"]
    assert nach_id["heizregler_herzschlag"]["entity_category"] == "diagnostic"
    assert "entity_category" not in nach_id["heizregler_y"]
    fenster = nach_id["heizregler_fenster"]
    assert fenster["payload_on"] == "ON" and "'ON' if v else 'OFF'" in fenster["value_template"]
    kp = nach_id["heizregler_kp"]
    assert kp["min"] == 0.05 and kp["max"] == 5.0 and kp["mode"] == "box" and kp["entity_category"] == "config"
    klima = nach_id["heizregler_klima"]
    assert klima["name"] is None and klima["modes"] == ["off", "heat"]


def test_diagnose_json():
    t = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    d = json.loads(diagnose_json({"herzschlag": t, "grund": "x" * 300, "e": float("nan"), "fenster": True}))
    assert d["herzschlag"] == "2026-10-20T12:00:00+00:00"
    assert len(d["grund"]) == 255 and d["e"] is None and d["fenster"] is True


def test_parameter_payload():
    assert parameter_payload(True) == "ON" and parameter_payload(False) == "OFF"
    assert parameter_payload(40) == "40.0"


async def test_befehle_werden_geroutet():
    aufrufe = []

    async def ba(v):
        aufrufe.append(("betriebsart", v))

    async def sw(v):
        aufrufe.append(("sollwert", v))

    async def pa(k, v):
        aufrufe.append(("parameter", k, v))

    async def ta(n):
        aufrufe.append(("taste", n))

    b = MQTTBedienung(einstellungen.laden(), ba, sw, pa, ta)
    for topic, text in [
        ("heizregler/parameter/kp/set", "9"),
        ("heizregler/parameter/dither_aktiv/set", "ON"),
        ("heizregler/parameter/gibtsnicht/set", "1"),
        ("heizregler/parameter/kp/set", "abc"),
        ("heizregler/betriebsart/set", "pi"),
        ("heizregler/betriebsart/set", "quatsch"),
        ("heizregler/klima/modus/set", "off"),
        ("heizregler/klima/modus/set", "heat"),
        ("heizregler/sollwert/set", "30"),
        ("heizregler/taste/i_zuruecksetzen", "PRESS"),
    ]:
        await b._verarbeiten(None, topic, text)
    assert aufrufe == [
        ("parameter", "kp", 5.0),
        ("parameter", "dither_aktiv", True),
        ("betriebsart", "pi"),
        ("betriebsart", "aus"),
        ("betriebsart", "pi"),
        ("sollwert", parameter.SOLLWERT_MAX),
        ("taste", "i_zuruecksetzen"),
    ]
