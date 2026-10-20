"""Verträge zwischen Kern (Aufsicht, Regler) und Ein-/Ausgabe.

Der Kern hängt nur von diesen Protokollen ab, nicht von aiohttp/MQTT. Tests
setzen Attrappen ein, der Dienst die echten Klassen aus quellen/.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol


@dataclass(frozen=True)
class EntityZustand:
    state: str
    attributes: dict[str, Any]
    last_changed: datetime
    last_updated: datetime
    # Zeitpunkt der letzten Meldung, auch wenn sich nichts geändert hat.
    # Bei Z2M-Entitäten = letzte MQTT-Nachricht des Geräts.
    last_reported: datetime


class HA(Protocol):
    @property
    def verbunden(self) -> bool: ...

    def zustand(self, entity_id: str) -> EntityZustand | None: ...

    async def dienst(
        self, domain: str, service: str, data: dict[str, Any] | None = None, target: dict[str, Any] | None = None
    ) -> None:
        """Ruft einen HA-Dienst auf. Wirft HAFehler bei Fehlern/Timeout."""

    async def warte_auf(
        self, entity_id: str, bedingung: Callable[[EntityZustand], bool], timeout_s: float
    ) -> EntityZustand | None:
        """Wartet, bis bedingung(zustand) wahr ist (prüft sofort und bei jeder Meldung).
        None bei Timeout."""


class HAFehler(Exception):
    pass


@dataclass(frozen=True)
class SetzErgebnis:
    ok: bool
    wert: int
    versuche: int
    latenz_s: float | None  # Befehl bis bestätigende Meldung
    bestaetigt: datetime | None
    meldung: str = ""


class Stellantrieb(Protocol):
    async def ventil_setzen(self, wert: int) -> SetzErgebnis: ...

    async def uebernehmen(self) -> tuple[bool, str]:
        """TRV in Handbetrieb bringen. (ok, Grund/Meldung)."""

    async def zurueckgeben(self) -> tuple[bool, str]:
        """TRV in Automatikbetrieb mit gesichertem Sollwert zurückgeben."""


class Bedienung(Protocol):
    """HA-Seite des Reglers (MQTT Discovery)."""

    async def diagnose_senden(self, werte: dict[str, Any]) -> None: ...

    async def parameter_senden(self, werte: dict[str, float | bool]) -> None: ...

    async def bedienung_senden(self, betriebsart: str, sollwert: float, hvac_aktion: str) -> None: ...


class Protokoll(Protocol):
    async def schreiben(self, zeit: datetime, felder: dict[str, Any], tags: dict[str, str] | None = None) -> None:
        """Ein Datensatz pro Takt. Darf nie werfen; puffert bei Ausfall."""


# Rückrufe von der Bedienung in den Kern
BetriebsartRueckruf = Callable[[str], Awaitable[None]]
SollwertRueckruf = Callable[[float], Awaitable[None]]
ParameterRueckruf = Callable[[str, float | bool], Awaitable[None]]
TasteRueckruf = Callable[[str], Awaitable[None]]
