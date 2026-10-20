# Heizregler thermostat2

Raumtemperaturregler für `climate.thermostat2` (Eurotronic Spirit, Zigbee2MQTT).
Läuft als systemd-Dienst im LXC-Container 111 `heizregler` (Proxmox) und spricht
mit Home Assistant über die WebSocket-API (Sensoren lesen, Dienste aufrufen) und
über MQTT Discovery (eigenes Gerät „Heizregler“ mit Parametern und Diagnose).
Jeder Rechentakt wird zusätzlich direkt in InfluxDB (Bucket `heizregler`) geschrieben.

## Aufbau

```
src/heizregler/
  parameter.py      Live-Parameter (-> number/switch in HA)
  diagnose.py       Diagnosegrößen (-> Sensoren in HA, Felder in InfluxDB)
  schnittstellen.py Verträge zwischen Kern und Ein-/Ausgabe
  einstellungen.py  heizregler.toml + .local.env
  regler/           austauschbare Regler (basis.py = Schnittstelle, pi.py)
  quellen/          HA-WebSocket, TRV-Stellantrieb, MQTT-Bedienung, InfluxDB
```

## Betrieb

```bash
ssh heizregler
sudo -u heizregler -H bash -lc 'cd /opt/heizregler/app && poetry run pytest'
systemctl status heizregler
journalctl -u heizregler -f
```

Zugangsdaten stehen in `.local.env` (nicht im Repo).
