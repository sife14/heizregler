"""TRV-Stellantrieb über HA (Vorbild: heizexp_ventil_setzen / _lauf / _wiederherstellung).

Bestätigt wird eine Ventilstellung erst durch eine Z2M-Meldung des Geräts,
die NACH dem Befehl eintrifft und den Zielwert enthält. Der Zustand der
number-Entität allein reicht nicht: Er ist der Z2M-Cache und „bestätigt“
sofort, wenn Ziel = Cachewert (20.10. 08:33). Die Meldungen kommen über einen
HA-MQTT-Trigger auf ``zigbee2mqtt/<gerät>`` (siehe quellen/ha.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from heizregler.einstellungen import Einstellungen
from heizregler.schnittstellen import HA, EntityZustand, HAFehler, SetzErgebnis

log = logging.getLogger(__name__)

NICHT_DA = ("unavailable", "unknown")


class HAMitTrigger(HA, Protocol):
    def trigger_registrieren(self, trigger: dict[str, Any], rueckruf: Callable[[dict[str, Any]], None]) -> None: ...


def _jetzt() -> datetime:
    return datetime.now(timezone.utc)


def _als_int(wert: Any) -> int | None:
    try:
        return int(float(wert))
    except (TypeError, ValueError):
        return None


def _als_float(wert: Any) -> float | None:
    try:
        return float(wert)
    except (TypeError, ValueError):
        return None


class TRVStellantrieb:
    """Implementiert schnittstellen.Stellantrieb."""

    def __init__(self, ha: HAMitTrigger, einstellungen: Einstellungen, wartezeit_s: float = 60.0) -> None:
        e, t = einstellungen.entitaeten, einstellungen.trv
        self._ha = ha
        self._trv = str(e["trv"])
        self._ventil = str(e["ventil"])
        self._modus = str(e["trv_modus"])
        self._geraet = str(e["z2m_geraet"])
        self._konkurrenten = [str(k) for k in e.get("konkurrenten", [])]
        self._manuell = str(t["modus_manuell"])
        self._automatisch = str(t["modus_automatisch"])
        self._hvac_regel = str(t["hvac_regelbetrieb"])
        self._sollwert_rueckgabe = float(t["sollwert_rueckgabe"])
        self._timeout = float(t["bestaetigung_timeout_s"])
        self._versuche = int(t["versuche"])
        self._wartezeit = wartezeit_s
        self._sicherung: dict[str, Any] | None = None
        self._meldung: dict[str, Any] = {}
        self._meldung_zeit: datetime | None = None
        self._neue_meldung = asyncio.Event()
        ha.trigger_registrieren({"trigger": "mqtt", "topic": f"zigbee2mqtt/{self._geraet}"}, self._bei_meldung)

    # ------------------------------------------------------------ Meldungen
    @property
    def letzte_meldung(self) -> tuple[datetime, dict[str, Any]] | None:
        """Letzte Z2M-Meldung des TRV (Empfangszeit, Nutzdaten)."""
        return None if self._meldung_zeit is None else (self._meldung_zeit, dict(self._meldung))

    def _bei_meldung(self, variables: dict[str, Any]) -> None:
        trig = variables.get("trigger") or {}
        daten = trig.get("payload_json")
        if not isinstance(daten, dict):
            try:
                daten = json.loads(trig.get("payload") or "")
            except (TypeError, ValueError):
                return
            if not isinstance(daten, dict):
                return
        self._meldung = daten
        self._meldung_zeit = _jetzt()
        ereignis, self._neue_meldung = self._neue_meldung, asyncio.Event()
        ereignis.set()

    async def _warte_meldung(self, t0: datetime, ziel: int, timeout_s: float) -> datetime | None:
        frist = asyncio.get_running_loop().time() + timeout_s
        while True:
            if (
                self._meldung_zeit is not None
                and self._meldung_zeit > t0
                and _als_int(self._meldung.get("valve_position")) == ziel
            ):
                return self._meldung_zeit
            rest = frist - asyncio.get_running_loop().time()
            if rest <= 0:
                return None
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._neue_meldung.wait(), rest)

    # ---------------------------------------------------------- Stellantrieb
    async def ventil_setzen(self, wert: int) -> SetzErgebnis:
        """Latenz = erster Befehl bis bestätigende Meldung (über alle Versuche)."""
        ziel = max(0, min(255, int(round(wert))))
        t_start = _jetzt()
        fehler = ""
        for versuch in range(1, self._versuche + 1):
            t0 = _jetzt()
            try:
                await self._ha.dienst("number", "set_value", {"value": ziel}, {"entity_id": self._ventil})
            except HAFehler as f:
                fehler = str(f)
            try:
                await self._ha.dienst(
                    "mqtt", "publish", {"topic": f"zigbee2mqtt/{self._geraet}/get", "payload": '{"valve_position": ""}'}
                )
            except HAFehler as f:
                fehler = str(f)
            bestaetigt = await self._warte_meldung(t0, ziel, self._timeout)
            if bestaetigt is not None:
                return SetzErgebnis(True, ziel, versuch, (bestaetigt - t_start).total_seconds(), bestaetigt)
            log.info("Ventil %s: Versuch %s nicht bestätigt", ziel, versuch)
        gemeldet = self._meldung.get("valve_position")
        meldung = f"nicht bestätigt nach {self._versuche} Versuchen (gemeldet {gemeldet})"
        return SetzErgebnis(False, ziel, self._versuche, None, None, f"{meldung}; {fehler}" if fehler else meldung)

    async def uebernehmen(self) -> tuple[bool, str]:
        for k in self._konkurrenten:
            z = self._ha.zustand(k)
            if z is not None and z.state not in (*NICHT_DA, "off"):
                return False, f"Konkurrierender Regler {k} aktiv ({z.state})"
        if not self._ha.verbunden:
            return False, "keine Verbindung zu HA"
        trv, modus = self._ha.zustand(self._trv), self._ha.zustand(self._modus)
        if trv is None or modus is None or trv.state in NICHT_DA or modus.state in NICHT_DA:
            return False, "TRV nicht erreichbar"

        hinweise: list[str] = []
        if modus.state == self._manuell:
            hinweise.append("TRV war schon im Handbetrieb (Neustart)")
        else:
            self._sicherung = {
                "sollwert": _als_float(trv.attributes.get("temperature")),
                "hvac": trv.state,
                "zeit": _jetzt().isoformat(),
            }

        # Sollwert auf Minimum, damit der Geräte-PI beim Wechsel nicht öffnet
        min_temp = _als_float(trv.attributes.get("min_temp")) or 8.0
        if (_als_float(trv.attributes.get("temperature")) or 99.0) > min_temp:
            if not await self._mit_pruefung(
                "climate", "set_temperature", {"temperature": min_temp}, self._trv,
                lambda z: (_als_float(z.attributes.get("temperature")) or 99.0) <= min_temp, 1,
            ):
                hinweise.append(f"Sollwert {min_temp} °C nicht bestätigt")

        if trv.state != self._hvac_regel and not await self._mit_pruefung(
            "climate", "set_hvac_mode", {"hvac_mode": self._hvac_regel}, self._trv,
            lambda z: z.state == self._hvac_regel, 3,
        ):
            ok, meldung = await self.zurueckgeben()
            return False, f"hvac_mode {self._hvac_regel} nicht übernommen; Rückgabe: {meldung}"

        if not await self._mit_pruefung(
            "select", "select_option", {"option": self._manuell}, self._modus,
            lambda z: z.state == self._manuell, 3,
        ):
            ok, meldung = await self.zurueckgeben()
            return False, f"TRV-Modus {self._manuell} nicht übernommen; Rückgabe: {meldung}"

        return True, "; ".join(["übernommen", *hinweise])

    async def zurueckgeben(self) -> tuple[bool, str]:
        fehler: list[str] = []
        hinweise: list[str] = []
        if not await self._mit_pruefung(
            "select", "select_option", {"option": self._automatisch}, self._modus,
            lambda z: z.state == self._automatisch, 5,
        ):
            fehler.append(f"TRV-Modus {self._automatisch} nicht bestätigt")

        s = self._sicherung or {}
        sollwert = _als_float(s.get("sollwert"))
        if sollwert is None:
            sollwert = self._sollwert_rueckgabe
            hinweise.append(f"kein gesicherter Sollwert, {sollwert} °C gesetzt")
        try:
            await self._ha.dienst("climate", "set_temperature", {"temperature": sollwert}, {"entity_id": self._trv})
        except HAFehler as f:
            fehler.append(f"Sollwert: {f}")

        # "heat" ist beim Eurotronic Boost -> nie zurückschreiben
        hvac = s.get("hvac")
        trv = self._ha.zustand(self._trv)
        modi = (trv.attributes.get("hvac_modes") if trv else None) or []
        if hvac in (None, "heat", *NICHT_DA) or (modi and hvac not in modi):
            hvac = "auto"
        if trv is None or trv.state != hvac:
            if not await self._mit_pruefung(
                "climate", "set_hvac_mode", {"hvac_mode": hvac}, self._trv, lambda z: z.state == hvac, 3
            ):
                fehler.append(f"hvac_mode {hvac} nicht bestätigt")

        if not fehler:
            self._sicherung = None
        return not fehler, "; ".join([*fehler, *hinweise]) or "zurückgegeben"

    # --------------------------------------------------------------- Zustand
    def zustand(self) -> dict[str, Any]:
        return {"sicherung": self._sicherung}

    def zustand_laden(self, daten: dict[str, Any]) -> None:
        s = (daten or {}).get("sicherung")
        self._sicherung = dict(s) if isinstance(s, dict) else None

    # ---------------------------------------------------------------- intern
    async def _mit_pruefung(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        entity_id: str,
        bedingung: Callable[[EntityZustand], bool],
        versuche: int,
    ) -> bool:
        for versuch in range(1, versuche + 1):
            try:
                await self._ha.dienst(domain, service, data, {"entity_id": entity_id})
            except HAFehler as f:
                log.warning("%s.%s %s (Versuch %s): %s", domain, service, data, versuch, f)
            if await self._ha.warte_auf(entity_id, bedingung, self._wartezeit) is not None:
                return True
        return False
