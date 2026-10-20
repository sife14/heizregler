"""Taktprotokoll direkt in InfluxDB 2.x (Line Protocol über /api/v2/write).

Measurements: ``heizregler`` (ein Datensatz pro Takt) und ``heizregler_ereignis``
(Tag ``typ``, Feld ``text``). Zahlen und ints werden als float geschrieben,
bool als Boolean, datetime als Unix-Sekunden (float), Text als String;
None und NaN/inf entfallen. Bei Ausfall bleiben bis zu 1440 Zeilen im
Ringpuffer und werden beim nächsten erfolgreichen Schreiben nachgeholt.
Bei 400 (ungültige Daten) wird der Stapel verworfen, damit er nicht alles blockiert.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from datetime import datetime
from typing import Any

import aiohttp

from heizregler.einstellungen import Zugang

log = logging.getLogger(__name__)

MEASUREMENT = "heizregler"
MEASUREMENT_EREIGNIS = "heizregler_ereignis"
STAPEL = 500
WARN_ABSTAND_S = 300


def _esc_name(s: str) -> str:
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(" ", "\\ ")


def _esc_tag(s: str) -> str:
    return _esc_name(s).replace("=", "\\=")


def _esc_text(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def feldwert(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        v = v.timestamp()
    if isinstance(v, (int, float)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else repr(f)
    return _esc_text(str(v))


def zeile(measurement: str, zeit: datetime, felder: dict[str, Any], tags: dict[str, str] | None = None) -> str | None:
    """Eine Zeile Line Protocol (Präzision s); None, wenn kein Feld übrig bleibt."""
    teile = [f"{_esc_tag(k)}={feldwert(v)}" for k, v in felder.items() if feldwert(v) is not None]
    if not teile:
        return None
    kopf = _esc_name(measurement)
    for k, v in sorted((tags or {}).items()):
        if v not in (None, ""):
            kopf += f",{_esc_tag(k)}={_esc_tag(str(v))}"
    return f"{kopf} {','.join(teile)} {int(zeit.timestamp())}"


class InfluxProtokoll:
    """Implementiert schnittstellen.Protokoll."""

    def __init__(self, zugang: Zugang, puffer: int = 1440, timeout_s: float = 10.0) -> None:
        self._url = f"{zugang.influx_url}/api/v2/write" if zugang.influx_url else ""
        self._params = {"org": zugang.influx_org, "bucket": zugang.influx_bucket, "precision": "s"}
        self._kopf = {"Authorization": f"Token {zugang.influx_token}", "Content-Type": "text/plain; charset=utf-8"}
        self._puffer: deque[str] = deque(maxlen=puffer)
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: aiohttp.ClientSession | None = None
        self._sperre = asyncio.Lock()
        self._letzte_warnung = 0.0
        self.geschrieben = 0
        self.verworfen = 0

    @property
    def gepuffert(self) -> int:
        return len(self._puffer)

    async def schreiben(self, zeit: datetime, felder: dict[str, Any], tags: dict[str, str] | None = None) -> None:
        await self._hinzu(zeile(MEASUREMENT, zeit, felder, tags))

    async def ereignis(self, zeit: datetime, typ: str, text: str) -> None:
        await self._hinzu(zeile(MEASUREMENT_EREIGNIS, zeit, {"text": text}, {"typ": typ}))

    async def schliessen(self) -> None:
        await self._leeren()
        if self._session:
            await self._session.close()
            self._session = None

    async def _hinzu(self, z: str | None) -> None:
        try:
            if z is None:
                return
            if not self._url:
                self._warnen("INFLUX_URL fehlt, Protokoll wird nicht geschrieben")
                return
            if len(self._puffer) == self._puffer.maxlen:
                self.verworfen += 1
            self._puffer.append(z)
            await self._leeren()
        except Exception as f:  # nie nach außen werfen
            self._warnen(f"InfluxDB: {f}")

    async def _leeren(self) -> None:
        if not self._puffer or not self._url:
            return
        async with self._sperre:
            if self._session is None:
                self._session = aiohttp.ClientSession(timeout=self._timeout)
            while self._puffer:
                stapel = [self._puffer[i] for i in range(min(STAPEL, len(self._puffer)))]
                try:
                    async with self._session.post(
                        self._url, params=self._params, headers=self._kopf, data="\n".join(stapel).encode()
                    ) as antwort:
                        if antwort.status == 204:
                            self.geschrieben += len(stapel)
                        elif antwort.status == 400:
                            self.verworfen += len(stapel)
                            self._warnen(f"InfluxDB lehnt Daten ab: {(await antwort.text())[:200]}")
                        else:
                            self._warnen(f"InfluxDB HTTP {antwort.status}: {(await antwort.text())[:200]}")
                            return
                except (aiohttp.ClientError, asyncio.TimeoutError) as f:
                    self._warnen(f"InfluxDB nicht erreichbar: {f!r}")
                    return
                for _ in stapel:
                    self._puffer.popleft()

    def _warnen(self, text: str) -> None:
        jetzt = time.monotonic()
        if jetzt - self._letzte_warnung >= WARN_ABSTAND_S:
            self._letzte_warnung = jetzt
            log.warning("%s (gepuffert: %s)", text, len(self._puffer))
