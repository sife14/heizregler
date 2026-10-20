"""Reine Kernbausteine: PI, Stellglied, Dither, Schätzer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from heizregler import parameter
from heizregler.anregung import FOLGE, dither
from heizregler.regler.basis import ReglerEingang
from heizregler.regler.pi import PIRegler
from heizregler.schaetzer import Erholung, Zentrale, im_zeitfenster
from heizregler.stellglied import Stellglied

from .simulator import Strecke

T0 = datetime(2026, 10, 21, 8, 0, tzinfo=timezone.utc)


def eingang(y: float, w: float = 21.0, frei: bool = True, h_max: float = 1.0, **p: float) -> ReglerEingang:
    par = parameter.vorgaben() | p
    return ReglerEingang(zeit=T0, dt_s=60, y=y, w=w, parameter=par, integration_frei=frei, h_max=h_max)


def test_prbs_wie_heizexp():
    assert FOLGE == (1, 1, 1, 1, 1, 0, 0, 0, 1, 1, 0, 1, 1, 1, 0, 1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 1, 0, 1, 1, 0, 0)


def test_dither_aus_und_erlaubt():
    p = parameter.vorgaben()
    assert dither(T0, p, True)[0] == 0.0
    p["dither_aktiv"] = True
    assert abs(dither(T0, p, True)[0]) == pytest.approx(p["dither_amplitude"])
    assert dither(T0, p, False)[0] == 0.0


def test_pi_eingeschwungen_p_ist_regelabweichung():
    r = PIRegler()
    r.stossfrei_initialisieren(0.3, eingang(21.0))
    a = r.schritt(eingang(21.0))
    assert a.h == pytest.approx(0.3)
    a = r.schritt(eingang(20.5))
    assert a.diagnose["p"] == pytest.approx(0.8 * 0.5, rel=1e-3)


def test_pi_sollwertsprung_gedaempft():
    r = PIRegler()
    r.stossfrei_initialisieren(0.3, eingang(21.0))
    a = r.schritt(eingang(21.0, w=22.0))
    # b = 0.5: nur halber Sprung im P-Anteil
    assert a.diagnose["p"] == pytest.approx(0.8 * 0.5, abs=0.02)


def test_pi_integriert_nicht_in_saettigung():
    r = PIRegler()
    r.stossfrei_initialisieren(1.0, eingang(18.0))
    i0 = r.zustand()["i"]
    for _ in range(30):
        a = r.schritt(eingang(18.0))
    assert a.h == 1.0
    assert r.zustand()["i"] == pytest.approx(i0, abs=1e-9)


def test_pi_einfrieren():
    r = PIRegler()
    r.stossfrei_initialisieren(0.2, eingang(21.0))
    for _ in range(30):
        r.schritt(eingang(20.0, frei=False))
    assert r.zustand()["i"] == pytest.approx(0.2 - 0.0, abs=1e-9)


def test_stellglied_kennlinie_und_senden():
    s = Stellglied((0.0, 0.5, 1.0), (0, 64, 255))
    assert s.ventil(0.25, 255) == 32
    assert s.ventil(1.0, 200) == 200
    assert s.h_aus_ventil(64) == pytest.approx(0.5)
    p = parameter.vorgaben()
    t = T0
    assert Stellglied.senden(t, 100, None, None, p, False)
    assert not Stellglied.senden(t + timedelta(minutes=5), 130, 100, t, p, False)  # Stelltakt
    assert Stellglied.senden(t + timedelta(minutes=5), 0, 100, t, p, True)  # erzwungen
    assert not Stellglied.senden(t + timedelta(minutes=11), 104, 100, t, p, False)  # Mindeständerung
    assert Stellglied.senden(t + timedelta(minutes=11), 0, 4, t, p, False)  # Endlage


def test_zeitfenster_ueber_mitternacht():
    assert im_zeitfenster(datetime(2026, 1, 1, 23, 0), 22, 5)
    assert im_zeitfenster(datetime(2026, 1, 1, 4, 59), 22, 5)
    assert not im_zeitfenster(datetime(2026, 1, 1, 12, 0), 22, 5)


def test_zentrale_erkennung():
    z = Zentrale()
    p = parameter.vorgaben() | {"zentrale_zeitfenster": False}
    mittag = datetime(2026, 1, 1, 12, 0)
    for _ in range(19):
        assert not z.aktualisieren(mittag, 60, 200, 1.0, p)[0]
    assert z.aktualisieren(mittag, 60, 200, 1.0, p)[0]
    assert z.aktualisieren(mittag, 60, 0, None, p)[0]  # Ventil zu: Ergebnis bleibt
    assert not z.aktualisieren(mittag, 60, 200, 8.0, p)[0]


def test_erholung_sagt_rueckkehr_voraus():
    e = Erholung()
    p = parameter.vorgaben()
    e.aktualisieren(21.0, 60, False, p)
    for _ in range(15):  # 15 min lüften: Luft fällt, Speicher kaum
        e.aktualisieren(17.0, 60, True, p)
    assert e.speicher > 20.5
    assert 17.0 < e.y_eff(17.0, p) < e.speicher


def simuliere(minuten: int, strecke: Strecke, r: PIRegler, w: float = 21.0, **p: float) -> list[float]:
    ys = []
    y = strecke.t_luft
    r.stossfrei_initialisieren(0.3, eingang(y, w, **p))
    for _ in range(minuten):
        a = r.schritt(eingang(y, w, **p))
        y = strecke.schritt(a.h)
        ys.append(y)
    return ys


def test_geschlossener_kreis_regelt_aus_ohne_schwingen():
    s = Strecke(t_luft=20.0, t_masse=20.0)
    ys = simuliere(12 * 60, s, PIRegler())
    letzte = ys[-120:]
    assert max(letzte) - min(letzte) < 0.1
    assert sum(letzte) / len(letzte) == pytest.approx(21.0, abs=0.1)
    assert max(ys) < 21.6  # kein großes Überschwingen
