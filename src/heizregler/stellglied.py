"""Heizanforderung h -> Ventilstellung, und wann ein Befehl gesendet wird.

Jeder Befehl weckt das batteriebetriebene TRV; deshalb Mindeständerung und
Mindestabstand. Sicherheitsfälle (Fenster, T_max, Frost) erzwingen den Befehl.
"""

from __future__ import annotations

from datetime import datetime


def _interp(x: float, xs: tuple[float, ...], ys: tuple[float, ...]) -> float:
    if x <= xs[0]:
        return ys[0]
    for (x0, y0), (x1, y1) in zip(zip(xs, ys), zip(xs[1:], ys[1:])):
        if x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]


class Stellglied:
    def __init__(self, kennlinie_h: tuple[float, ...], kennlinie_ventil: tuple[int, ...]) -> None:
        if any(b <= a for a, b in zip(kennlinie_h, kennlinie_h[1:])) or any(
            b < a for a, b in zip(kennlinie_ventil, kennlinie_ventil[1:])
        ):
            raise ValueError("Kennlinie muss monoton steigen")
        self.h = kennlinie_h
        self.v = tuple(float(v) for v in kennlinie_ventil)

    def ventil(self, h: float, ventil_max: float) -> int:
        return int(round(min(_interp(min(max(h, 0.0), 1.0), self.h, self.v), ventil_max)))

    def h_aus_ventil(self, ventil: float) -> float:
        return _interp(float(ventil), self.v, self.h)

    @staticmethod
    def senden(
        zeit: datetime,
        ziel: int,
        letzter: int | None,
        letzter_zeit: datetime | None,
        parameter: dict[str, float | bool],
        erzwingen: bool,
    ) -> bool:
        if letzter is None or letzter_zeit is None:
            return True
        if ziel == letzter:
            return False
        if erzwingen:
            return True
        if (zeit - letzter_zeit).total_seconds() < float(parameter["stelltakt_min"]) * 60.0:
            return False
        # Ganz schließen/öffnen auch unterhalb der Mindeständerung
        if ziel in (0, int(parameter["ventil_max"])):
            return True
        return abs(ziel - letzter) >= float(parameter["ventil_min_aenderung"])
