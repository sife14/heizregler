"""Live-Parameter: einzige Quelle für Name, Grenzen und Vorgabewert.

Jeder Eintrag erscheint in HA automatisch als number- bzw. switch-Entität
(MQTT Discovery). Werte werden im Zustandsfile gespeichert und überleben
Neustarts. Neuer Parameter = neuer Eintrag hier, sonst nichts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Zahl:
    key: str
    name: str
    vorgabe: float
    min: float
    max: float
    schritt: float
    einheit: str | None = None
    gruppe: str = "regler"
    icon: str | None = None


@dataclass(frozen=True)
class Schalter:
    key: str
    name: str
    vorgabe: bool
    gruppe: str = "regler"
    icon: str | None = None


ZAHLEN: tuple[Zahl, ...] = (
    # PI
    Zahl("kp", "Kp", 0.8, 0.05, 5.0, 0.05, "1/K", "pi", "mdi:alpha-p"),
    Zahl("ti_min", "Ti", 40, 5, 300, 1, "min", "pi", "mdi:alpha-i"),
    Zahl("b", "Sollwertgewichtung b", 0.5, 0.0, 1.0, 0.05, None, "pi", "mdi:scale-balance"),
    # Stellglied
    Zahl("ventil_min_aenderung", "Ventil Mindeständerung", 8, 1, 64, 1, None, "stellglied", "mdi:valve"),
    Zahl("stelltakt_min", "Stelltakt", 10, 1, 60, 1, "min", "stellglied", "mdi:timer-outline"),
    Zahl("ventil_max", "Ventil Maximum", 255, 0, 255, 1, None, "stellglied", "mdi:valve-open"),
    # Sicherheit
    Zahl("t_max", "Höchsttemperatur", 24.0, 18.0, 28.0, 0.1, "°C", "sicherheit", "mdi:thermometer-high"),
    Zahl("t_frost", "Frostschutz", 12.0, 5.0, 16.0, 0.5, "°C", "sicherheit", "mdi:snowflake-thermometer"),
    # klimasensor4 meldet nur bei Änderung, spätestens nach ~60 min (7 Tage: 9 % der Abstände > 60 min, max 133)
    Zahl("fuehler_max_alter_min", "Fühler veraltet nach", 90, 5, 240, 1, "min", "sicherheit", "mdi:timer-alert-outline"),
    Zahl("fuehler_rueckgabe_min", "Rückgabe an TRV nach", 180, 10, 480, 1, "min", "sicherheit", "mdi:timer-cancel-outline"),
    # Fenster und Erholung (Zweispeicher-Effekt)
    Zahl("nachlauf_min", "Nachlauf nach Fenster", 60, 0, 240, 5, "min", "fenster", "mdi:window-closed-variant"),
    Zahl("h_max_nachlauf", "Max. Anforderung im Nachlauf", 0.6, 0.0, 1.0, 0.05, None, "fenster", "mdi:gauge-low"),
    Zahl("erholung_tau_min", "Erholung τ", 30, 5, 180, 1, "min", "fenster", "mdi:chart-bell-curve-cumulative"),
    Zahl("erholung_horizont_min", "Erholung Horizont", 20, 0, 120, 5, "min", "fenster", "mdi:crystal-ball"),
    Zahl("speicher_tau_min", "Speicher τ (Auskühlen)", 240, 30, 1440, 10, "min", "fenster", "mdi:sofa"),
    # Zentrale
    Zahl("zentrale_uebertemp_k", "Zentrale aus unter", 2.0, 0.5, 10.0, 0.5, "K", "zentrale", "mdi:water-boiler-off"),
    Zahl("zentrale_ventil_min", "Zentrale-Erkennung ab Ventil", 64, 16, 255, 1, None, "zentrale", "mdi:valve"),
    Zahl("zentrale_erkennung_min", "Zentrale-Erkennung Dauer", 20, 5, 120, 5, "min", "zentrale", "mdi:timer-sand"),
    Zahl("zentrale_aus_von_h", "Zentrale nachts aus von", 22.0, 0.0, 24.0, 0.5, "h", "zentrale", "mdi:clock-start"),
    Zahl("zentrale_aus_bis_h", "Zentrale nachts aus bis", 5.0, 0.0, 24.0, 0.5, "h", "zentrale", "mdi:clock-end"),
    # Identifikation
    Zahl("dither_amplitude", "Dither Amplitude", 0.08, 0.0, 0.3, 0.01, None, "identifikation", "mdi:sine-wave"),
    Zahl("dither_takt_min", "Dither Takt", 30, 5, 240, 5, "min", "identifikation", "mdi:metronome"),
    # Vorlaufkompensation
    Zahl("vorlauf_ref_k", "Vorlauf Referenz-Übertemperatur", 6.0, 1.0, 30.0, 0.5, "K", "vorlauf", "mdi:thermometer-lines"),
)

SCHALTER: tuple[Schalter, ...] = (
    Schalter("erholung_aktiv", "Erholungsvorhersage", True, "fenster", "mdi:crystal-ball"),
    Schalter("zentrale_zeitfenster", "Zentrale-Zeitfenster nutzen", True, "zentrale", "mdi:clock-outline"),
    # Ohne Vorlauf bringt Stellen nichts außer Batterieverbrauch
    Schalter("ventil_halten_zentrale_aus", "Ventil halten bei Zentrale aus", True, "zentrale", "mdi:pause-circle-outline"),
    Schalter("dither_aktiv", "Dither aktiv", False, "identifikation", "mdi:sine-wave"),
    Schalter("vorlauf_kompensation", "Vorlaufkompensation", False, "vorlauf", "mdi:thermometer-auto"),
)

BETRIEBSARTEN: tuple[str, ...] = ("aus", "pi")

SOLLWERT_VORGABE = 21.0
SOLLWERT_MIN = 15.0
SOLLWERT_MAX = 25.0
SOLLWERT_SCHRITT = 0.1


def vorgaben() -> dict[str, float | bool]:
    werte: dict[str, float | bool] = {z.key: z.vorgabe for z in ZAHLEN}
    werte.update({s.key: s.vorgabe for s in SCHALTER})
    return werte


def begrenzen(key: str, wert: float | bool) -> float | bool:
    """Wert auf die Definition zurechtstutzen; unbekannte Keys -> KeyError."""
    for z in ZAHLEN:
        if z.key == key:
            return min(max(float(wert), z.min), z.max)
    for s in SCHALTER:
        if s.key == key:
            return bool(wert)
    raise KeyError(key)
