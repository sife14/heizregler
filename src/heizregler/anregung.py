"""Dither für die Identifikation im geschlossenen Kreis.

PRBS wie in heizexp: 5-Bit-Fibonacci-LFSR, x^5 + x^3 + 1, Startwert 11111,
Ausgabe = LSB vor dem Schieben, Periode 31 Takte. Der Index hängt nur an der
Uhrzeit (Epoche / Takt), damit die Folge Neustarts übersteht und sich aus den
Zeitstempeln rekonstruieren lässt. 1 -> +a, 0 -> −a.
"""

from __future__ import annotations

from datetime import datetime


def prbs31() -> tuple[int, ...]:
    reg = 0b11111
    folge = []
    for _ in range(31):
        folge.append(reg & 1)
        neu = ((reg >> 0) ^ (reg >> 2)) & 1  # Abgriffe x^5 und x^3
        reg = (reg >> 1) | (neu << 4)
    return tuple(folge)


FOLGE = prbs31()


def index(zeit: datetime, takt_min: float) -> int:
    return int(zeit.timestamp() // (takt_min * 60.0)) % len(FOLGE)


def dither(zeit: datetime, parameter: dict[str, float | bool], erlaubt: bool) -> tuple[float, int]:
    """(Ditherwert, PRBS-Index). 0, wenn aus oder nicht erlaubt."""
    i = index(zeit, float(parameter["dither_takt_min"]))
    if not (parameter["dither_aktiv"] and erlaubt):
        return 0.0, i
    a = float(parameter["dither_amplitude"])
    return (a if FOLGE[i] else -a), i
