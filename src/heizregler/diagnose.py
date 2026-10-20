"""Diagnosegrößen: einzige Quelle für alles, was der Regler pro Takt meldet.

Jeder Eintrag wird in HA ein Sensor bzw. Binärsensor (MQTT Discovery, ein
gemeinsamer JSON-State-Topic) und ein Feld im InfluxDB-Measurement.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Groesse:
    key: str
    name: str
    einheit: str | None = None
    device_class: str | None = None
    state_class: str | None = "measurement"
    icon: str | None = None
    binaer: bool = False
    text: bool = False
    diagnose: bool = False  # entity_category: diagnostic


GROESSEN: tuple[Groesse, ...] = (
    # Zustand
    Groesse("zustand", "Zustand", state_class=None, icon="mdi:state-machine", text=True),
    Groesse("grund", "Grund", state_class=None, icon="mdi:comment-text-outline", text=True),
    Groesse("regler", "Aktiver Regler", state_class=None, icon="mdi:function-variant", text=True, diagnose=True),
    # Regelkreis
    Groesse("y", "Raumtemperatur", "°C", "temperature"),
    Groesse("y_eff", "Raumtemperatur wirksam", "°C", "temperature", icon="mdi:crystal-ball"),
    Groesse("w", "Sollwert", "°C", "temperature", icon="mdi:target"),
    Groesse("e", "Regelabweichung", "K", icon="mdi:delta"),
    Groesse("p", "P-Anteil", icon="mdi:alpha-p-box-outline"),
    Groesse("i", "I-Anteil", icon="mdi:alpha-i-box-outline"),
    Groesse("dither", "Dither", icon="mdi:sine-wave"),
    Groesse("h_regler", "Anforderung Regler", icon="mdi:fire"),
    Groesse("h", "Anforderung", icon="mdi:fire"),
    Groesse("vorlauf_faktor", "Vorlauf-Faktor", icon="mdi:multiplication", diagnose=True),
    # Stellglied
    Groesse("ventil_soll", "Ventil Soll", icon="mdi:valve"),
    Groesse("ventil_ist", "Ventil Ist", icon="mdi:valve"),
    Groesse("ventil_bestaetigt", "Ventil bestätigt um", device_class="timestamp", state_class=None, diagnose=True),
    Groesse("latenz_s", "Bestätigungsdauer", "s", "duration", diagnose=True),
    Groesse("fehlversuche", "Fehlversuche", state_class="total_increasing", icon="mdi:alert-circle-outline", diagnose=True),
    Groesse("befehle", "Stellbefehle", state_class="total_increasing", icon="mdi:counter", diagnose=True),
    # Strecke und Schätzung
    Groesse("zuleitung_ueber", "Zuleitung Übertemperatur", "K", icon="mdi:pipe"),
    Groesse("speicher", "Speicher-Schätzung", "°C", "temperature", icon="mdi:sofa"),
    Groesse("aussen", "Außentemperatur", "°C", "temperature", diagnose=True),
    # Abschnittsmarker
    Groesse("gueltig", "Daten gültig", binaer=True, icon="mdi:check-decagram", state_class=None),
    Groesse("saettigung", "Sättigung", binaer=True, icon="mdi:arrow-collapse-vertical", state_class=None),
    Groesse("fenster", "Fenster offen", binaer=True, icon="mdi:window-open-variant", state_class=None),
    Groesse("nachlauf", "Nachlauf", binaer=True, icon="mdi:window-closed-variant", state_class=None),
    Groesse("zentrale_aus", "Zentrale aus", binaer=True, icon="mdi:water-boiler-off", state_class=None),
    Groesse("experiment", "Experiment läuft", binaer=True, icon="mdi:flask-outline", state_class=None),
    Groesse("fuehler_veraltet", "Fühler veraltet", binaer=True, icon="mdi:timer-alert-outline", state_class=None),
    Groesse("integration_frei", "Integration frei", binaer=True, icon="mdi:sigma", state_class=None),
    # Lebenszeichen
    Groesse("herzschlag", "Herzschlag", device_class="timestamp", state_class=None, diagnose=True),
    Groesse("version", "Version", state_class=None, text=True, diagnose=True),
)

KEYS = tuple(g.key for g in GROESSEN)
