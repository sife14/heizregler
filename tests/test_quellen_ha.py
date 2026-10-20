import asyncio
from datetime import datetime, timezone

from heizregler.einstellungen import Zugang
from heizregler.quellen.ha import HAWebSocket, zustand_aus_kompakt, zustand_mit_diff


def test_kompakt_und_diff():
    z = zustand_aus_kompakt({"s": "16", "a": {"min": 0, "max": 255}, "c": "x", "lc": 1000.0})
    assert z.state == "16" and z.last_changed == z.last_updated == datetime.fromtimestamp(1000, timezone.utc)
    z2 = zustand_mit_diff(z, {"+": {"lu": 1010.0, "a": {"step": 1}}, "-": {"a": ["max"]}})
    assert z2.state == "16" and z2.attributes == {"min": 0, "step": 1}
    assert z2.last_changed == z.last_changed and z2.last_updated.timestamp() == 1010.0
    assert z2.last_reported == z2.last_updated
    z3 = zustand_mit_diff(z2, {"+": {"s": "32", "lc": 1020.0}})
    assert z3.state == "32" and z3.last_changed.timestamp() == z3.last_updated.timestamp() == 1020.0


def _ha():
    return HAWebSocket(Zugang("http://ha:8123", "t", "", 1883, "", "", "", "", "", ""), ["sensor.a"])


async def test_events_aktualisieren_und_warte_auf():
    ha = _ha()
    assert ha._url == "ws://ha:8123/api/websocket"
    ha._bei_entities({"a": {"sensor.a": {"s": "1", "a": {}, "lc": 1.0}}})
    assert ha.zustand("sensor.a").state == "1"

    async def spaeter():
        await asyncio.sleep(0.02)
        ha._bei_entities({"c": {"sensor.a": {"+": {"s": "2", "lc": 2.0}}}})

    asyncio.get_running_loop().create_task(spaeter())
    z = await ha.warte_auf("sensor.a", lambda z: z.state == "2", 1.0)
    assert z is not None and z.state == "2"
    assert await ha.warte_auf("sensor.a", lambda z: z.state == "3", 0.05) is None


async def test_dienst_ohne_verbindung_wirft():
    from heizregler.schnittstellen import HAFehler

    ha = _ha()
    try:
        await ha.dienst("number", "set_value", {"value": 1}, {"entity_id": "number.x"})
    except HAFehler as f:
        assert "keine Verbindung" in str(f)
    else:
        raise AssertionError("HAFehler erwartet")


async def test_ergebnis_und_trigger_routing():
    ha = _ha()
    erhalten = []
    ha._abo_rueckrufe[7] = lambda ereignis: erhalten.append(ereignis.get("variables"))
    zukunft = asyncio.get_running_loop().create_future()
    ha._offen[3] = zukunft
    ha._verarbeiten({"id": 3, "type": "result", "success": True, "result": None})
    ha._verarbeiten({"id": 7, "type": "event", "event": {"variables": {"trigger": {"payload": "{}"}}}})
    assert zukunft.result()["success"] and erhalten == [{"trigger": {"payload": "{}"}}]
