# TowerScan v2

Passive GSM cell tower scanner and mapper for **Raspberry Pi 5** + **Nooelec NESDR SMArt XTR**.

## Architecture

```
RTL-SDR dongle
    │
    ▼
rtl_power           ← sweeps GSM frequency bands, finds RF carriers
    │
    ▼
scan.py             ← identifies ARFCNs, stores detections
    │
    ├── OpenCelliD CSV   ← offline tower database (lat/lon/MCC/MNC/LAC/CI)
    │       ↓
    │   towers identified instantly, no internet needed
    │
    └── OpenCelliD API / Mozilla ← online fallback for individual lookups
            ↓
        towers.db (SQLite)
            ↓
        server.py → http://<pi>:5000  (Leaflet map)
```

## Files

| File | Purpose |
|------|---------|
| `scan.py` | RF scanner + OCID lookup. All scanning logic. |
| `server.py` | Web server. Serves the map on port 5000. |
| `setup.sh` | Installs all dependencies including gr-gsm from source. |
| `towers.db` | SQLite database (auto-created). |
| `config.json` | Settings (auto-created, edit as needed). |
| `cell_towers.csv.gz` | OpenCelliD database — you provide this. |

## Quick Start

### 1. Install
```bash
chmod +x setup.sh
./setup.sh
sudo reboot
```

### 2. Download OpenCelliD data
Register free at https://opencellid.org then download the Czech Republic extract:
```bash
cd ~/gsm-inspector-mk1
wget -O cell_towers.csv.gz \
  "https://opencellid.org/ocid/downloads?token=YOUR_TOKEN&type=mcc&file=mcc-230.csv.gz"
```

### 3. Import nearby towers (populates map immediately)
```bash
python3 scan.py --import-ocid --lat 50.0759 --lon 14.4378 --radius 15
```

### 4. RF scan (detects active carriers)
```bash
python3 scan.py --scan \
  --lat 50.0759 --lon 14.4378 \
  --bands GSM-900 GSM-1800 \
  --duration 60 --gain 40
```

### 5. Open the map
```bash
python3 server.py
# Open http://localhost:5000 or http://<pi-ip>:5000
```

## scan.py Reference

```
--scan            Run RF scan
--import-ocid     Import towers from OCID CSV near your location
--diagnose        Hardware diagnostic
--stats           Show database stats
--lat / --lon     Your location (observer coordinates)
--bands           GSM-900 GSM-1800 GSM-850 GSM-1900
--gain            SDR gain 0-50 (default 40, try 45 for weak signals)
--ppm             Frequency correction (default 0, XTR TCXO is very accurate)
--duration        Seconds per band scan (default 60)
--threshold       dB above noise to count as carrier (default 12, try 8-10 for weak)
--radius          km radius for OCID import (default 15)
--token           OpenCelliD API token (for individual lookups)
```

## Map Legend

| Marker | Meaning |
|--------|---------|
| Cyan circle | GSM-900 tower (identified) |
| Orange circle | GSM-1800 tower (identified) |
| Green circle | GSM-850 tower (identified) |
| Yellow circle | GSM-1900 tower (identified) |
| Yellow triangle | Raw detection (carrier found, not yet identified) |
| Faded marker | Tower in database but no GPS coordinates |

## Frequency Bands

| Band | Downlink | Region |
|------|----------|--------|
| GSM-900 | 935–960 MHz | Europe, Africa, Asia |
| GSM-1800 | 1805–1880 MHz | Europe, Asia (DCS-1800) |
| GSM-850 | 869–894 MHz | Americas |
| GSM-1900 | 1930–1990 MHz | North America |

## Czech Republic Operators (MCC 230)

| MNC | Operator |
|-----|----------|
| 01 | T-Mobile CZ |
| 02 | O2 CZ |
| 03 | Vodafone CZ |
| 04 | Nordic Telecom |
| 06 | Sazka Mobil |

## Tips

- **Antenna**: Keep it vertical. GSM signals are vertically polarized.
- **Location**: Near a window or outdoors dramatically improves results.
- **Gain**: Start at 40. If finding nothing, try 45-50. If overloaded, try 30.
- **Threshold**: Lower if missing weak towers, raise if getting false positives.
- **PPM**: The Nooelec XTR TCXO is accurate enough that `--ppm 0` works well.
- **OCID import**: Run `--import-ocid` before scanning to pre-populate the map
  with all known towers in your area — you'll see them on the map immediately.

## Legal

This tool only receives unencrypted public broadcast signals (GSM BCCH).
No calls, SMS, or user data are intercepted. Passive reception of public
broadcasts is legal in most jurisdictions. Check local regulations.
