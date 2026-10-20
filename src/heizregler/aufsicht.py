"""Betriebsführung und Sicherheit. Bleibt gleich, wenn der Regler getauscht wird.

Zustände:
    aus               Betriebsart aus, TRV regelt selbst
    pause_experiment  heizexp läuft, Finger weg vom TRV
    aktiv             Regler stellt das Ventil
    stoerung          Übernahme gescheitert/TRV extern zurückgestellt/Fühler weg;
                      neuer Versuch nach WIEDERHOLUNG
    ha_getrennt       keine Verbindung zu HA, nichts tun

Beim Beenden des Dienstes wird das TRV nicht zurückgegeben, damit ein Neustart
stoßfrei ist. Bleibt der Dienst weg, stellt der Watchdog in HA nach 5 min auf
Automatik zurück.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import __version__, parameter, zustand
from .anregung import dither
from .einstellungen import Einstellungen
from .messung import Messung, erfassen
from .regler.basis import Regler, ReglerEingang
from .schaetzer import Erholung, Zentrale
from .schnittstellen import HA, Bedienung, Protokoll, SetzErgebnis, Stellantrieb
from .stellglied import Stellglied

log = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Berlin")
WIEDERHOLUNG = timedelta(minutes=30)
RUECKGABE_WIEDERHOLUNG = timedelta(minutes=5)
FEHLGESCHLAGEN = -1  # Merker für „letzter Befehl unbestätigt“, erzwingt neuen Versuch nach Stelltakt


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


def _begrenzt(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


class Aufsicht:
    def __init__(
        self,
        einst: Einstellungen,
        ha: HA,
        antrieb: Stellantrieb,
        bedienung: Bedienung,
        protokoll: Protokoll,
        regler: Regler,
        uhr: Callable[[], datetime] = _jetzt,
    ) -> None:
        self.einst = einst
        self.ha = ha
        self.antrieb = antrieb
        self.bedienung = bedienung
        self.protokoll = protokoll
        self.regler = regler
        self.uhr = uhr
        self.stellglied = Stellglied(einst.kennlinie_h, einst.kennlinie_ventil)
        self.erholung = Erholung()
        self.zentrale = Zentrale()

        self.param: dict[str, float | bool] = parameter.vorgaben()
        self.betriebsart = "aus"
        self.sollwert = parameter.SOLLWERT_VORGABE
        self.zustand = "aus"
        self.grund = "Start"
        self.uebernommen = False
        self.naechster_versuch: datetime | None = None
        self.rueckgabe_faellig: datetime | None = None
        self.regler_geladen = False

        self.letzte_zeit: datetime | None = None
        self.fenster_war_offen = False
        self.nachlauf_bis: datetime | None = None
        self.vorlauf_ueber: float | None = None
        self.h_wirksam: float | None = None

        self.befehl: int | None = None
        self.befehl_zeit: datetime | None = None
        self.bestaetigt_ok = True
        self.bestaetigt_um: datetime | None = None
        self.latenz_s: float | None = None
        self.fehlversuche = 0
        self.befehle = 0
        self._stell_task: asyncio.Task[None] | None = None
        self.wecker = asyncio.Event()

    # ------------------------------------------------------------------ Zustand
    def laden(self) -> None:
        d = zustand.laden(self.einst.zustand_datei)
        for k, v in d.get("parameter", {}).items():
            try:
                self.param[k] = parameter.begrenzen(k, v)
            except KeyError:
                log.warning("Unbekannter gespeicherter Parameter %s ignoriert", k)
        if d.get("betriebsart") in parameter.BETRIEBSARTEN:
            self.betriebsart = d["betriebsart"]
        self.sollwert = _begrenzt(float(d.get("sollwert", self.sollwert)), parameter.SOLLWERT_MIN, parameter.SOLLWERT_MAX)
        self.uebernommen = bool(d.get("uebernommen", False))
        self.fehlversuche = int(d.get("fehlversuche", 0))
        self.befehle = int(d.get("befehle", 0))
        if "regler" in d and d.get("regler_name") == self.regler.name:
            self.regler.zustand_laden(d["regler"])
            self.regler_geladen = True
        self.erholung.zustand_laden(d.get("erholung", {}))
        if hasattr(self.antrieb, "zustand_laden"):
            self.antrieb.zustand_laden(d.get("antrieb", {}))  # type: ignore[attr-defined]

    def speichern(self) -> None:
        d: dict[str, Any] = {
            "parameter": self.param,
            "betriebsart": self.betriebsart,
            "sollwert": self.sollwert,
            "uebernommen": self.uebernommen,
            "fehlversuche": self.fehlversuche,
            "befehle": self.befehle,
            "regler_name": self.regler.name,
            "regler": self.regler.zustand(),
            "erholung": self.erholung.zustand(),
        }
        if hasattr(self.antrieb, "zustand"):
            d["antrieb"] = self.antrieb.zustand()  # type: ignore[attr-defined]
        try:
            zustand.speichern(self.einst.zustand_datei, d)
        except OSError as fehler:
            log.error("Zustand nicht gespeichert: %s", fehler)

    # ---------------------------------------------------------------- Rückrufe
    async def betriebsart_setzen(self, wert: str) -> None:
        if wert not in parameter.BETRIEBSARTEN:
            log.warning("Unbekannte Betriebsart %r ignoriert", wert)
            return
        log.info("Betriebsart %s -> %s", self.betriebsart, wert)
        self.betriebsart = wert
        self.naechster_versuch = None
        self.speichern()
        await self._bedienung_senden()
        self.wecker.set()

    async def sollwert_setzen(self, wert: float) -> None:
        self.sollwert = _begrenzt(float(wert), parameter.SOLLWERT_MIN, parameter.SOLLWERT_MAX)
        self.speichern()
        await self._bedienung_senden()

    async def parameter_setzen(self, key: str, wert: float | bool) -> None:
        try:
            self.param[key] = parameter.begrenzen(key, wert)
        except KeyError:
            log.warning("Unbekannter Parameter %s", key)
            return
        log.info("Parameter %s = %s", key, self.param[key])
        self.speichern()
        await self.bedienung.parameter_senden({key: self.param[key]})

    async def taste(self, name: str) -> None:
        if name == "i_zuruecksetzen":
            log.info("I-Anteil zurückgesetzt")
            self.regler.zuruecksetzen()
            self.speichern()
            self.wecker.set()

    async def _bedienung_senden(self) -> None:
        if self.betriebsart == "aus":
            aktion = "off"
        else:
            aktion = "heating" if self.zustand == "aktiv" and (self.befehl or 0) > 0 else "idle"
        await self.bedienung.bedienung_senden(self.betriebsart, self.sollwert, aktion)

    async def start(self) -> None:
        """Erste Meldung an HA nach dem Verbinden."""
        await self.bedienung.parameter_senden(dict(self.param))
        await self._bedienung_senden()

    # -------------------------------------------------------------------- Takt
    async def takt(self) -> dict[str, Any]:
        jetzt = self.uhr()
        dt = self.einst.takt_s if self.letzte_zeit is None else (jetzt - self.letzte_zeit).total_seconds()
        dt = _begrenzt(dt, 1.0, 600.0)
        self.letzte_zeit = jetzt
        auffrischen = getattr(self.ha, "auffrischen", None)  # last_reported per REST (nur echtes HA)
        if auffrischen is not None:
            try:
                await auffrischen()
            except Exception:  # noqa: BLE001
                log.exception("last_reported nicht aufgefrischt")
        m = erfassen(self.ha, self.einst.entitaeten, jetzt)
        d: dict[str, Any] = {}

        if not self.ha.verbunden:
            self._setze("ha_getrennt", "Keine Verbindung zu Home Assistant")
        elif self.rueckgabe_faellig and not m.experiment_laeuft:
            if jetzt >= self.rueckgabe_faellig:
                await self._rueckgabe("Wiederholung der Rückgabe")
        elif self.betriebsart == "aus":
            if self.uebernommen:
                await self._rueckgabe("Betriebsart aus")
            elif not self.grund.startswith("Zurückgegeben"):
                self._setze("aus", "Betriebsart aus")
        elif m.experiment_laeuft:
            if self.zustand != "pause_experiment":
                await self._stell_task_abbrechen()
                self.uebernommen = False  # heizexp führt das TRV und stellt es selbst zurück
            self._setze("pause_experiment", f"heizexp: {m.experiment_status}")
        else:
            if self.zustand == "aktiv" and m.trv_modus == str(self.einst.trv["modus_automatisch"]):
                self.uebernommen = False
                self._stoerung(jetzt, "TRV extern auf Automatik gestellt (Watchdog/Hand)")
            if self.zustand != "aktiv":
                await self._uebernahme(m, jetzt)
            if self.zustand == "aktiv":
                await self._regeln(m, dt, jetzt, d)

        d.update(self._grunddiagnose(m, jetzt))
        await self._melden(jetzt, d)
        self.speichern()
        return d

    def _setze(self, zustand_: str, grund: str) -> None:
        if (zustand_, grund) != (self.zustand, self.grund):
            log.info("Zustand %s (%s)", zustand_, grund)
        self.zustand, self.grund = zustand_, grund

    def _stoerung(self, jetzt: datetime, grund: str) -> None:
        self._setze("stoerung", grund)
        self.naechster_versuch = jetzt + WIEDERHOLUNG

    async def _uebernahme(self, m: Messung, jetzt: datetime) -> None:
        if self.zustand == "stoerung" and self.naechster_versuch and jetzt < self.naechster_versuch:
            return
        alter_s = m.raum_alter_s if m.raum_alter_s is not None else math.inf
        if m.raum is None or alter_s > float(self.param["fuehler_max_alter_min"]) * 60:
            self._stoerung(jetzt, "Raumfühler fehlt oder veraltet, keine Übernahme")
            return
        if m.konkurrent_aktiv:
            self._stoerung(jetzt, "Anderer Regler (Better Thermostat o. a.) aktiv")
            return
        ok, meldung = await self.antrieb.uebernehmen()
        if not ok:
            self.uebernommen = False
            self._stoerung(jetzt, f"Übernahme gescheitert: {meldung}")
            return
        neu = not (self.uebernommen and self.regler_geladen)
        self.uebernommen = True
        self.naechster_versuch = None
        self.befehl, self.befehl_zeit, self.bestaetigt_ok = None, None, True
        if neu:
            h0 = self.stellglied.h_aus_ventil(m.ventil_ist or 0)
            self.regler.stossfrei_initialisieren(h0, self._eingang(jetzt, self.einst.takt_s, m.raum, True, 1.0))
        self.regler_geladen = True
        self._setze("aktiv", "Übernommen" + ("" if neu else " (Neustart, Reglerzustand fortgesetzt)"))

    async def _rueckgabe(self, grund: str) -> bool:
        await self._stell_task_abbrechen()
        ok, meldung = await self.antrieb.zurueckgeben()
        if ok:
            self.uebernommen = False
            self.rueckgabe_faellig = None
            self._setze("aus", f"Zurückgegeben: {grund}")
        else:
            # TRV hängt evtl. im Handbetrieb: weiter als übernommen führen und wiederholen
            self.rueckgabe_faellig = self.uhr() + RUECKGABE_WIEDERHOLUNG
            self._stoerung(self.uhr(), f"Rückgabe unvollständig ({grund}): {meldung}")
        await self._ereignis("rueckgabe", f"{grund}: {'ok' if ok else 'FEHLER'} {meldung}")
        return ok

    def _eingang(self, jetzt: datetime, dt: float, y: float, frei: bool, h_max: float) -> ReglerEingang:
        return ReglerEingang(
            zeit=jetzt, dt_s=dt, y=y, w=self.sollwert, parameter=self.param,
            integration_frei=frei, h_min=0.0, h_max=h_max, h_wirksam=self.h_wirksam,
        )

    async def _regeln(self, m: Messung, dt: float, jetzt: datetime, d: dict[str, Any]) -> None:
        p = self.param
        alter_min = (m.raum_alter_s if m.raum_alter_s is not None else math.inf) / 60.0
        if m.raum is None or alter_min > float(p["fuehler_rueckgabe_min"]):
            if await self._rueckgabe("Raumfühler zu lange ohne Meldung"):
                self._stoerung(jetzt, "Raumfühler zu lange ohne Meldung, TRV regelt selbst")
            return
        fuehler_veraltet = alter_min > float(p["fuehler_max_alter_min"])

        # Fenster und Nachlauf
        fenster = bool(m.fenster_offen)
        if fenster:
            self.fenster_war_offen, self.nachlauf_bis = True, None
        elif self.fenster_war_offen:
            self.fenster_war_offen = False
            self.nachlauf_bis = jetzt + timedelta(minutes=float(p["nachlauf_min"]))
        nachlauf = self.nachlauf_bis is not None and jetzt < self.nachlauf_bis

        y = m.raum
        self.erholung.aktualisieren(y, dt, fenster or nachlauf, p)
        y_eff = self.erholung.y_eff(y, p) if p["erholung_aktiv"] and (fenster or nachlauf) else y

        zentrale_aus, zentrale_grund = self.zentrale.aktualisieren(
            jetzt.astimezone(TZ), dt, m.ventil_ist, m.zuleitung_ueber, p
        )
        if (
            not zentrale_aus
            and m.ventil_ist is not None
            and m.ventil_ist >= float(p["zentrale_ventil_min"])
            and m.zuleitung_ueber is not None
        ):
            self.vorlauf_ueber = m.zuleitung_ueber

        frei = not (fenster or zentrale_aus or fuehler_veraltet or not self.bestaetigt_ok)
        h_max = float(p["h_max_nachlauf"]) if nachlauf else 1.0
        aus = self.regler.schritt(self._eingang(jetzt, dt, y_eff, frei, h_max))
        h_roh = float(aus.diagnose.get("h_roh", aus.h))
        saettigung = h_roh >= h_max - 1e-9 or h_roh <= 1e-9

        dith, dith_i = dither(jetzt, p, not (fenster or nachlauf or zentrale_aus or fuehler_veraltet or saettigung))
        h = _begrenzt(aus.h + dith, 0.0, h_max)
        faktor = 1.0
        if p["vorlauf_kompensation"] and self.vorlauf_ueber:
            faktor = _begrenzt(float(p["vorlauf_ref_k"]) / self.vorlauf_ueber, 0.5, 2.0)
            h = _begrenzt(h * faktor, 0.0, h_max)

        erzwingen, sicherheit = False, ""
        if fenster:
            h, erzwingen, sicherheit = 0.0, True, "Fenster offen"
        elif y > float(p["t_max"]):
            h, erzwingen, sicherheit = 0.0, True, "Höchsttemperatur"
        elif y < float(p["t_frost"]):
            h, erzwingen, sicherheit = 1.0, True, "Frostschutz"

        ziel = self.stellglied.ventil(h, float(p["ventil_max"]))
        gesendet = False
        halten = fuehler_veraltet or (zentrale_aus and bool(p["ventil_halten_zentrale_aus"]))
        if (not halten or erzwingen) and self._stell_task is None:
            if Stellglied.senden(jetzt, ziel, self.befehl, self.befehl_zeit, p, erzwingen):
                self._stellen_starten(ziel, jetzt)
                gesendet = True
        self.h_wirksam = self.stellglied.h_aus_ventil(m.ventil_ist if m.ventil_ist is not None else ziel)

        if sicherheit:
            grund = sicherheit
        elif fuehler_veraltet:
            grund = "Raumfühler veraltet, Ventil gehalten"
        elif nachlauf:
            grund = "Nachlauf nach Lüften"
        elif zentrale_aus:
            grund = f"Zentrale aus: {zentrale_grund}" + (", Ventil gehalten" if p["ventil_halten_zentrale_aus"] else "")
        else:
            grund = "Regelt"
        self._setze("aktiv", grund)

        d.update(
            y_eff=y_eff, e=self.sollwert - y, p=aus.diagnose.get("p"), i=aus.diagnose.get("i"),
            dither=dith, h_regler=aus.h, h=h, vorlauf_faktor=faktor, ventil_soll=ziel,
            speicher=self.erholung.speicher, saettigung=saettigung, nachlauf=nachlauf,
            zentrale_aus=zentrale_aus, fuehler_veraltet=fuehler_veraltet, integration_frei=frei,
            gueltig=not (fenster or nachlauf or zentrale_aus or fuehler_veraltet or erzwingen) and self.bestaetigt_ok,
            # nur InfluxDB
            h_roh=h_roh, w_f=aus.diagnose.get("w_f"), dither_index=dith_i, befehl_gesendet=gesendet,
            zentrale_grund=zentrale_grund, raum_alter_s=m.raum_alter_s,
        )

    # --------------------------------------------------------------- Stellen
    def _stellen_starten(self, ziel: int, jetzt: datetime) -> None:
        self.befehl, self.befehl_zeit = ziel, jetzt
        self._stell_task = asyncio.create_task(self._stellen(ziel), name=f"ventil_{ziel}")

    async def _stellen(self, ziel: int) -> None:
        try:
            erg = await self.antrieb.ventil_setzen(ziel)
        except Exception as fehler:  # noqa: BLE001 – darf den Takt nie beenden
            log.exception("Ventil setzen fehlgeschlagen")
            erg = SetzErgebnis(False, ziel, 1, None, None, str(fehler))
        finally:
            self._stell_task = None
        self.befehle += erg.versuche
        if erg.ok:
            self.bestaetigt_ok, self.bestaetigt_um, self.latenz_s = True, erg.bestaetigt, erg.latenz_s
        else:
            self.fehlversuche += 1
            self.bestaetigt_ok = False
            self.befehl = FEHLGESCHLAGEN
            await self._ereignis("ventil_fehler", f"Ventil {ziel} nicht bestätigt: {erg.meldung}")

    async def _stell_task_abbrechen(self) -> None:
        t = self._stell_task
        if t is not None:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._stell_task = None

    # --------------------------------------------------------------- Melden
    def _grunddiagnose(self, m: Messung, jetzt: datetime) -> dict[str, Any]:
        return {
            "zustand": self.zustand,
            "grund": self.grund,
            "regler": self.regler.name,
            "y": m.raum,
            "w": self.sollwert,
            "ventil_ist": m.ventil_ist,
            "ventil_bestaetigt": self.bestaetigt_um.isoformat() if self.bestaetigt_um else None,
            "latenz_s": self.latenz_s,
            "fehlversuche": self.fehlversuche,
            "befehle": self.befehle,
            "zuleitung_ueber": m.zuleitung_ueber,
            "aussen": m.aussen,
            "fenster": bool(m.fenster_offen),
            "experiment": m.experiment_laeuft,
            "herzschlag": jetzt.isoformat(),
            "version": __version__,
            "trv_modus": m.trv_modus,
            "betriebsart": self.betriebsart,
        }

    async def _melden(self, jetzt: datetime, d: dict[str, Any]) -> None:
        try:
            # HA bekommt gerundete Werte, InfluxDB die vollen
            await self.bedienung.diagnose_senden(
                {k: round(v, 4) if isinstance(v, float) else v for k, v in d.items()}
            )
            await self._bedienung_senden()
        except Exception:  # noqa: BLE001
            log.exception("Diagnose an HA fehlgeschlagen")
        felder = {k: v for k, v in d.items() if k not in ("herzschlag", "ventil_bestaetigt", "version")}
        await self.protokoll.schreiben(jetzt, felder, {"regler": self.regler.name})

    async def _ereignis(self, typ: str, text: str) -> None:
        log.info("Ereignis %s: %s", typ, text)
        f = getattr(self.protokoll, "ereignis", None)
        if f is not None:
            try:
                await f(self.uhr(), typ, text)
            except Exception:  # noqa: BLE001
                log.exception("Ereignis nicht protokolliert")
