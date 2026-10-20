"""Schnittstelle für austauschbare Regler (PI jetzt, MPC später).

Ein Regler kennt weder HA noch MQTT. Er bekommt pro Takt einen ReglerEingang
und liefert eine Heizanforderung h (0…1) plus Diagnosewerte. Begrenzungen,
Sicherheit und Stellglied liegen außerhalb (Aufsicht).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class ReglerEingang:
    zeit: datetime
    dt_s: float
    y: float  # Regelgröße, ggf. durch Erholungsvorhersage korrigiert
    w: float  # Sollwert
    parameter: dict[str, float | bool]
    integration_frei: bool  # False: I-Anteil einfrieren (Aufsicht entscheidet)
    h_min: float = 0.0
    h_max: float = 1.0
    # Tatsächlich wirksame Anforderung des letzten Takts (nach Begrenzung,
    # Dither, Stellglied) – für Anti-Windup per Rückrechnung.
    h_wirksam: float | None = None
    zusatz: dict[str, Any] = field(default_factory=dict)  # Messung u. a. für modellbasierte Regler


@dataclass(frozen=True)
class ReglerAusgang:
    h: float  # begrenzt auf [h_min, h_max]
    diagnose: dict[str, Any]  # mindestens p, i, e (Keys aus diagnose.GROESSEN)


class Regler(Protocol):
    name: str

    def schritt(self, eingang: ReglerEingang) -> ReglerAusgang: ...

    def stossfrei_initialisieren(self, h_aktuell: float, eingang: ReglerEingang) -> None:
        """Internen Zustand so setzen, dass der nächste Schritt h_aktuell liefert."""

    def zuruecksetzen(self) -> None: ...

    def zustand(self) -> dict[str, Any]: ...

    def zustand_laden(self, daten: dict[str, Any]) -> None: ...
