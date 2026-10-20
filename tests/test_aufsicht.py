"""Aufsicht mit Attrappen für HA, TRV, Bedienung und Protokoll."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from heizregler.aufsicht import Aufsicht
from heizregler.einstellungen import Einstellungen, Zugang
from heizregler.regler.pi import PIRegler
from heizregler.schnittstellen import EntityZustand, SetzErgebnis

T0 = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)  # 12:00 Ortszeit, außerhalb des Nachtfensters

ENT = {
    "raum": "sensor.raum",
    "zuleitung": "sensor.zul",
    "aussen": "sensor.aussen",
    "fenster": "binary_sensor.fenster",
    "trv": "climate.trv",
    "ventil": "number.ventil",
    "trv_modus": "select.modus",
    "experiment_status": "sensor.exp",
    "konkurrenten": ["climate.bt"],
}


class Uhr:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t


class FakeHA:
    def __init__(self, uhr: Uhr) -> None:
        self.uhr = uhr
        self.verbunden = True
        self.s: dict[str, EntityZustand] = {}
        self.setze("sensor.raum", "20.5")
        self.setze("sensor.zul", "30")
        self.setze("binary_sensor.fenster", "off")
        self.setze("number.ventil", "0")
        self.setze("select.modus", "2")
        self.setze("climate.trv", "auto", temperature=20.5)
        self.setze("sensor.exp", "aus")

    def setze(self, eid: str, state: str, **attr: Any) -> None:
        t = self.uhr()
        self.s[eid] = EntityZustand(state, attr, t, t, t)

    def zustand(self, eid: str) -> EntityZustand | None:
        return self.s.get(eid)

    async def dienst(self, *a: Any, **k: Any) -> None:
        pass

    async def warte_auf(self, *a: Any, **k: Any) -> None:
        return None


class FakeTRV:
    def __init__(self, ha: FakeHA) -> None:
        self.ha = ha
        self.befehle: list[int] = []
        self.uebernahme_ok = True
        self.setzen_ok = True
        self.rueckgaben = 0

    async def ventil_setzen(self, wert: int) -> SetzErgebnis:
        self.befehle.append(wert)
        if self.setzen_ok:
            self.ha.setze("number.ventil", str(wert))
        return SetzErgebnis(self.setzen_ok, wert, 1, 2.0 if self.setzen_ok else None, self.ha.uhr() if self.setzen_ok else None)

    async def uebernehmen(self) -> tuple[bool, str]:
        if self.uebernahme_ok:
            self.ha.setze("select.modus", "1")
        return self.uebernahme_ok, "test"

    async def zurueckgeben(self) -> tuple[bool, str]:
        self.rueckgaben += 1
        self.ha.setze("select.modus", "2")
        return True, "test"


class FakeBedienung:
    def __init__(self) -> None:
        self.diagnose: list[dict[str, Any]] = []

    async def diagnose_senden(self, werte: dict[str, Any]) -> None:
        self.diagnose.append(werte)

    async def parameter_senden(self, werte: dict[str, Any]) -> None:
        pass

    async def bedienung_senden(self, *a: Any) -> None:
        pass


class FakeProtokoll:
    def __init__(self) -> None:
        self.saetze: list[dict[str, Any]] = []

    async def schreiben(self, zeit: datetime, felder: dict[str, Any], tags: Any = None) -> None:
        self.saetze.append(felder)


@pytest.fixture
def umgebung(tmp_path: Path):
    uhr = Uhr()
    ha = FakeHA(uhr)
    trv = FakeTRV(ha)
    einst = Einstellungen(
        zugang=Zugang("", "", "", 0, "", "", "", "", "", ""),
        takt_s=60, zustand_datei=tmp_path / "z.json", mqtt_basis="heizregler", discovery_praefix="homeassistant",
        entitaeten=ENT, trv={"modus_automatisch": "2", "modus_manuell": "1"},
        kennlinie_h=(0.0, 1.0), kennlinie_ventil=(0, 255),
    )
    a = Aufsicht(einst, ha, trv, FakeBedienung(), FakeProtokoll(), PIRegler(), uhr=uhr)
    return a, ha, trv, uhr


async def takte(a: Aufsicht, ha: FakeHA, uhr: Uhr, n: int) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for _ in range(n):
        uhr.t += timedelta(minutes=1)
        # Fühler meldet sich jede Minute
        z = ha.s["sensor.raum"]
        ha.s["sensor.raum"] = replace(z, last_reported=uhr.t)
        d = await a.takt()
        await asyncio.sleep(0)  # Stell-Task laufen lassen
    return d


async def test_aus_bleibt_finger_weg(umgebung):
    a, ha, trv, uhr = umgebung
    d = await takte(a, ha, uhr, 3)
    assert d["zustand"] == "aus" and trv.befehle == []


async def test_uebernahme_und_regeln(umgebung):
    a, ha, trv, uhr = umgebung
    await a.betriebsart_setzen("pi")
    d = await takte(a, ha, uhr, 2)
    assert d["zustand"] == "aktiv"
    assert trv.befehle and trv.befehle[0] > 0  # 0,5 K zu kalt -> öffnet
    assert d["gueltig"] is True


async def test_fenster_schliesst_sofort_und_friert_ein(umgebung):
    a, ha, trv, uhr = umgebung
    await a.betriebsart_setzen("pi")
    await takte(a, ha, uhr, 12)
    i0 = a.regler.zustand()["i"]
    ha.setze("binary_sensor.fenster", "on")
    d = await takte(a, ha, uhr, 1)
    assert trv.befehle[-1] == 0 and d["fenster"] and not d["integration_frei"]
    ha.setze("sensor.raum", "17.0")
    await takte(a, ha, uhr, 5)
    assert a.regler.zustand()["i"] == pytest.approx(i0)
    ha.setze("binary_sensor.fenster", "off")
    d = await takte(a, ha, uhr, 1)
    assert d["nachlauf"] and d["h"] <= a.param["h_max_nachlauf"] + 1e-9
    assert d["y_eff"] > 17.0  # Erholung vorhergesagt


async def test_experiment_pausiert(umgebung):
    a, ha, trv, uhr = umgebung
    await a.betriebsart_setzen("pi")
    await takte(a, ha, uhr, 2)
    n = len(trv.befehle)
    ha.setze("sensor.exp", "läuft")
    d = await takte(a, ha, uhr, 15)
    assert d["zustand"] == "pause_experiment" and len(trv.befehle) == n
    ha.setze("sensor.exp", "beendet")
    ha.setze("select.modus", "2")
    d = await takte(a, ha, uhr, 1)
    assert d["zustand"] == "aktiv"


async def test_extern_zurueckgestellt_wartet(umgebung):
    a, ha, trv, uhr = umgebung
    await a.betriebsart_setzen("pi")
    await takte(a, ha, uhr, 2)
    ha.setze("select.modus", "2")  # Watchdog
    d = await takte(a, ha, uhr, 1)
    assert d["zustand"] == "stoerung"
    d = await takte(a, ha, uhr, 10)
    assert d["zustand"] == "stoerung"
    d = await takte(a, ha, uhr, 21)
    assert d["zustand"] == "aktiv"


async def test_konkurrent_verhindert_uebernahme(umgebung):
    a, ha, trv, uhr = umgebung
    ha.setze("climate.bt", "heat")
    await a.betriebsart_setzen("pi")
    d = await takte(a, ha, uhr, 1)
    assert d["zustand"] == "stoerung" and "Anderer Regler" in d["grund"]


async def test_betriebsart_aus_gibt_zurueck(umgebung):
    a, ha, trv, uhr = umgebung
    await a.betriebsart_setzen("pi")
    await takte(a, ha, uhr, 2)
    await a.betriebsart_setzen("aus")
    d = await takte(a, ha, uhr, 1)
    assert trv.rueckgaben == 1 and d["zustand"] == "aus"


async def test_tmax_erzwingt_zu(umgebung):
    a, ha, trv, uhr = umgebung
    await a.betriebsart_setzen("pi")
    await takte(a, ha, uhr, 2)
    ha.setze("sensor.raum", "25.0")
    await takte(a, ha, uhr, 1)
    assert trv.befehle[-1] == 0


async def test_fuehler_weg_gibt_zurueck(umgebung):
    a, ha, trv, uhr = umgebung
    await a.betriebsart_setzen("pi")
    await takte(a, ha, uhr, 2)
    alt = ha.s["sensor.raum"]
    for _ in range(181):
        uhr.t += timedelta(minutes=1)
        ha.s["sensor.raum"] = alt  # meldet sich nicht mehr
        d = await a.takt()
    assert trv.rueckgaben == 1 and d["zustand"] == "stoerung"


async def test_neustart_setzt_reglerzustand_fort(umgebung, tmp_path):
    a, ha, trv, uhr = umgebung
    await a.betriebsart_setzen("pi")
    await takte(a, ha, uhr, 30)
    i_alt = a.regler.zustand()["i"]
    b = Aufsicht(a.einst, ha, trv, FakeBedienung(), FakeProtokoll(), PIRegler(), uhr=uhr)
    b.laden()
    assert b.betriebsart == "pi"
    d = await takte(b, ha, uhr, 1)
    assert d["zustand"] == "aktiv"
    # fortgesetzt statt stoßfrei neu initialisiert; ein Takt Integration (~0,01) dazu
    assert b.regler.zustand()["i"] == pytest.approx(i_alt, abs=0.02)


async def test_zentrale_nachts_ventil_gehalten(umgebung):
    a, ha, trv, uhr = umgebung
    uhr.t = datetime(2026, 10, 21, 19, 50, tzinfo=timezone.utc)  # 21:50 Ortszeit
    await a.betriebsart_setzen("pi")
    await takte(a, ha, uhr, 2)
    n = len(trv.befehle)
    await takte(a, ha, uhr, 10)  # jetzt nach 22:00 Ortszeit
    ha.setze("sensor.raum", "19.5")  # kühlt aus, Zentrale liefert nichts
    d = await takte(a, ha, uhr, 30)
    assert d["zentrale_aus"] and "gehalten" in d["grund"]
    assert len(trv.befehle) == n
    ha.setze("binary_sensor.fenster", "on")  # Sicherheit geht vor
    await takte(a, ha, uhr, 1)
    assert trv.befehle[-1] == 0
