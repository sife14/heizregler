"""Verdrahtung und Hauptschleife des Dienstes."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from . import __version__
from .aufsicht import Aufsicht
from .einstellungen import Einstellungen
from .quellen.bedienung import MQTTBedienung
from .quellen.ha import HAWebSocket
from .quellen.influx import InfluxProtokoll
from .quellen.trv import TRVStellantrieb
from .regler.pi import PIRegler

log = logging.getLogger(__name__)

# Entitäten-Keys, die keine einzelne Entity-ID sind
_KEINE_ENTITY = {"z2m_geraet", "konkurrenten"}


def _beobachtete(einst: Einstellungen) -> list[str]:
    ids = [str(v) for k, v in einst.entitaeten.items() if k not in _KEINE_ENTITY]
    ids += [str(e) for e in einst.entitaeten.get("konkurrenten", [])]  # type: ignore[union-attr]
    return sorted(set(ids))


async def laufen(einst: Einstellungen) -> None:
    log.info("Heizregler %s startet, Takt %.0f s", __version__, einst.takt_s)
    ha = HAWebSocket(einst.zugang, _beobachtete(einst))
    antrieb = TRVStellantrieb(ha, einst)
    protokoll = InfluxProtokoll(einst.zugang)
    aufsicht: Aufsicht | None = None

    async def betriebsart(w: str) -> None:
        assert aufsicht is not None
        await aufsicht.betriebsart_setzen(w)

    async def sollwert(w: float) -> None:
        assert aufsicht is not None
        await aufsicht.sollwert_setzen(w)

    async def param(k: str, w: float | bool) -> None:
        assert aufsicht is not None
        await aufsicht.parameter_setzen(k, w)

    async def taste(n: str) -> None:
        assert aufsicht is not None
        await aufsicht.taste(n)

    bedienung = MQTTBedienung(einst, betriebsart, sollwert, param, taste)
    aufsicht = Aufsicht(einst, ha, antrieb, bedienung, protokoll, PIRegler())
    aufsicht.laden()

    stopp = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopp.set)

    await ha.start()
    for _ in range(150):  # bis 15 s auf die erste Verbindung warten
        if ha.verbunden:
            break
        await asyncio.sleep(0.1)
    await bedienung.start()
    await aufsicht.start()
    try:
        while not stopp.is_set():
            try:
                await aufsicht.takt()
            except Exception:  # noqa: BLE001 – ein Fehler im Takt darf den Dienst nicht beenden
                log.exception("Fehler im Takt")
            aufsicht.wecker.clear()
            warten = [asyncio.create_task(stopp.wait()), asyncio.create_task(aufsicht.wecker.wait())]
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.wait(warten, return_when=asyncio.FIRST_COMPLETED), einst.takt_s)
            for t in warten:
                t.cancel()
    finally:
        log.info("Heizregler beendet (TRV bleibt beim Regler, Watchdog in HA greift nach 5 min)")
        aufsicht.speichern()
        await bedienung.stop()
        await protokoll.schliessen()
        await ha.stop()
