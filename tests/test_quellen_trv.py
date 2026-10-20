import asyncio
import dataclasses
import json
from datetime import datetime, timezone

import pytest

from heizregler import einstellungen
from heizregler.quellen.trv import TRVStellantrieb
from heizregler.schnittstellen import EntityZustand


def _z(state, **attrs):
    t = datetime.now(timezone.utc)
    return EntityZustand(state, attrs, t, t, t)


class FakeHA:
    """Simuliert HA + TRV: Dienste ändern Zustände, mqtt.publish(/get) löst eine Meldung aus."""

    def __init__(self):
        self.verbunden = True
        self.zustaende = {}
        self.aufrufe = []
        self.trigger = []
        self.antworten_ab_get = 1  # ab dem wievielten /get das TRV antwortet (0 = nie)
        self.gets = 0
        self.ventil = 0

    def zustand(self, eid):
        return self.zustaende.get(eid)

    def setzen(self, eid, state, **attrs):
        alt = self.zustaende.get(eid)
        a = dict(alt.attributes) if alt else {}
        a.update(attrs)
        self.zustaende[eid] = _z(state, **a)

    def melden(self, payload):
        for _, cb in self.trigger:
            cb({"trigger": {"payload": json.dumps(payload), "payload_json": payload}})

    def trigger_registrieren(self, trigger, rueckruf):
        self.trigger.append((trigger, rueckruf))

    async def dienst(self, domain, service, data=None, target=None):
        eid = (target or {}).get("entity_id")
        self.aufrufe.append((domain, service, data, eid))
        if (domain, service) == ("number", "set_value"):
            self.ventil = int(data["value"])
        elif (domain, service) == ("mqtt", "publish"):
            self.gets += 1
            if self.antworten_ab_get and self.gets >= self.antworten_ab_get:
                asyncio.get_running_loop().call_later(0.02, self.melden, {"valve_position": self.ventil})
        elif (domain, service) == ("climate", "set_temperature"):
            z = self.zustaende[eid]
            self.setzen(eid, z.state, temperature=data["temperature"])
        elif (domain, service) == ("climate", "set_hvac_mode"):
            self.setzen(eid, data["hvac_mode"])
        elif (domain, service) == ("select", "select_option"):
            self.setzen(eid, data["option"])

    async def warte_auf(self, eid, bedingung, timeout_s):
        frist = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < frist:
            z = self.zustaende.get(eid)
            if z is not None and bedingung(z):
                return z
            await asyncio.sleep(0.005)
        return None


@pytest.fixture
def cfg():
    e = einstellungen.laden()
    return dataclasses.replace(e, trv={**e.trv, "bestaetigung_timeout_s": 0.2})


@pytest.fixture
def ha():
    h = FakeHA()
    h.setzen("climate.thermostat2", "heat", temperature=21.0, min_temp=8, hvac_modes=["off", "auto", "heat"])
    h.setzen("select.thermostat2_trv_mode", "2")
    h.setzen("number.thermostat2_valve_position", "0")
    return h


def _trv(ha, cfg):
    return TRVStellantrieb(ha, cfg, wartezeit_s=0.1)


def test_trigger_auf_z2m_topic(ha, cfg):
    _trv(ha, cfg)
    assert ha.trigger[0][0] == {"trigger": "mqtt", "topic": "zigbee2mqtt/thermostat2"}


async def test_ventil_bestaetigt_durch_neue_meldung(ha, cfg):
    trv = _trv(ha, cfg)
    erg = await trv.ventil_setzen(128)
    assert erg.ok and erg.wert == 128 and erg.versuche == 1
    assert erg.latenz_s is not None and erg.latenz_s > 0
    assert ("mqtt", "publish", {"topic": "zigbee2mqtt/thermostat2/get", "payload": '{"valve_position": ""}'}, None) in ha.aufrufe


async def test_alte_meldung_mit_zielwert_bestaetigt_nicht(ha, cfg):
    trv = _trv(ha, cfg)
    ha.melden({"valve_position": 100})  # vor dem Befehl (Cache-Fall)
    ha.antworten_ab_get = 0
    erg = await trv.ventil_setzen(100)
    assert not erg.ok and erg.versuche == 3
    assert sum(1 for a in ha.aufrufe if a[:2] == ("number", "set_value")) == 3


async def test_wiederholung_bis_bestaetigung(ha, cfg):
    trv = _trv(ha, cfg)
    ha.antworten_ab_get = 2
    erg = await trv.ventil_setzen(64)
    assert erg.ok and erg.versuche == 2


async def test_uebernahme_verweigert_bei_aktivem_konkurrenten(ha, cfg):
    ha.setzen("climate.thermostat_bt", "heat")
    trv = _trv(ha, cfg)
    ok, grund = await trv.uebernehmen()
    assert not ok and "thermostat_bt" in grund
    assert ha.aufrufe == []


async def test_uebernahme_normal(ha, cfg):
    ha.setzen("climate.thermostat_bt", "unavailable")
    trv = _trv(ha, cfg)
    ok, grund = await trv.uebernehmen()
    assert ok, grund
    assert trv.zustand()["sicherung"]["sollwert"] == 21.0
    assert trv.zustand()["sicherung"]["hvac"] == "heat"
    assert ha.zustand("climate.thermostat2").attributes["temperature"] == 8
    assert ha.zustand("climate.thermostat2").state == "auto"
    assert ha.zustand("select.thermostat2_trv_mode").state == "1"
    reihenfolge = [a[:2] for a in ha.aufrufe]
    assert reihenfolge.index(("climate", "set_hvac_mode")) < reihenfolge.index(("select", "select_option"))


async def test_uebernahme_nach_neustart_behaelt_sicherung(ha, cfg):
    ha.setzen("select.thermostat2_trv_mode", "1")
    ha.setzen("climate.thermostat2", "auto", temperature=8)
    trv = _trv(ha, cfg)
    trv.zustand_laden({"sicherung": {"sollwert": 20.0, "hvac": "auto"}})
    ok, grund = await trv.uebernehmen()
    assert ok and "Neustart" in grund
    assert trv.zustand()["sicherung"]["sollwert"] == 20.0


async def test_uebernahme_fehlschlag_gibt_zurueck(ha, cfg):
    trv = _trv(ha, cfg)
    original = ha.dienst

    async def modus_klemmt(domain, service, data=None, target=None):
        if (domain, service, (data or {}).get("option")) == ("select", "select_option", "1"):
            ha.aufrufe.append((domain, service, data, (target or {}).get("entity_id")))
            return
        await original(domain, service, data, target)

    ha.dienst = modus_klemmt
    ok, grund = await trv.uebernehmen()
    assert not ok and "nicht übernommen" in grund
    assert ha.zustand("select.thermostat2_trv_mode").state == "2"
    assert ha.zustand("climate.thermostat2").attributes["temperature"] == 21.0


async def test_rueckgabe_ohne_boost(ha, cfg):
    trv = _trv(ha, cfg)
    assert (await trv.uebernehmen())[0]
    ok, meldung = await trv.zurueckgeben()
    assert ok, meldung
    assert ha.zustand("select.thermostat2_trv_mode").state == "2"
    assert ha.zustand("climate.thermostat2").state == "auto"  # nicht "heat" (Boost)
    assert ha.zustand("climate.thermostat2").attributes["temperature"] == 21.0
    assert trv.zustand()["sicherung"] is None


async def test_rueckgabe_ohne_sicherung_nutzt_vorgabe(ha, cfg):
    trv = _trv(ha, cfg)
    ok, meldung = await trv.zurueckgeben()
    assert ok and "kein gesicherter Sollwert" in meldung
    assert ha.zustand("climate.thermostat2").attributes["temperature"] == cfg.trv["sollwert_rueckgabe"]
