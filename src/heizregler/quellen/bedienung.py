"""HA-Bedienung über MQTT Discovery: Gerät „Heizregler“ mit Parametern und Diagnose.

Topics (Basis ``heizregler``):
  status                           online/offline (Last Will, retained)
  diagnose                         JSON mit allen diagnose.GROESSEN (ein Topic)
  parameter/<key>/state|set        je Zahl/Schalter aus parameter.py
  betriebsart/state|set            select
  sollwert/state|set               Sollwert (climate)
  klima/modus/state|set            off/heat (climate)
  klima/aktion/state               off/idle/heating (climate)
  taste/<name>                     Tasten (PRESS)

Die Bedienung spiegelt keine Werte selbst zurück: Der Kern bestätigt
übernommene Werte mit parameter_senden/bedienung_senden.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime
from typing import Any

import aiomqtt

from heizregler import __version__
from heizregler import diagnose, parameter
from heizregler.einstellungen import Einstellungen
from heizregler.schnittstellen import (
    BetriebsartRueckruf,
    ParameterRueckruf,
    SollwertRueckruf,
    TasteRueckruf,
)

log = logging.getLogger(__name__)

GERAET = "heizregler"
TASTEN = {"i_zuruecksetzen": ("I-Anteil zurücksetzen", "mdi:restore")}
BACKOFF_S = (1, 2, 5, 10, 30)
TEXT_MAX = 255


# ------------------------------------------------------------- reine Funktionen
def _geraet(version: str) -> dict[str, Any]:
    return {
        "identifiers": [GERAET],
        "name": "Heizregler",
        "manufacturer": "heizregler",
        "model": "PI-Regler thermostat2",
        "sw_version": version,
    }


def _gemeinsam(basis: str, version: str, domain: str, key: str) -> dict[str, Any]:
    return {
        "unique_id": f"{GERAET}_{key}",
        "default_entity_id": f"{domain}.{GERAET}_{key}",
        "availability_topic": f"{basis}/status",
        "device": _geraet(version),
        "origin": {"name": GERAET, "sw_version": version},
    }


def _ohne_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None or k == "name"}


def discovery_nachrichten(basis: str, praefix: str, version: str = __version__) -> list[tuple[str, dict[str, Any]]]:
    """Alle Discovery-Konfigurationen als (Topic, Payload)."""
    n: list[tuple[str, dict[str, Any]]] = []

    def hinzu(domain: str, key: str, payload: dict[str, Any]) -> None:
        n.append((f"{praefix}/{domain}/{GERAET}/{key}/config", _ohne_none(payload)))

    for z in parameter.ZAHLEN:
        hinzu("number", z.key, {
            **_gemeinsam(basis, version, "number", z.key),
            "name": z.name,
            "command_topic": f"{basis}/parameter/{z.key}/set",
            "state_topic": f"{basis}/parameter/{z.key}/state",
            "min": z.min, "max": z.max, "step": z.schritt, "mode": "box",
            "unit_of_measurement": z.einheit,
            "entity_category": "config",
            "icon": z.icon,
        })
    for s in parameter.SCHALTER:
        hinzu("switch", s.key, {
            **_gemeinsam(basis, version, "switch", s.key),
            "name": s.name,
            "command_topic": f"{basis}/parameter/{s.key}/set",
            "state_topic": f"{basis}/parameter/{s.key}/state",
            "payload_on": "ON", "payload_off": "OFF",
            "entity_category": "config",
            "icon": s.icon,
        })
    hinzu("select", "betriebsart", {
        **_gemeinsam(basis, version, "select", "betriebsart"),
        "name": "Betriebsart",
        "command_topic": f"{basis}/betriebsart/set",
        "state_topic": f"{basis}/betriebsart/state",
        "options": list(parameter.BETRIEBSARTEN),
        "icon": "mdi:power-settings",
    })
    hinzu("climate", "klima", {
        **_gemeinsam(basis, version, "climate", "klima"),
        "default_entity_id": f"climate.{GERAET}",
        "name": None,
        "modes": ["off", "heat"],
        "mode_command_topic": f"{basis}/klima/modus/set",
        "mode_state_topic": f"{basis}/klima/modus/state",
        "temperature_command_topic": f"{basis}/sollwert/set",
        "temperature_state_topic": f"{basis}/sollwert/state",
        "current_temperature_topic": f"{basis}/diagnose",
        "current_temperature_template": "{{ value_json.get('y') }}",
        "action_topic": f"{basis}/klima/aktion/state",
        "min_temp": parameter.SOLLWERT_MIN,
        "max_temp": parameter.SOLLWERT_MAX,
        "temp_step": parameter.SOLLWERT_SCHRITT,
        "precision": 0.1,
        "temperature_unit": "C",
    })
    for key, (name, icon) in TASTEN.items():
        hinzu("button", key, {
            **_gemeinsam(basis, version, "button", key),
            "name": name,
            "command_topic": f"{basis}/taste/{key}",
            "payload_press": "PRESS",
            "entity_category": "config",
            "icon": icon,
        })
    for g in diagnose.GROESSEN:
        domain = "binary_sensor" if g.binaer else "sensor"
        if g.binaer:
            vorlage = (
                f"{{% set v = value_json.get('{g.key}') %}}"
                "{{ 'None' if v is none else ('ON' if v else 'OFF') }}"
            )
        else:
            vorlage = f"{{{{ value_json.get('{g.key}') }}}}"
        payload = {
            **_gemeinsam(basis, version, domain, g.key),
            "name": g.name,
            "state_topic": f"{basis}/diagnose",
            "value_template": vorlage,
            "icon": g.icon,
            "entity_category": "diagnostic" if g.diagnose else None,
            "device_class": g.device_class,
        }
        if g.binaer:
            payload.update({"payload_on": "ON", "payload_off": "OFF"})
        elif not g.text:
            payload.update({"unit_of_measurement": g.einheit, "state_class": g.state_class})
        hinzu(domain, g.key, payload)
    return n


def diagnose_json(werte: dict[str, Any]) -> str:
    """Diagnosewerte serialisieren: datetime -> ISO, Text auf 255 Zeichen."""

    def wandeln(v: Any) -> Any:
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, str):
            return v[:TEXT_MAX]
        if isinstance(v, float) and v != v:  # NaN
            return None
        return v

    return json.dumps({k: wandeln(v) for k, v in werte.items()}, ensure_ascii=False)


def parameter_payload(wert: float | bool) -> str:
    if isinstance(wert, bool):
        return "ON" if wert else "OFF"
    return repr(float(wert))


# ------------------------------------------------------------------ Klasse
class MQTTBedienung:
    """Implementiert schnittstellen.Bedienung."""

    def __init__(
        self,
        einstellungen: Einstellungen,
        betriebsart: BetriebsartRueckruf,
        sollwert: SollwertRueckruf,
        parameter_rueckruf: ParameterRueckruf,
        taste: TasteRueckruf,
        version: str = __version__,
    ) -> None:
        z = einstellungen.zugang
        self._host, self._port = z.mqtt_host, z.mqtt_port
        self._user, self._passwort = z.mqtt_user or None, z.mqtt_password or None
        self._basis = einstellungen.mqtt_basis
        self._praefix = einstellungen.discovery_praefix
        self._version = version
        self._rueckruf_betriebsart = betriebsart
        self._rueckruf_sollwert = sollwert
        self._rueckruf_parameter = parameter_rueckruf
        self._rueckruf_taste = taste
        self._zahlen = {p.key for p in parameter.ZAHLEN}
        self._schalter = {p.key for p in parameter.SCHALTER}
        self._client: aiomqtt.Client | None = None
        self._aufgabe: asyncio.Task | None = None
        self._zustaende: dict[str, str] = {}  # retained Zustände, nach Neuverbinden erneut senden
        self._diagnose: str | None = None
        self._letzte_aktive = next(b for b in parameter.BETRIEBSARTEN if b != "aus")

    @property
    def verbunden(self) -> bool:
        return self._client is not None

    async def start(self) -> None:
        if self._aufgabe is None:
            self._aufgabe = asyncio.create_task(self._schleife(), name="mqtt-bedienung")

    async def stop(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.publish(f"{self._basis}/status", "offline", qos=1, retain=True)
        if self._aufgabe:
            self._aufgabe.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._aufgabe
            self._aufgabe = None

    # ------------------------------------------------------------ Bedienung
    async def diagnose_senden(self, werte: dict[str, Any]) -> None:
        self._diagnose = diagnose_json(werte)
        await self._senden(f"{self._basis}/diagnose", self._diagnose, retain=False)

    async def parameter_senden(self, werte: dict[str, float | bool]) -> None:
        for key, wert in werte.items():
            await self._zustand(f"{self._basis}/parameter/{key}/state", parameter_payload(wert))

    async def bedienung_senden(self, betriebsart: str, sollwert: float, hvac_aktion: str) -> None:
        if betriebsart != "aus":
            self._letzte_aktive = betriebsart
        await self._zustand(f"{self._basis}/betriebsart/state", betriebsart)
        await self._zustand(f"{self._basis}/sollwert/state", repr(float(sollwert)))
        await self._zustand(f"{self._basis}/klima/modus/state", "off" if betriebsart == "aus" else "heat")
        await self._zustand(f"{self._basis}/klima/aktion/state", hvac_aktion)

    # --------------------------------------------------------------- intern
    async def _zustand(self, topic: str, payload: str) -> None:
        self._zustaende[topic] = payload
        await self._senden(topic, payload, retain=True)

    async def _senden(self, topic: str, payload: str, retain: bool) -> None:
        client = self._client
        if client is None:
            return
        try:
            await client.publish(topic, payload, qos=1, retain=retain)
        except aiomqtt.MqttError as f:
            log.warning("MQTT publish %s: %s", topic, f)

    async def _alles_senden(self, client: aiomqtt.Client) -> None:
        await client.publish(f"{self._basis}/status", "online", qos=1, retain=True)
        for topic, payload in discovery_nachrichten(self._basis, self._praefix, self._version):
            await client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=1, retain=True)
        for topic, payload in self._zustaende.items():
            await client.publish(topic, payload, qos=1, retain=True)
        if self._diagnose is not None:
            await client.publish(f"{self._basis}/diagnose", self._diagnose, qos=1, retain=False)

    async def _schleife(self) -> None:
        versuch = 0
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self._host,
                    port=self._port,
                    username=self._user,
                    password=self._passwort,
                    identifier=f"{GERAET}-dienst",
                    keepalive=30,
                    will=aiomqtt.Will(f"{self._basis}/status", "offline", qos=1, retain=True),
                ) as client:
                    await client.subscribe([
                        (f"{self._basis}/parameter/+/set", 1),
                        (f"{self._basis}/betriebsart/set", 1),
                        (f"{self._basis}/sollwert/set", 1),
                        (f"{self._basis}/klima/modus/set", 1),
                        (f"{self._basis}/taste/+", 1),
                        (f"{self._praefix}/status", 1),
                    ])
                    await self._alles_senden(client)
                    self._client = client
                    versuch = 0
                    log.info("MQTT verbunden (%s:%s)", self._host, self._port)
                    async for nachricht in client.messages:
                        await self._verarbeiten(client, nachricht.topic.value, bytes(nachricht.payload or b"").decode())
            except asyncio.CancelledError:
                self._client = None
                raise
            except aiomqtt.MqttError as f:
                log.warning("MQTT: %s", f)
            self._client = None
            pause = BACKOFF_S[min(versuch, len(BACKOFF_S) - 1)]
            versuch += 1
            await asyncio.sleep(pause)

    async def _verarbeiten(self, client: aiomqtt.Client, topic: str, text: str) -> None:
        text = text.strip()
        b = self._basis
        try:
            if topic == f"{self._praefix}/status":
                if text == "online":
                    await self._alles_senden(client)
            elif topic.startswith(f"{b}/parameter/") and topic.endswith("/set"):
                key = topic[len(f"{b}/parameter/"):-len("/set")]
                if key in self._zahlen:
                    await self._rueckruf_parameter(key, parameter.begrenzen(key, float(text)))
                elif key in self._schalter:
                    await self._rueckruf_parameter(key, text.upper() in ("ON", "TRUE", "1"))
                else:
                    log.warning("Unbekannter Parameter %s", key)
            elif topic == f"{b}/betriebsart/set":
                if text in parameter.BETRIEBSARTEN:
                    await self._rueckruf_betriebsart(text)
                else:
                    log.warning("Unbekannte Betriebsart %s", text)
            elif topic == f"{b}/klima/modus/set":
                if text == "off":
                    await self._rueckruf_betriebsart("aus")
                elif text == "heat":
                    await self._rueckruf_betriebsart(self._letzte_aktive)
            elif topic == f"{b}/sollwert/set":
                w = min(max(float(text), parameter.SOLLWERT_MIN), parameter.SOLLWERT_MAX)
                await self._rueckruf_sollwert(w)
            elif topic.startswith(f"{b}/taste/"):
                name = topic[len(f"{b}/taste/"):]
                if name in TASTEN:
                    await self._rueckruf_taste(name)
        except ValueError:
            log.warning("Ungültiger Wert %r auf %s", text, topic)
        except Exception:
            log.exception("Fehler bei MQTT-Nachricht %s", topic)
