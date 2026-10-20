from datetime import datetime, timezone

from aiohttp import web
from aiohttp.test_utils import TestServer

from heizregler.einstellungen import Zugang
from heizregler.quellen.influx import InfluxProtokoll, zeile

T = datetime(2026, 10, 20, 12, 0, 5, tzinfo=timezone.utc)


def test_zeile_format():
    z = zeile("heizregler", T, {"y": 21.3, "befehle": 4, "fenster": False, "zustand": 'ak"tiv', "leer": None,
                                "nan": float("nan"), "zeit": datetime(1970, 1, 1, 0, 1, tzinfo=timezone.utc)},
              {"regler": "pi"})
    assert z == ('heizregler,regler=pi y=21.3,befehle=4.0,fenster=false,zustand="ak\\"tiv",zeit=60.0 '
                 f"{int(T.timestamp())}")


def test_zeile_escaping_und_leer():
    z = zeile("heizregler ereignis", T, {"feld a": 1, "b=c": "x\\y"}, {"typ": "a b,c=d"})
    assert z.startswith("heizregler\\ ereignis,typ=a\\ b\\,c\\=d ")
    assert 'feld\\ a=1.0,b\\=c="x\\\\y"' in z
    assert zeile("heizregler", T, {"a": None}) is None


def _zugang(url):
    return Zugang("", "", "", 1883, "", "", url, "home", "heizregler", "geheim")


async def test_puffer_und_nachholen():
    empfangen = []
    status = [503]

    async def write(request):
        assert request.headers["Authorization"] == "Token geheim"
        assert request.query["bucket"] == "heizregler" and request.query["precision"] == "s"
        if status[0] != 204:
            return web.Response(status=status[0], text="down")
        empfangen.extend((await request.text()).splitlines())
        return web.Response(status=204)

    app = web.Application()
    app.router.add_post("/api/v2/write", write)
    async with TestServer(app) as server:
        p = InfluxProtokoll(_zugang(str(server.make_url("")).rstrip("/")))
        await p.schreiben(T, {"y": 1.0})
        await p.schreiben(T, {"y": 2.0})
        assert p.gepuffert == 2 and empfangen == []
        status[0] = 204
        await p.ereignis(T, "uebernahme", "ok")
        assert p.gepuffert == 0 and len(empfangen) == 3
        assert empfangen[2].startswith('heizregler_ereignis,typ=uebernahme text="ok"')
        await p.schliessen()


async def test_400_verwirft_stapel():
    async def write(request):
        return web.Response(status=400, text="bad")

    app = web.Application()
    app.router.add_post("/api/v2/write", write)
    async with TestServer(app) as server:
        p = InfluxProtokoll(_zugang(str(server.make_url("")).rstrip("/")))
        await p.schreiben(T, {"y": 1.0})
        assert p.gepuffert == 0 and p.verworfen == 1
        await p.schliessen()


async def test_nicht_erreichbar_wirft_nicht():
    p = InfluxProtokoll(_zugang("http://127.0.0.1:9"))
    await p.schreiben(T, {"y": 1.0})
    assert p.gepuffert == 1
    await p.schliessen()
