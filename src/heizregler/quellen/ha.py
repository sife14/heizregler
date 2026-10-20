"""Home-Assistant-WebSocket: Zustände abonnieren, Dienste aufrufen, Trigger abonnieren.

Entscheidung zu last_reported: Über die WebSocket-API kommt ein unverändertes
Wiedermelden nicht an. ``subscribe_entities`` hört nur auf ``state_changed``
(kompakte Diffs mit ``lc``/``lu``, kein ``lr``), und ``state_reported`` lässt
sich per ``subscribe_events`` nicht abonnieren (der Bus verlangt dafür einen
Event-Filter, den die WebSocket-API nicht setzt). Deshalb:

- ``EntityZustand.last_reported`` ist hier = ``last_updated`` (Näherung).
- Wer jede Gerätemeldung braucht (TRV-Bestätigung), registriert mit
  ``trigger_registrieren`` einen HA-Trigger, z. B. MQTT auf
  ``zigbee2mqtt/<gerät>``; HA leitet jede Nachricht über ``subscribe_trigger``
  weiter. Das verlangt einen Admin-Token.

Trigger-Abos werden nach jedem Neuverbinden erneut angelegt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import aiohttp

from heizregler.einstellungen import Zugang
from heizregler.schnittstellen import EntityZustand, HAFehler

log = logging.getLogger(__name__)

DIENST_TIMEOUT_S = 15.0
BACKOFF_S = (1, 2, 5, 10, 30)

TriggerRueckruf = Callable[[dict[str, Any]], None]


def _zeit(ts: float | None) -> datetime | None:
    return None if ts is None else datetime.fromtimestamp(float(ts), tz=timezone.utc)


def zustand_aus_kompakt(k: dict[str, Any]) -> EntityZustand:
    """Kompakten Zustand (subscribe_entities, Schlüssel s/a/lc/lu) umwandeln."""
    lc = _zeit(k.get("lc")) or datetime.now(timezone.utc)
    lu = _zeit(k.get("lu")) or lc
    return EntityZustand(
        state=str(k.get("s", "unknown")),
        attributes=dict(k.get("a") or {}),
        last_changed=lc,
        last_updated=lu,
        last_reported=lu,
    )


def zustand_mit_diff(alt: EntityZustand, diff: dict[str, Any]) -> EntityZustand:
    """Diff (``+`` Ergänzungen, ``-`` entfernte Attribute) auf einen Zustand anwenden."""
    plus = diff.get("+") or {}
    minus = diff.get("-") or {}
    attrs = dict(alt.attributes)
    for key in minus.get("a", []) or []:
        attrs.pop(key, None)
    attrs.update(plus.get("a") or {})
    lc, lu = alt.last_changed, alt.last_updated
    if "lc" in plus:
        lc = lu = _zeit(plus["lc"]) or lc
    elif "lu" in plus:
        lu = _zeit(plus["lu"]) or lu
    else:
        # Nur Kontext/Attribute ohne Zeitangabe: als frische Meldung werten
        lu = datetime.now(timezone.utc)
    return EntityZustand(
        state=str(plus.get("s", alt.state)),
        attributes=attrs,
        last_changed=lc,
        last_updated=lu,
        last_reported=lu,
    )


class HAWebSocket:
    """Implementiert schnittstellen.HA über /api/websocket."""

    def __init__(self, zugang: Zugang, entity_ids: Iterable[str]) -> None:
        self._url = zugang.ha_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
        self._token = zugang.ha_token
        self._http = zugang.ha_url
        self._entity_ids = sorted(set(entity_ids))
        self._zustaende: dict[str, EntityZustand] = {}
        self._ereignisse: dict[str, asyncio.Event] = {}
        self._trigger: list[tuple[dict[str, Any], TriggerRueckruf]] = []
        self._abo_rueckrufe: dict[int, Callable[[dict[str, Any]], None]] = {}
        self._offen: dict[int, asyncio.Future] = {}
        self._naechste_id = 1
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._aufgabe: asyncio.Task | None = None
        self._verbunden = False
        self._sende_sperre = asyncio.Lock()

    # ------------------------------------------------------------------ API
    @property
    def verbunden(self) -> bool:
        return self._verbunden

    def zustand(self, entity_id: str) -> EntityZustand | None:
        return self._zustaende.get(entity_id)

    async def auffrischen(self, timeout_s: float = 10.0) -> None:
        """last_reported der beobachteten Entities über REST nachtragen.

        Die WebSocket-Abos liefern nur Änderungen; ein Fühler mit konstantem
        Wert sähe sonst veraltet aus. GET /api/states/<id> enthält last_reported.
        """
        if not self._verbunden:
            return
        if self._session is None:
            self._session = aiohttp.ClientSession()
        kopf = {"Authorization": f"Bearer {self._token}"}
        zeit = aiohttp.ClientTimeout(total=timeout_s)
        for eid in self._entity_ids:
            try:
                async with self._session.get(f"{self._http}/api/states/{eid}", headers=kopf, timeout=zeit) as r:
                    if r.status != 200:
                        continue
                    d = await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as fehler:
                log.debug("auffrischen %s: %s", eid, fehler)
                continue
            z = self._zustaende.get(eid)
            lr = d.get("last_reported")
            if z is None or not lr:
                continue
            lr_zeit = datetime.fromisoformat(lr)
            if lr_zeit > z.last_reported:
                self._zustaende[eid] = replace(z, last_reported=lr_zeit)

    async def start(self) -> None:
        if self._aufgabe is None:
            self._aufgabe = asyncio.create_task(self._schleife(), name="ha-websocket")

    async def stop(self) -> None:
        if self._aufgabe:
            self._aufgabe.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._aufgabe
            self._aufgabe = None
        await self._schliessen()
        if self._session:
            await self._session.close()
            self._session = None

    async def dienst(
        self, domain: str, service: str, data: dict[str, Any] | None = None, target: dict[str, Any] | None = None
    ) -> None:
        nachricht: dict[str, Any] = {"type": "call_service", "domain": domain, "service": service}
        if data:
            nachricht["service_data"] = data
        if target:
            nachricht["target"] = target
        await self._anfrage(nachricht, f"{domain}.{service}")

    async def warte_auf(
        self, entity_id: str, bedingung: Callable[[EntityZustand], bool], timeout_s: float
    ) -> EntityZustand | None:
        frist = asyncio.get_running_loop().time() + timeout_s
        while True:
            z = self._zustaende.get(entity_id)
            if z is not None and bedingung(z):
                return z
            rest = frist - asyncio.get_running_loop().time()
            if rest <= 0:
                return None
            ereignis = self._ereignisse.setdefault(entity_id, asyncio.Event())
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ereignis.wait(), rest)

    def trigger_registrieren(self, trigger: dict[str, Any], rueckruf: TriggerRueckruf) -> None:
        """HA-Trigger dauerhaft abonnieren; rueckruf bekommt die ``variables`` (u. a. ``trigger``)."""
        self._trigger.append((trigger, rueckruf))
        if self._verbunden:
            asyncio.get_running_loop().create_task(self._trigger_abonnieren(trigger, rueckruf))

    async def hole_states(self) -> list[dict[str, Any]]:
        """Alle Zustände (nur für Debug)."""
        return await self._anfrage({"type": "get_states"}, "get_states")

    # -------------------------------------------------------------- intern
    async def _anfrage(self, nachricht: dict[str, Any], was: str) -> Any:
        if not self._verbunden or self._ws is None:
            raise HAFehler(f"{was}: keine Verbindung zu HA")
        mid = self._neue_id()
        zukunft: asyncio.Future = asyncio.get_running_loop().create_future()
        self._offen[mid] = zukunft
        try:
            await self._senden({"id": mid, **nachricht})
            antwort = await asyncio.wait_for(zukunft, DIENST_TIMEOUT_S)
        except asyncio.TimeoutError as f:
            raise HAFehler(f"{was}: keine Antwort nach {DIENST_TIMEOUT_S:.0f} s") from f
        except (aiohttp.ClientError, ConnectionError, RuntimeError) as f:
            raise HAFehler(f"{was}: {f}") from f
        finally:
            self._offen.pop(mid, None)
        if not antwort.get("success", False):
            fehler = antwort.get("error") or {}
            raise HAFehler(f"{was}: {fehler.get('code', '?')} {fehler.get('message', '')}".strip())
        return antwort.get("result")

    def _neue_id(self) -> int:
        mid = self._naechste_id
        self._naechste_id += 1
        return mid

    async def _senden(self, nachricht: dict[str, Any]) -> None:
        if self._ws is None:
            raise HAFehler("keine Verbindung zu HA")
        async with self._sende_sperre:
            await self._ws.send_json(nachricht)

    async def _schleife(self) -> None:
        versuch = 0
        while True:
            try:
                await self._verbinden()
                versuch = 0
                await self._empfangen()
            except asyncio.CancelledError:
                raise
            except Exception as f:  # Netz, Auth, Protokoll
                log.warning("HA-WebSocket: %s", f)
            await self._schliessen()
            pause = BACKOFF_S[min(versuch, len(BACKOFF_S) - 1)]
            versuch += 1
            log.info("HA-WebSocket: neuer Versuch in %s s", pause)
            await asyncio.sleep(pause)

    async def _verbinden(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self._url, heartbeat=30, max_msg_size=0)
        erste = await self._ws.receive_json(timeout=10)
        if erste.get("type") != "auth_required":
            raise HAFehler(f"unerwartete Begrüßung: {erste}")
        await self._ws.send_json({"type": "auth", "access_token": self._token})
        antwort = await self._ws.receive_json(timeout=10)
        if antwort.get("type") != "auth_ok":
            raise HAFehler(f"Anmeldung abgelehnt: {antwort.get('message', antwort)}")
        self._verbunden = True
        log.info("HA-WebSocket verbunden (HA %s)", antwort.get("ha_version"))
        # Abos nach dem Start der Empfangsschleife anlegen (Antworten kommen dort an)
        asyncio.get_running_loop().create_task(self._abos_anlegen())

    async def _abos_anlegen(self) -> None:
        try:
            mid = self._neue_id()
            self._abo_rueckrufe[mid] = self._bei_entities
            zukunft: asyncio.Future = asyncio.get_running_loop().create_future()
            self._offen[mid] = zukunft
            await self._senden({"id": mid, "type": "subscribe_entities", "entity_ids": self._entity_ids})
            antwort = await asyncio.wait_for(zukunft, DIENST_TIMEOUT_S)
            self._offen.pop(mid, None)
            if not antwort.get("success"):
                log.error("subscribe_entities fehlgeschlagen: %s", antwort.get("error"))
            for trigger, rueckruf in list(self._trigger):
                await self._trigger_abonnieren(trigger, rueckruf)
        except Exception as f:
            log.error("Abos anlegen fehlgeschlagen: %s", f)

    async def _trigger_abonnieren(self, trigger: dict[str, Any], rueckruf: TriggerRueckruf) -> None:
        mid = self._neue_id()
        self._abo_rueckrufe[mid] = lambda ereignis: rueckruf(ereignis.get("variables") or {})
        zukunft: asyncio.Future = asyncio.get_running_loop().create_future()
        self._offen[mid] = zukunft
        try:
            await self._senden({"id": mid, "type": "subscribe_trigger", "trigger": trigger})
            antwort = await asyncio.wait_for(zukunft, DIENST_TIMEOUT_S)
            if not antwort.get("success"):
                log.error("subscribe_trigger %s fehlgeschlagen: %s", trigger, antwort.get("error"))
                self._abo_rueckrufe.pop(mid, None)
        except Exception as f:
            log.error("subscribe_trigger %s: %s", trigger, f)
            self._abo_rueckrufe.pop(mid, None)
        finally:
            self._offen.pop(mid, None)

    async def _empfangen(self) -> None:
        assert self._ws is not None
        async for nachricht in self._ws:
            if nachricht.type == aiohttp.WSMsgType.TEXT:
                daten = nachricht.json()
                for einzel in daten if isinstance(daten, list) else [daten]:
                    self._verarbeiten(einzel)
            elif nachricht.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
        raise HAFehler("Verbindung von HA geschlossen")

    def _verarbeiten(self, n: dict[str, Any]) -> None:
        typ = n.get("type")
        mid = n.get("id")
        if typ == "result":
            zukunft = self._offen.get(mid)
            if zukunft and not zukunft.done():
                zukunft.set_result(n)
        elif typ == "event":
            rueckruf = self._abo_rueckrufe.get(mid)
            if rueckruf:
                try:
                    rueckruf(n.get("event") or {})
                except Exception:
                    log.exception("Fehler im Rückruf für Abo %s", mid)
        elif typ == "pong":
            pass

    def _bei_entities(self, ereignis: dict[str, Any]) -> None:
        for entity_id, k in (ereignis.get("a") or {}).items():
            self._aktualisieren(entity_id, zustand_aus_kompakt(k))
        for entity_id, diff in (ereignis.get("c") or {}).items():
            alt = self._zustaende.get(entity_id)
            if alt is not None:
                self._aktualisieren(entity_id, zustand_mit_diff(alt, diff))
        for entity_id in ereignis.get("r") or []:
            self._zustaende.pop(entity_id, None)
            self._melden(entity_id)

    def _aktualisieren(self, entity_id: str, z: EntityZustand) -> None:
        self._zustaende[entity_id] = z
        self._melden(entity_id)

    def _melden(self, entity_id: str) -> None:
        ereignis = self._ereignisse.pop(entity_id, None)
        if ereignis:
            ereignis.set()

    async def _schliessen(self) -> None:
        self._verbunden = False
        for zukunft in self._offen.values():
            if not zukunft.done():
                zukunft.set_exception(HAFehler("Verbindung zu HA verloren"))
        self._offen.clear()
        self._abo_rueckrufe.clear()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
