"""PI-Regler mit Sollwertgewichtung und bedingter Integration.

Sollwertgewichtung b als Vorfilter umgesetzt (gleichwertig zu
u = Kp·[(b·w − y) + 1/(Ti·s)·(w − y)]):

    w_f = b·w + (1 − b)·z,   z = Tiefpass(w, Ti)
    P   = Kp·(w_f − y)
    I  += Kp/Ti·(w_f − y)·dt

Im eingeschwungenen Zustand ist w_f = w, P und I sind dann direkt als
Anteile an der Heizanforderung h lesbar. I bleibt in [0, 1]: er steht für
den Dauerbedarf.
"""

from __future__ import annotations

import math
from typing import Any

from .basis import ReglerAusgang, ReglerEingang


class PIRegler:
    name = "pi"

    def __init__(self) -> None:
        self._i = 0.0
        self._z: float | None = None  # Tiefpass des Sollwerts

    def _w_f(self, eingang: ReglerEingang, z: float) -> float:
        b = float(eingang.parameter["b"])
        return b * eingang.w + (1.0 - b) * z

    def _z_neu(self, eingang: ReglerEingang) -> float:
        if self._z is None:
            return eingang.w
        ti_s = float(eingang.parameter["ti_min"]) * 60.0
        a = 1.0 - math.exp(-eingang.dt_s / ti_s)
        return self._z + a * (eingang.w - self._z)

    def schritt(self, eingang: ReglerEingang) -> ReglerAusgang:
        kp = float(eingang.parameter["kp"])
        ti_s = float(eingang.parameter["ti_min"]) * 60.0
        self._z = self._z_neu(eingang)
        w_f = self._w_f(eingang, self._z)
        e_f = w_f - eingang.y
        p = kp * e_f

        di = 0.0
        if eingang.integration_frei:
            di = kp / ti_s * e_f * eingang.dt_s
            h_roh = p + self._i + di
            # Nicht weiter in eine Begrenzung hineinintegrieren
            if (h_roh > eingang.h_max and di > 0) or (h_roh < eingang.h_min and di < 0):
                di = 0.0
            self._i = min(max(self._i + di, 0.0), 1.0)

        h_roh = p + self._i
        h = min(max(h_roh, eingang.h_min), eingang.h_max)
        return ReglerAusgang(
            h=h,
            diagnose={
                "e": eingang.w - eingang.y,
                "p": p,
                "i": self._i,
                "h_roh": h_roh,
                "w_f": w_f,
                "di": di,
            },
        )

    def stossfrei_initialisieren(self, h_aktuell: float, eingang: ReglerEingang) -> None:
        self._z = eingang.w
        p = float(eingang.parameter["kp"]) * (eingang.w - eingang.y)
        self._i = min(max(h_aktuell - p, 0.0), 1.0)

    def zuruecksetzen(self) -> None:
        self._i = 0.0
        self._z = None

    def zustand(self) -> dict[str, Any]:
        return {"i": self._i, "z": self._z}

    def zustand_laden(self, daten: dict[str, Any]) -> None:
        self._i = min(max(float(daten.get("i", 0.0)), 0.0), 1.0)
        z = daten.get("z")
        self._z = None if z is None else float(z)
