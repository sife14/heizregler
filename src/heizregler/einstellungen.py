"""Feste Konfiguration (heizregler.toml) und Zugangsdaten (.local.env)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJEKT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Zugang:
    ha_url: str
    ha_token: str
    mqtt_host: str
    mqtt_port: int
    mqtt_user: str
    mqtt_password: str
    influx_url: str
    influx_org: str
    influx_bucket: str
    influx_token: str


@dataclass(frozen=True)
class Einstellungen:
    zugang: Zugang
    takt_s: float
    zustand_datei: Path
    mqtt_basis: str
    discovery_praefix: str
    entitaeten: dict[str, object]
    trv: dict[str, object]
    kennlinie_h: tuple[float, ...]
    kennlinie_ventil: tuple[int, ...]


def laden(toml: Path | None = None, env: Path | None = None) -> Einstellungen:
    toml = toml or PROJEKT / "heizregler.toml"
    load_dotenv(env or PROJEKT / ".local.env")
    with toml.open("rb") as f:
        cfg = tomllib.load(f)

    def e(name: str, vorgabe: str = "") -> str:
        return os.environ.get(name, vorgabe).strip()

    zugang = Zugang(
        ha_url=e("HA_URL").rstrip("/"),
        ha_token=e("HA_TOKEN"),
        mqtt_host=e("MQTT_HOST"),
        mqtt_port=int(e("MQTT_PORT", "1883")),
        mqtt_user=e("MQTT_USER"),
        mqtt_password=e("MQTT_PASSWORD"),
        influx_url=e("INFLUX_URL").rstrip("/"),
        influx_org=e("INFLUX_ORG"),
        influx_bucket=e("INFLUX_BUCKET"),
        influx_token=e("INFLUX_TOKEN"),
    )
    a = cfg["allgemein"]
    k = cfg["kennlinie"]
    if len(k["h"]) != len(k["ventil"]) or len(k["h"]) < 2:
        raise ValueError("Kennlinie: h und ventil brauchen gleich viele Punkte (>= 2)")
    return Einstellungen(
        zugang=zugang,
        takt_s=float(a["takt_s"]),
        zustand_datei=Path(a["zustand_datei"]),
        mqtt_basis=a["mqtt_basis"],
        discovery_praefix=a["discovery_praefix"],
        entitaeten=dict(cfg["entitaeten"]),
        trv=dict(cfg["trv"]),
        kennlinie_h=tuple(float(x) for x in k["h"]),
        kennlinie_ventil=tuple(int(x) for x in k["ventil"]),
    )
