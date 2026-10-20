"""Momentaufnahme aller Eingänge eines Takts, gebaut aus dem HA-Zustandscache."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .schnittstellen import HA, EntityZustand

UNGUELTIG = {"unavailable", "unknown", "none", ""}
EXPERIMENT_AKTIV = {"vorbereitung", "läuft", "begrenzt_tmax", "wiederherstellung"}


@dataclass(frozen=True)
class Messung:
    zeit: datetime
    raum: float | None
    raum_alter_s: float | None  # seit letzter Meldung des Fühlers (last_reported)
    zuleitung: float | None
    aussen: float | None
    fenster_offen: bool | None
    ventil_ist: int | None
    trv_modus: str | None
    trv_hvac: str | None
    trv_sollwert: float | None
    experiment_status: str | None
    konkurrent_aktiv: bool

    @property
    def zuleitung_ueber(self) -> float | None:
        if self.zuleitung is None or self.raum is None:
            return None
        return self.zuleitung - self.raum

    @property
    def experiment_laeuft(self) -> bool:
        return self.experiment_status in EXPERIMENT_AKTIV


def _zahl(z: EntityZustand | None) -> float | None:
    if z is None or z.state.lower() in UNGUELTIG:
        return None
    try:
        return float(z.state)
    except ValueError:
        return None


def _text(z: EntityZustand | None) -> str | None:
    if z is None or z.state.lower() in UNGUELTIG:
        return None
    return z.state


def erfassen(ha: HA, entitaeten: dict[str, object], zeit: datetime) -> Messung:
    def z(key: str) -> EntityZustand | None:
        return ha.zustand(str(entitaeten[key]))

    raum = z("raum")
    fenster = _text(z("fenster"))
    ventil = _zahl(z("ventil"))
    trv = z("trv")
    konkurrent_aktiv = any(
        (k := ha.zustand(str(e))) is not None and k.state.lower() not in UNGUELTIG | {"off"}
        for e in entitaeten.get("konkurrenten", [])  # type: ignore[union-attr]
    )
    return Messung(
        zeit=zeit,
        raum=_zahl(raum),
        raum_alter_s=(zeit - raum.last_reported).total_seconds() if raum is not None else None,
        zuleitung=_zahl(z("zuleitung")),
        aussen=_zahl(z("aussen")),
        fenster_offen=None if fenster is None else fenster == "on",
        ventil_ist=None if ventil is None else int(round(ventil)),
        trv_modus=_text(z("trv_modus")),
        trv_hvac=_text(trv),
        trv_sollwert=None if trv is None else trv.attributes.get("temperature"),
        experiment_status=_text(z("experiment_status")),
        konkurrent_aktiv=konkurrent_aktiv,
    )
