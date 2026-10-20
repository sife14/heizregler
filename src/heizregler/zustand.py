"""Persistenter Zustand (JSON, atomar geschrieben)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def laden(datei: Path) -> dict[str, Any]:
    try:
        return json.loads(datei.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as fehler:
        log.error("Zustand %s unlesbar, starte mit Vorgaben: %s", datei, fehler)
        return {}


def speichern(datei: Path, daten: dict[str, Any]) -> None:
    datei.parent.mkdir(parents=True, exist_ok=True)
    tmp = datei.with_suffix(".tmp")
    tmp.write_text(json.dumps(daten, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, datei)
