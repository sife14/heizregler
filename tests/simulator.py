"""Zweispeicher-Strecke (Luft, Masse) mit Nachbarkopplung für Tests.

Grob an die Identifikation vom 19./20.10. angelehnt: Totzeit 10 min,
schneller Modus ~30 min, langsamer Modus mehrere Stunden, Heizleistung
k·u·(T_vor − T_luft). Lüften koppelt die Luft stark an außen.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass
class Strecke:
    t_luft: float = 20.5
    t_masse: float = 20.5
    t_nachbar: float = 20.6
    t_aussen: float = 8.0
    t_vor: float = 32.0
    # Kehrwerte der Zeitkonstanten [1/min]
    a_lm: float = 1 / 35  # Luft <-> Masse (aus Sicht der Luft)
    a_ml: float = 1 / 400  # Masse <-> Luft (aus Sicht der Masse)
    a_ln: float = 1 / 600  # Luft <-> Nachbarn
    a_heiz: float = 1 / 120  # Heizkörper bei u = 1, pro K Übertemperatur
    a_lueft: float = 1 / 12  # Fenster offen
    totzeit_min: int = 10
    fenster: bool = False
    _u_puffer: deque[float] = field(default_factory=deque)

    def schritt(self, u: float, dt_min: float = 1.0) -> float:
        if not self._u_puffer:
            self._u_puffer.extend([u] * self.totzeit_min)
        self._u_puffer.append(u)
        u_wirk = self._u_puffer.popleft()
        heiz = self.a_heiz * u_wirk * max(self.t_vor - self.t_luft, 0.0)
        lueft = self.a_lueft * (self.t_aussen - self.t_luft) if self.fenster else 0.0
        d_luft = heiz + self.a_lm * (self.t_masse - self.t_luft) + self.a_ln * (self.t_nachbar - self.t_luft) + lueft
        d_masse = self.a_ml * (self.t_luft - self.t_masse)
        self.t_luft += d_luft * dt_min
        self.t_masse += d_masse * dt_min
        return self.t_luft
