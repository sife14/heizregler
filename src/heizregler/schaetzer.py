"""Einfache Schätzer, die der PI-Regler nicht selbst kennt.

Erholung: Nach dem Lüften ist die Luft kalt, Möbel und Wände sind noch warm
und heizen die Luft von selbst wieder auf (zweiter Speicher). Ohne Korrektur
sieht der PI eine große Abweichung, öffnet weit und überschwingt. Der Schätzer
führt eine Speichertemperatur mit und sagt voraus, wie weit sich die Luft in
den nächsten H Minuten ohnehin erholt:

    y_eff = y + (T_s − y)·(1 − exp(−H/τ_erh))      (nur wenn T_s > y)

Der Regler arbeitet im Nachlauf mit y_eff statt y. Im Normalbetrieb folgt T_s
der Luft mit 60 min, bei offenem Fenster und im Nachlauf kühlt T_s nur langsam
(τ_speicher) Richtung Luft ab. Ein MPC mit Kalman-Filter ersetzt das später.

Zentrale: Ventil offen, aber Zuleitung kaum wärmer als der Raum -> die
Heizungszentrale liefert nicht. Zusätzlich ein festes Nachtfenster.
"""

from __future__ import annotations

import math
from datetime import datetime

NORMAL_TAU_MIN = 60.0


def _folgen(alt: float, ziel: float, dt_s: float, tau_min: float) -> float:
    return alt + (ziel - alt) * (1.0 - math.exp(-dt_s / (tau_min * 60.0)))


class Erholung:
    def __init__(self) -> None:
        self.speicher: float | None = None

    def aktualisieren(self, y: float, dt_s: float, gestoert: bool, parameter: dict[str, float | bool]) -> None:
        """gestoert = Fenster offen oder Nachlauf."""
        if self.speicher is None:
            self.speicher = y
            return
        tau = float(parameter["speicher_tau_min"]) if gestoert else NORMAL_TAU_MIN
        self.speicher = _folgen(self.speicher, y, dt_s, tau)

    def y_eff(self, y: float, parameter: dict[str, float | bool]) -> float:
        if self.speicher is None or self.speicher <= y:
            return y
        h = float(parameter["erholung_horizont_min"])
        tau = float(parameter["erholung_tau_min"])
        return y + (self.speicher - y) * (1.0 - math.exp(-h / tau))

    def zustand(self) -> dict[str, float | None]:
        return {"speicher": self.speicher}

    def zustand_laden(self, daten: dict[str, float | None]) -> None:
        s = daten.get("speicher")
        self.speicher = None if s is None else float(s)


def im_zeitfenster(lokal: datetime, von_h: float, bis_h: float) -> bool:
    """Liegt die Ortszeit in [von, bis)? Fenster über Mitternacht erlaubt."""
    t = lokal.hour + lokal.minute / 60.0
    if von_h == bis_h:
        return False
    if von_h < bis_h:
        return von_h <= t < bis_h
    return t >= von_h or t < bis_h


class Zentrale:
    def __init__(self) -> None:
        self.kalt_s = 0.0
        self.erkannt = False

    def aktualisieren(
        self,
        lokal: datetime,
        dt_s: float,
        ventil_ist: int | None,
        zuleitung_ueber: float | None,
        parameter: dict[str, float | bool],
    ) -> tuple[bool, str]:
        """(aus, Grund). Erkennung nur bei ausreichend offenem Ventil, sonst
        bleibt das letzte Ergebnis stehen."""
        if ventil_ist is not None and zuleitung_ueber is not None and ventil_ist >= parameter["zentrale_ventil_min"]:
            if zuleitung_ueber < float(parameter["zentrale_uebertemp_k"]):
                self.kalt_s += dt_s
                if self.kalt_s >= float(parameter["zentrale_erkennung_min"]) * 60.0:
                    self.erkannt = True
            else:
                self.kalt_s = 0.0
                self.erkannt = False
        if self.erkannt:
            return True, "Zuleitung kalt trotz offenem Ventil"
        if parameter["zentrale_zeitfenster"] and im_zeitfenster(
            lokal, float(parameter["zentrale_aus_von_h"]), float(parameter["zentrale_aus_bis_h"])
        ):
            return True, "Nachtfenster der Zentrale"
        return False, ""
