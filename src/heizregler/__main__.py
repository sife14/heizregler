"""Einstiegspunkt: python -m heizregler [--debug]"""

from __future__ import annotations

import argparse
import asyncio
import logging

from . import einstellungen
from .app import laufen


def main() -> None:
    ap = argparse.ArgumentParser(prog="heizregler")
    ap.add_argument("--debug", action="store_true", help="ausführliches Log")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",  # Zeitstempel setzt journald
    )
    logging.getLogger("aiomqtt").setLevel(logging.WARNING)
    asyncio.run(laufen(einstellungen.laden()))


if __name__ == "__main__":
    main()
